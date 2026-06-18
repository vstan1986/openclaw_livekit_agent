"""
LiveKit STT plugin — universal WebSocket bridge to a remote STT service.

Contract
--------
- The agent sends binary PCM chunks (int16, 16 kHz, mono) over a WebSocket.
- The service replies with JSON events (interim, final, eou, silence_timeout).
- No knowledge of the STT engine (Sber, T-One, Whisper, Vosk, …).
- The service owns all audio configuration (language, sample rate, gain, VAD).

Contents
--------
- ``SttWsClient`` — WebSocket transport (connect, send, receive, reconnect).
- ``WsSttStream`` — LiveKit ``RecognizeStream`` wrapping ``SttWsClient``.
- ``SttWsPlugin`` — LiveKit STT plugin.
- ``SttPluginPack``, ``create_stt_plugin()`` — factory.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from livekit.agents import stt, APIConnectOptions, utils
from livekit.agents.types import NotGivenOr, NOT_GIVEN

if TYPE_CHECKING:
    from websockets.client import WebSocketClientProtocol

logger = logging.getLogger("agent.plugin_stt")

_SAMPLE_RATE = 16000



# ---------------------------------------------------------------------------
# SttWsClient — WebSocket transport
# ---------------------------------------------------------------------------


class SttWsClient:
    """WebSocket client for the remote STT service.

    Handles connection, exponential-backoff reconnection, binary audio
    send, JSON command send, and message receive.

    Args:
        base_url: Root URL of the STT service (e.g. ``http://127.0.0.1:8092``).
    """

    _RETRY_BASE = 1.0
    _RETRY_MAX = 16.0
    _RETRY_FACTOR = 2.0

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._ws: WebSocketClientProtocol | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open a WebSocket connection with exponential backoff."""
        import websockets

        url = self._base_url.replace("http://", "ws://").replace("https://", "wss://")
        url = url.rstrip("/") + "/ws"

        backoff = self._RETRY_BASE
        last_err: str | None = None
        while backoff <= self._RETRY_MAX:
            try:
                self._ws = await websockets.connect(url, ping_interval=20, ping_timeout=10)
                logger.info("stt ws connected to %s", url)
                return
            except Exception as e:
                last_err = str(e)
                logger.warning("stt ws connect failed (%s), retry in %.1fs", last_err, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * self._RETRY_FACTOR, self._RETRY_MAX)

        raise ConnectionError(f"stt ws connect failed after all retries: {last_err}")

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    async def send_audio(self, data: bytes) -> None:
        """Send a binary PCM frame."""
        if self._ws is not None:
            await self._ws.send(data)

    async def send_command(self, cmd: dict[str, Any]) -> None:
        """Send a JSON command (e.g. ``{"type": "start"}``)."""
        if self._ws is not None:
            await self._ws.send(json.dumps(cmd))

    # ------------------------------------------------------------------
    # Receive
    # ------------------------------------------------------------------

    async def read_message(self) -> bytes | dict[str, Any]:
        """Read the next WebSocket message.

        Returns:
            - ``bytes`` for binary audio frames (server -> client, rare).
            - ``dict`` for JSON events.
        """
        if self._ws is None:
            raise ConnectionError("WebSocket is not connected")

        msg = await self._ws.recv()
        if isinstance(msg, bytes):
            return msg
        return json.loads(msg)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()


class WsSttStream(stt.RecognizeStream):
    """RecognizeStream backed by a WebSocket connection to the STT service."""

    def __init__(
        self,
        *,
        stt_plugin: SttWsPlugin,
        conn_options: APIConnectOptions,
        sample_rate: int = _SAMPLE_RATE,
    ) -> None:
        super().__init__(stt=stt_plugin, conn_options=conn_options, sample_rate=sample_rate)
        self._plugin = stt_plugin
        self._has_seen_eou: bool = False
        # Text of the last FINAL_TRANSCRIPT emitted this turn. The service
        # sends ``final`` then ``eou`` with the same text, so the ``eou``
        # branch must not re-emit FINAL for an already-final transcript
        # (that produced a duplicate FINAL into AgentSession). Reset at the
        # end of every turn (END_OF_SPEECH).
        self._last_final_text: str | None = None
        # Strong refs to fire-and-forget silence-handler tasks; asyncio
        # only keeps weak references, so the GC could cancel them mid-run.
        self._bg_tasks: set[asyncio.Task] = set()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    def _send_event(self, event: stt.SpeechEvent) -> None:
        try:
            self._event_ch.send_nowait(event)
        except Exception:
            pass

    def _make_speech_data(self, text: str) -> stt.SpeechData:
        return stt.SpeechData(
            text=text,
            language=self._plugin._language,
            start_time=0,
            end_time=0,
        )

    def _on_interim(self, text: str) -> None:
        self._send_event(stt.SpeechEvent(
            type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
            alternatives=[self._make_speech_data(text)],
        ))

    def _on_final(self, text: str) -> None:
        self._last_final_text = text
        self._send_event(stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[self._make_speech_data(text)],
        ))

    def _on_eou(self, text: str) -> None:
        self._has_seen_eou = True
        self._last_final_text = None
        self._plugin.reset_silence_counter()
        self._send_event(stt.SpeechEvent(
            type=stt.SpeechEventType.END_OF_SPEECH,
            alternatives=[self._make_speech_data(text)],
        ))

    # ------------------------------------------------------------------
    # JSON event dispatch
    # ------------------------------------------------------------------

    def _handle_json(self, data: dict[str, Any]) -> None:
        msg_type = data.get("type")

        if msg_type == "interim":
            text = data.get("text", "")
            if text:
                self._on_interim(text)

        elif msg_type == "final":
            text = data.get("text", "")
            if text:
                self._on_final(text)

        elif msg_type == "eou":
            text = data.get("text", "")
            if text:
                # The service sends ``final`` then ``eou`` with the same
                # text; emit FINAL only if this transcript was not already
                # finalised (avoids a duplicate FINAL into AgentSession),
                # while still supporting backends that send ``eou`` alone.
                if text != self._last_final_text:
                    self._on_final(text)
                self._on_eou(text)
            else:
                # Empty EOU — Sber detected silence (no speech).
                # Don't emit END_OF_SPEECH (AgentSession ignores
                # empty EOS per gotcha #12) and don't set
                # _has_seen_eou — the gate would spin forever.
                # Instead, handle silence locally.
                self._plugin._silence_counter += 1
                handler = self._plugin._silence_timeout_handler
                if handler is not None:
                    self._spawn(handler(self._plugin._silence_counter))

        elif msg_type == "silence_timeout":
            count = data.get("count", 1)
            handler = self._plugin._silence_timeout_handler
            if handler is not None:
                self._spawn(handler(count))
            logger.info("stt silence timeout #%d (from ws)", count)

        elif msg_type == "error":
            logger.error("stt ws error: code=%s msg=%s", data.get("code"), data.get("message"))

        elif msg_type == "backend_info":
            logger.debug("stt ws backend: %s", data)

    @utils.log_exceptions(logger=logger)
    async def _run(self) -> None:
        client = self._plugin._get_client()

        # Connect once at stream start
        await client.connect()
        logger.info("stt _run: connected, starting drain/send tasks")

        # Audio queue + drain: reads _input_ch from AgentSession,
        # drops frames when the plugin is not listening (echo prevention).
        audio_q: asyncio.Queue = asyncio.Queue(maxsize=500)

        async def drain_input() -> None:
            """Read AgentSession audio input, queue or drop."""
            dropped = 0
            queued = 0
            _resumed = False
            logger.info("stt drain: task started")
            try:
                async for frame in self._input_ch:
                    if not self._plugin._listening_event.is_set():
                        dropped += 1
                        _resumed = False
                        if dropped == 1:
                            logger.info("stt drain: started dropping audio (listening_event not set)")
                        continue
                    if not _resumed and dropped > 0:
                        _resumed = True
                        logger.info("stt drain: listening_event set, resuming (was dropping %d frames)", dropped)
                    if audio_q.full():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            audio_q.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        audio_q.put_nowait(frame)
                    queued += 1
                    if queued == 1:
                        logger.info("stt drain: first frame queued")
            except Exception:
                logger.exception("stt drain_input error")
            logger.info("stt drain: task ended (queued=%d dropped=%d)", queued, dropped)

        drain_task = asyncio.create_task(drain_input(), name="stt_ws_drain")

        # Audio sender: moves frames from queue into WS.
        # Re-created after every WS reconnect.
        send_task: asyncio.Task | None = None

        async def send_audio() -> None:
            """Send queued audio frames into the WebSocket."""
            sent = 0
            logger.info("stt send_audio: task started")
            try:
                while True:
                    frame = await audio_q.get()
                    try:
                        await client.send_audio(frame.data.tobytes())
                        sent += 1
                        if sent == 1:
                            logger.info("stt send_audio: first audio frame sent")
                    except Exception as exc:
                        logger.warning("stt send_audio: send failed after %d frames: %s", sent, exc)
                        audio_q.put_nowait(frame)
                        break
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("stt send_audio error")
            logger.info("stt send_audio: task ended (sent=%d)", sent)

        def _ensure_send_task() -> None:
            nonlocal send_task
            if send_task is None or send_task.done():
                logger.info("stt _ensure_send_task: creating send_audio (was None=%s)", send_task is None)
                send_task = asyncio.create_task(send_audio(), name="stt_ws_send")

        try:
            while not self._input_ch.closed:
                # ----------------------------------------------------------
                # Turn-start gate
                # ----------------------------------------------------------
                if self._has_seen_eou:
                    # Wait for listening to be cleared (agent transitions to
                    # thinking/speaking). 10s timeout prevents a hang
                    # if EOU arrived without text and AgentSession did not process
                    # END_OF_SPEECH.
                    _deadline = asyncio.get_event_loop().time() + 10.0
                    while self._plugin._listening_event.is_set() and not self._input_ch.closed:
                        if asyncio.get_event_loop().time() > _deadline:
                            logger.warning(
                                "stt _run: turn-start gate timeout (10s), "
                                "forcing restart of STT stream"
                            )
                            break
                        await asyncio.sleep(0.02)

                    self._has_seen_eou = False

                    if self._input_ch.closed:
                        break

                await self._plugin._listening_event.wait()
                if self._input_ch.closed:
                    break

                # Flush stale audio from the previous turn
                flushed = 0
                while not audio_q.empty():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        audio_q.get_nowait()
                        flushed += 1
                if flushed:
                    logger.info("stt _run: flushed %d stale frames from audio_q", flushed)

                # Send "start" -- reconnect if WS dropped
                try:
                    await client.send_command({"type": "start"})
                    logger.info("stt _run: sent 'start' to STT service")
                except Exception:
                    logger.warning("stt _run: send start failed, reconnecting...")
                    await client.connect()
                    await client.send_command({"type": "start"})
                    logger.info("stt _run: reconnected and sent 'start'")

                _ensure_send_task()
                logger.info("stt _run: entering read loop (has_seen_eou=%s listening_event=%s)",
                            self._has_seen_eou, self._plugin._listening_event.is_set())

                # ----------------------------------------------------------
                # Read responses until EOU
                # ----------------------------------------------------------
                try:
                    while not self._input_ch.closed:
                        message = await client.read_message()
                        if isinstance(message, bytes):
                            continue
                        self._handle_json(message)

                        if message.get("type") == "eou":
                            logger.info("stt _run: received EOU from STT service")
                            if self._has_seen_eou:
                                # Normal EOU with text — end this turn
                                try:
                                    await client.send_command({"type": "stop"})
                                except Exception:
                                    pass
                                break
                            else:
                                # Empty EOU (silence) — restart session
                                try:
                                    await client.send_command({"type": "stop"})
                                except Exception:
                                    pass
                                # Wait a beat for server to clean up,
                                # then restart listening immediately
                                await asyncio.sleep(0.1)
                                try:
                                    await client.send_command({"type": "start"})
                                except Exception:
                                    pass

                except Exception as e:
                    if self._input_ch.closed:
                        break
                    logger.warning("stt ws read error: %s, reconnecting", e)
                    await client.connect()

        finally:
            drain_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await drain_task
            if send_task is not None:
                send_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await send_task
            for t in list(self._bg_tasks):
                t.cancel()
            if self._bg_tasks:
                await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            await client.aclose()
            logger.info("stt _run EXIT (input_ch closed)")


