"""
CallSession — per-call state machine.

Replaces ``_run_session()`` and its half-dozen ``nonlocal`` variables
with a clean class. Every event handler is a method; every piece of
mutable state is an attribute.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    APIConnectOptions,
    JobContext,
    TurnHandlingOptions,
    room_io,
)
# SessionConnectOptions is not re-exported at the package top level.
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.agents.voice.room_io.types import AudioInputOptions
from livekit.plugins import openai

from my_agent.plugin_stt import create_stt_plugin, SttPluginPack
from my_agent.plugin_tts import (
    aclose_tts,
    ConfirmationPlayer,
    create_tts_plugin,
    TtsPluginPack,
)
from my_agent.plugin_tts_transforms import digits_to_words
from my_agent import config
from my_agent.http_api import register_call, unregister_call
from my_agent.utils.audio_diag import tap_audio

logger = logging.getLogger("agent.session")


class CallSession:
    """Encapsulates a single call lifecycle.

    Usage::

        session = CallSession(ctx, instructions="…")
        await session.run()
    """

    def __init__(self, ctx: JobContext, instructions: str = "") -> None:
        self.ctx = ctx
        self.instructions = instructions

        # ── Mutable state (was ``nonlocal`` in the old code) ──────────
        self._last_resume_time: float = time.monotonic()
        self._last_confirmation: str | None = None
        self._llm_errored_this_turn: bool = False
        self._agent_state: str = ""
        # Serialise silence handling: empty-EOU and silence_timeout events can
        # fire concurrently, and overlapping reminders/farewell would race the
        # listening-gate and double-play phrases.
        self._silence_lock: asyncio.Lock = asyncio.Lock()
        # Count of consecutive spurious early stream closes (see
        # STT_EARLY_CLOSE_MAX_COUNT). Reset on a real silence period.
        self._early_close_count: int = 0
        self.close_ev: asyncio.Event = asyncio.Event()
        self._tap_tasks: list[asyncio.Task] = []
        # Strong refs to fire-and-forget tasks. asyncio keeps only weak
        # references, so without this set the GC may cancel a playback
        # task mid-flight.
        self._bg_tasks: set[asyncio.Task] = set()

        # ── Plugins (created in _build_plugins) ───────────────────────
        self._stt_pack: SttPluginPack | None = None
        self._tts_pack: TtsPluginPack | None = None
        self._llm_plugin: openai.LLM | None = None

        # ── Session (created in run()) ────────────────────────────────
        self._session: AgentSession | None = None

        # ── ConfirmationPlayer (created in run()) ─────────────────────
        self._conf_player: ConfirmationPlayer | None = None

    # -------------------------------------------------------------------
    # Plugin lifecycle
    # -------------------------------------------------------------------

    def _build_plugins(self) -> None:
        """Create STT, TTS and LLM plugins."""
        self._stt_pack = create_stt_plugin(language="ru-RU")
        self._tts_pack = create_tts_plugin()

        llm_kwargs: dict[str, Any] = dict(
            model=config.LLM_MODEL,
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY or "ollama",
            temperature=0.7,
            max_completion_tokens=256,
            timeout=config.LLM_TIMEOUT,
        )
        # Use job ID for session affinity so all requests from the same
        # call land in the same OpenClaw / Ollama session.
        llm_kwargs["user"] = self.ctx.job.id
        self._llm_plugin = openai.LLM(**llm_kwargs)

        logger.info(
            "llm config: model=%s base_url=%s user=%s",
            config.LLM_MODEL, config.LLM_BASE_URL, self.ctx.job.id,
        )

    async def _aclose_plugins(self) -> None:
        """Shut down STT and TTS gracefully."""
        if self._stt_pack is not None:
            await self._stt_pack.plugin.aclose()
        if self._tts_pack is not None:
            await aclose_tts(self._tts_pack)

    # -------------------------------------------------------------------
    # Event handlers (was ``nonlocal`` closure functions)
    # -------------------------------------------------------------------

    def _spawn(self, coro: Any, *, name: str) -> asyncio.Task:
        """Schedule a fire-and-forget task and keep a strong reference."""
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _on_agent_state(self, ev: Any) -> None:
        """Track agent FSM transitions and drive the STT listening-gate."""
        logger.info("[session] agent_state: %s -> %s", ev.old_state, ev.new_state)
        self._agent_state = ev.new_state
        if ev.new_state == "listening":
            self._last_resume_time = time.monotonic()
            if self._stt_pack is not None:
                self._stt_pack.plugin.set_listening(True)
        elif ev.new_state in ("thinking", "speaking"):
            if self._stt_pack is not None:
                self._stt_pack.plugin.set_listening(False)

    def _on_user_state(self, ev: Any) -> None:
        logger.info("[session] user_state: %s -> %s", ev.old_state, ev.new_state)

    def _on_user_input(self, ev: Any) -> None:
        """Play a brief confirmation phrase after a FINAL transcript.

        De-duplicated by text — FINAL_TRANSCRIPT comes once just before
        END_OF_SPEECH.  If we already played a confirmation for this
        text, skip.
        """
        if ev.is_final and ev.transcript:
            text = ev.transcript.strip()
            if text == self._last_confirmation:
                return
            self._last_confirmation = text
            self._llm_errored_this_turn = False
            logger.info("[session] confirmation: → %s", text[:60])
            if self._conf_player is not None:
                self._spawn(self._conf_player.play(), name="confirmation_playback")

    def _on_close(self, ev: Any) -> None:
        logger.info(
            "[session] CLOSE event: reason=%s error=%s",
            getattr(ev, "reason", "?"), getattr(ev, "error", None),
        )
        self.close_ev.set()

    def _on_error(self, ev: Any) -> None:
        error = getattr(ev, "error", ev)
        source_name = type(getattr(ev, "source", None)).__name__
        error_type_name = type(error).__name__
        # ``recoverable`` lives on the error object (LLMError.recoverable), NOT
        # on ErrorEvent — ``getattr(ev, "recoverable", ...)`` would always fall
        # back to the default and treat every error as recoverable.
        recoverable = getattr(error, "recoverable", True)
        logger.error(
            "[session] ERROR event: source=%s type=%s recoverable=%s error=%s",
            source_name, error_type_name, recoverable, error,
        )

        if source_name != "LLM" or self._llm_errored_this_turn:
            return

        # Skip the apology ONLY while a (partial) reply is actively being
        # spoken — that is the overlap case the apology must not collide
        # with. In every other situation the user is left in silence and
        # must be told to repeat, even when ``recoverable=True``:
        # LiveKit's ``recoverable`` means "the session survives", NOT
        # "a successful retry with audio is guaranteed". Gating on the
        # flag alone (old code) suppressed the apology on genuine LLM
        # failures, leaving the caller in total silence.
        if recoverable and self._agent_state == "speaking":
            logger.info(
                "[session] LLM error → recoverable & TTS active, skipping apology"
            )
            return

        # Mark handled only when we actually apologise, so a later
        # non-recoverable error in the same turn is not swallowed by the
        # flag set on an earlier recoverable one.
        self._llm_errored_this_turn = True
        logger.info(
            "[session] LLM error → playing apology (recoverable=%s, state=%s)",
            recoverable, self._agent_state,
        )
        if self._conf_player is not None:
            self._spawn(
                self._conf_player.play_text(
                    "Sorry, it looks like the task took too long. "
                    "Please repeat your question."
                ),
                name="apology_playback",
            )

    # -------------------------------------------------------------------
    # Silence timeout handler
    # -------------------------------------------------------------------

    async def _on_silence_timeout(self, count: int) -> None:
        """Handle STT silence events — reminder → farewell → hangup.

        Re-entrant-safe: empty-EOU and ``silence_timeout`` events can be
        emitted concurrently by the STT plugin. Overlapping invocations
        would double-play phrases and race the listening-gate, so a busy
        handler skips duplicate events and a closing call is a no-op.
        """
        if self.close_ev.is_set():
            return
        if self._silence_lock.locked():
            logger.info("[silence] handler busy, skipping duplicate event (count=%d)", count)
            return

        async with self._silence_lock:
            elapsed = time.monotonic() - self._last_resume_time
            early = elapsed < config.STT_NO_SPEECH_TIMEOUT

            if early:
                self._early_close_count += 1
            else:
                # A real silence period — the early-close streak is broken.
                self._early_close_count = 0

            too_many_early = self._early_close_count >= config.STT_EARLY_CLOSE_MAX_COUNT

            # A spurious early close that has not hit the cap is not real
            # silence — retry without bumping the silence counter so it
            # cannot accumulate toward STT_SILENCE_MAX_COUNT.
            if early and not too_many_early:
                logger.info(
                    "[silence] ignoring early close #%d/%d (%.1fs < %.1fs timeout), retrying",
                    self._early_close_count, config.STT_EARLY_CLOSE_MAX_COUNT,
                    elapsed, config.STT_NO_SPEECH_TIMEOUT,
                )
                self._last_resume_time = time.monotonic()
                if self._stt_pack is not None:
                    self._stt_pack.plugin.reset_silence_counter()
                    self._stt_pack.plugin.set_listening(True)
                return

            # Escalate to hangup when either the silence counter reaches its
            # cap or Sber keeps closing the stream early forever (responses=0).
            escalate = too_many_early or count >= config.STT_SILENCE_MAX_COUNT

            if not escalate:
                logger.info(
                    "[silence] timeout #%d/%d → play reminder",
                    count, config.STT_SILENCE_MAX_COUNT,
                )
                if self._stt_pack is not None:
                    self._stt_pack.plugin.set_listening(False)
                if self._conf_player is not None:
                    await self._conf_player.play_phrase("reminder")
                self._last_resume_time = time.monotonic()
                if self._stt_pack is not None:
                    self._stt_pack.plugin.set_listening(True)
            else:
                reason = (
                    f"early-close cap #{self._early_close_count}"
                    if too_many_early
                    else f"timeout #{count}/{config.STT_SILENCE_MAX_COUNT}"
                )
                logger.info("[silence] %s → farewell and hangup", reason)
                if self._stt_pack is not None:
                    self._stt_pack.plugin.set_listening(False)
                if self._conf_player is not None:
                    await self._conf_player.play_phrase("farewell")
                await asyncio.sleep(2.0)
                self.close_ev.set()

    # -------------------------------------------------------------------
    # Room event handlers
    # -------------------------------------------------------------------

    def _on_participant_connected(self, p: rtc.RemoteParticipant) -> None:
        logger.info(
            "[room] participant_connected: identity=%s kind=%s name=%s",
            p.identity, p.kind, p.name,
        )

    def _on_track_subscribed(
        self,
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        logger.info(
            "[room] track_subscribed: kind=%s name=%s mime=%s source=%s "
            "participant=%s (kind=%s)",
            track.kind, publication.name,
            getattr(publication, "mime_type", "?"),
            publication.source, participant.identity, participant.kind,
        )
        if config.TAP_AUDIO and track.kind == rtc.TrackKind.KIND_AUDIO:
            label = f"{participant.identity}/{publication.name}"
            t = asyncio.create_task(tap_audio(track, label))
            self._tap_tasks.append(t)

    # -------------------------------------------------------------------
    # Main lifecycle
    # -------------------------------------------------------------------

    async def run(self) -> None:
        """Execute the full call lifecycle.

        This is the async equivalent of the old ``_run_session()``.
        """
        self._build_plugins()
        assert self._stt_pack is not None
        assert self._tts_pack is not None
        assert self._llm_plugin is not None

        # ── ConfirmationPlayer ────────────────────────────────────────
        self._conf_player = ConfirmationPlayer(
            self.ctx.room,
            self._tts_pack.engine.http_client,
        )

        # ── Build the prompt ──────────────────────────────────────────
        combined_prompt = config.SYSTEM_PROMPT
        if self.instructions:
            combined_prompt += "\n\n" + self.instructions
            logger.info("[agent] extra instructions appended (%d chars)", len(self.instructions))
        combined_prompt += (
            "\n\n"
            "You can end the call early if the user asks:\n"
            f"  POST /hangup at http://localhost:{config.CALL_API_PORT}/hangup "
            f'with body {{"room": "{self.ctx.room.name}"}}\n'
            'Before hanging up, say "Goodbye".'
        )
        logger.info("[agent] room=%s injected into prompt for hangup", self.ctx.room.name)

        # ── Create Agent ──────────────────────────────────────────────
        agent = Agent(
            instructions=combined_prompt,
            stt=self._stt_pack.plugin,
            tts=self._tts_pack.plugin,
            llm=self._llm_plugin,
        )

        # ── Audio input options ───────────────────────────────────────
        sip_audio_input = AudioInputOptions(
            sample_rate=24000,
            num_channels=1,
            frame_size_ms=20,
            noise_cancellation=None,
            auto_gain_control=False,
            pre_connect_audio=False,
        )
        logger.info(
            "[room_io] audio input: sr=%d ch=%d frame_ms=%d nc=%s agc=%s pre_connect=%s",
            sip_audio_input.sample_rate, sip_audio_input.num_channels,
            sip_audio_input.frame_size_ms, sip_audio_input.noise_cancellation,
            sip_audio_input.auto_gain_control, sip_audio_input.pre_connect_audio,
        )

        # ── Register room event handlers ──────────────────────────────
        @self.ctx.room.on("participant_connected")
        def _on_participant_connected(p: rtc.RemoteParticipant) -> None:
            self._on_participant_connected(p)

        @self.ctx.room.on("track_subscribed")
        def _on_track_subscribed(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            self._on_track_subscribed(track, publication, participant)

        # ── Register in global call registry ──────────────────────────
        loop = asyncio.get_running_loop()
        register_call(self.ctx.room.name, self.close_ev, loop)

        # ── Silence timeout callback ──────────────────────────────────
        self._stt_pack.plugin.set_silence_timeout_handler(self._on_silence_timeout)

        call_start = time.monotonic()
        try:
            await self.ctx.connect()

            # Greeting BEFORE session.start — otherwise STT opens gRPC
            # on empty audio (speaker echo) and Sber closes immediately.
            if self._conf_player is not None:
                await self._conf_player.play_phrase("greeting")

            # ── AgentSession ──────────────────────────────────────────
            # Per-turn LLM retry/limits. NOTE: the openai plugin overrides
            # the httpx timeout with APIConnectOptions.timeout per request,
            # so LLM_REQUEST_TIMEOUT below is the EFFECTIVE per-attempt
            # timeout (the 10s LiveKit default was too short for Ollama's
            # first token on long contexts). Low max_retry avoids stacking
            # timeouts; a higher max_unrecoverable_errors lets the call
            # survive a transient backend blip instead of being force-closed.
            llm_conn_options = APIConnectOptions(
                max_retry=config.LLM_MAX_RETRY,
                timeout=config.LLM_REQUEST_TIMEOUT,
            )
            logger.info(
                "[llm] conn_options: max_retry=%d request_timeout=%.0fs "
                "max_unrecoverable_errors=%d",
                config.LLM_MAX_RETRY, config.LLM_REQUEST_TIMEOUT,
                config.LLM_MAX_UNRECOVERABLE_ERRORS,
            )
            session = AgentSession(
                aec_warmup_duration=0,
                conn_options=SessionConnectOptions(
                    llm_conn_options=llm_conn_options,
                    max_unrecoverable_errors=config.LLM_MAX_UNRECOVERABLE_ERRORS,
                ),
                turn_handling=TurnHandlingOptions(
                    turn_detection="stt",
                    endpointing={"min_delay": 0, "max_delay": 0},
                    interruption={
                        "enabled": False,
                        "discard_audio_if_uninterruptible": False,
                    },
                    preemptive_generation={"enabled": False},
                ),
                tts_text_transforms=[
                    "filter_markdown",
                    "filter_emoji",
                    digits_to_words,
                ],
            )

            # Register event handlers on the session
            session.on("agent_state_changed", self._on_agent_state)
            session.on("user_state_changed", self._on_user_state)
            session.on("user_input_transcribed", self._on_user_input)
            session.on("close", self._on_close)
            session.on("error", self._on_error)

            self._session = session

            await session.start(
                agent=agent,
                room=self.ctx.room,
                room_options=room_io.RoomOptions(
                    audio_input=sip_audio_input,
                    close_on_disconnect=True,
                ),
            )

            # Wait for the call to finish (close event, NOT wait_for_inactive)
            await self.close_ev.wait()

        except Exception:
            logger.exception("[session] error")
        finally:
            unregister_call(self.ctx.room.name)
            logger.info("[session] unregistered from active_calls: room=%s", self.ctx.room.name)

            call_duration = time.monotonic() - call_start
            logger.info("[session] CALL END: duration=%.0fs", call_duration)

            # Remove SIP participants before the room closes
            for identity in list(self.ctx.room.remote_participants.keys()):
                try:
                    logger.info("[cleanup] removing SIP participant %s", identity)
                    await self.ctx.api.room.remove_participant(
                        api.RoomParticipantIdentity(
                            room=self.ctx.room.name, identity=identity,
                        )
                    )
                except Exception:
                    logger.exception("[cleanup] failed to remove participant %s", identity)

            for t in self._tap_tasks:
                t.cancel()
            if self._tap_tasks:
                await asyncio.gather(*self._tap_tasks, return_exceptions=True)

            # Cancel in-flight playback tasks (confirmation/apology) so they
            # don't publish a track into a room that is being torn down.
            for t in list(self._bg_tasks):
                t.cancel()
            if self._bg_tasks:
                await asyncio.gather(*self._bg_tasks, return_exceptions=True)

            if self._session is not None:
                await self._session.aclose()
            await self._aclose_plugins()
            self.ctx.shutdown()