@dataclass
class SttPluginPack:
    """Container for the STT plugin and its underlying client.

    Attributes:
        plugin: ``SttWsPlugin`` instance -- passed to ``AgentSession(stt=...)``.
        client: Raw ``SttWsClient`` -- exposed for direct access if needed.
    """

    plugin: stt.STT
    client: SttWsClient


class SttWsPlugin(stt.STT):
    """LiveKit STT plugin that delegates to a remote service over WebSocket.

    The service owns all STT configuration (language, sample rate, gain,
    VAD). This plugin only manages the WebSocket transport and translates
    JSON events into LiveKit ``SpeechEvent`` objects.

    Required env: ``STT_SERVICE_URL``.
    """

    def __init__(
        self,
        *,
        language: str = "ru-RU",
    ) -> None:
        service_url = os.getenv("STT_SERVICE_URL", "").strip()
        if not service_url:
            raise RuntimeError(
                "STT_SERVICE_URL is required. "
                "Set STT_SERVICE_URL=http://127.0.0.1:8092 "
                "(or your STT service address)."
            )

        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
            ),
        )
        self._language = language
        self._listening_event = asyncio.Event()
        self._client = SttWsClient(service_url)
        self._silence_counter: int = 0
        self._silence_timeout_handler: Any = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_listening(self, listening: bool) -> None:
        """Enable/disable audio forwarding to the STT service.

        Called from ``agent_state_changed`` in the entrypoint. When
        ``listening=False``, audio is dropped (echo prevention).
        """
        if listening:
            self._listening_event.set()
        else:
            self._listening_event.clear()
        logger.info("stt plugin: listening=%s", listening)

    def reset_silence_counter(self) -> None:
        """Reset the consecutive-silence counter.

        Called from ``CallSession`` on a real EOU with text and on a
        spurious early stream close, so spurious events cannot accumulate
        toward ``STT_SILENCE_MAX_COUNT`` and hang up the call.
        """
        self._silence_counter = 0

    def set_silence_timeout_handler(self, handler: Any) -> None:
        """Register a callback for silence-timeout events.

        The handler is called with a single ``count`` (int) argument
        every time the STT service detects sustained silence.
        """
        self._silence_timeout_handler = handler

    def _get_client(self) -> SttWsClient:
        return self._client

    # ------------------------------------------------------------------
    # LiveKit STT interface
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def model(self) -> str:
        return "remote-stt-service"

    @property
    def provider(self) -> str:
        return "Remote STT (WebSocket)"

    async def _recognize_impl(
        self,
        _buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        raise NotImplementedError(
            "SttWsPlugin.recognize() is not supported. Use .stream() "
            "for WebSocket streaming recognition."
        )

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions | None = None,
    ) -> WsSttStream:
        if conn_options is None:
            conn_options = APIConnectOptions()
        return WsSttStream(
            stt_plugin=self,
            conn_options=conn_options,
            sample_rate=_SAMPLE_RATE,
        )


def create_stt_plugin(*, language: str = "ru-RU") -> SttPluginPack:
    """Build the STT pipeline: ``SttWsPlugin`` wrapping ``SttWsClient``.

    Reads ``STT_SERVICE_URL`` from the environment (required).
    """
    plugin = SttWsPlugin(language=language)
    logger.info(
        "stt plugin created: url=%s language=%s",
        os.getenv("STT_SERVICE_URL", ""), language,
    )
    return SttPluginPack(plugin=plugin, client=plugin._client)
