"""
LiveKit TTS plugin — universal HTTP bridge to a remote TTS service.

Contract
--------
- Talks to ``TTS_SERVICE_URL`` over HTTP. Endpoints used:
    - ``POST /v1/tts``               — synthesise arbitrary text → PCM
    - ``POST /v1/tts/select-phrase`` — random cached phrase (no repeat) → PCM
    - ``POST /v1/tts/phrase``        — specific named phrase by key → PCM
- Receives raw PCM int16 bytes + ``X-Sample-Rate`` / ``X-Channels`` headers.
- No knowledge of the TTS engine (Silero, Whisper, ElevenLabs, …).
- The service owns all audio configuration (voice, sample rate, device).

Contents
--------
- ``AudioResult`` — PCM bytes with metadata from the service.
- ``TtsHttpClient`` — HTTP client for the remote TTS service.
- ``RemoteTTS`` — LiveKit TTS plugin wrapping ``TtsHttpClient``.
- ``TtsPluginPack``, ``create_tts_plugin()``, ``aclose_tts()`` — factory for
  ``StreamAdapter`` with ``AggressiveSentenceTokenizer``.
- ``ConfirmationPlayer`` — standalone AudioTrack playback (bypass AgentSession).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from livekit import rtc
from livekit.agents import tts as agents_tts
from livekit.agents import APIConnectOptions
from livekit.rtc import TrackPublishOptions, TrackSource

from my_agent.sentence_splitter import AggressiveSentenceTokenizer

if TYPE_CHECKING:
    from livekit.agents.tts import AudioEmitter

logger = logging.getLogger("agent.tts_setup")

_TIMEOUT = httpx.Timeout(
    connect=5.0,   # localhost — fail fast if the service is down
    read=30.0,     # TTS synthesis of ~1000 chars on CPU
    write=5.0,     # tiny JSON body (tens of bytes)
    pool=5.0,      # single connection, no concurrent requests
)


@dataclass
class AudioResult:
    """PCM audio returned by the TTS service with metadata.

    Attributes:
        pcm: Raw PCM int16 bytes.
        sample_rate: Sample rate in Hz (from ``X-Sample-Rate`` header).
        num_channels: Number of channels (from ``X-Channels`` header).
    """
    pcm: bytes
    sample_rate: int
    num_channels: int = 1

    @classmethod
    def from_response(cls, resp: httpx.Response) -> AudioResult:
        """Build ``AudioResult`` from an HTTP response with PCM body + headers."""
        sr = int(resp.headers.get("X-Sample-Rate", "24000"))
        ch = int(resp.headers.get("X-Channels", "1"))
        return cls(pcm=resp.content, sample_rate=sr, num_channels=ch)


class TtsHttpClient:
    """Asynchronous HTTP client for the remote TTS service.

    The service owns all audio parameters (voice, sample rate, device).
    This client only sends text and receives PCM + metadata headers.

    Args:
        base_url: Root URL of the TTS service (e.g. ``http://127.0.0.1:8090``).
    """

    _MAX_RETRIES = 2
    _BACKOFF = [1.0, 2.0]  # one delay per retry (len == _MAX_RETRIES)

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=_TIMEOUT)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send an HTTP request with retry on transient errors.

        Retries on:
        - ``ConnectError`` (service unreachable)
        - ``TimeoutException`` (connect / read / write timeout)
        - ``HTTPStatusError`` with status >= 500 (server errors)

        4xx errors are **not** retried.
        """
        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(self._MAX_RETRIES + 1):
            try:
                resp = await self._client.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise  # 4xx — no retry
                last_exc = exc  # 5xx — retry
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc

            if attempt < self._MAX_RETRIES:
                delay = self._BACKOFF[attempt]
                logger.warning(
                    "tts request failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, self._MAX_RETRIES + 1, delay, last_exc,
                )
                await asyncio.sleep(delay)

        raise last_exc  # type: ignore[misc]

    async def synthesize(self, text: str) -> AudioResult:
        """Synthesise text via ``POST /v1/tts``.

        Returns ``AudioResult`` with PCM bytes and metadata from the
        ``X-Sample-Rate`` / ``X-Channels`` response headers.
        """
        resp = await self._request("POST", "/v1/tts", json={"text": text})
        return AudioResult.from_response(resp)

    async def select_phrase(
        self,
        last_phrase: str | None = None,
    ) -> tuple[str, AudioResult]:
        """Fetch a random pre-synthesised phrase from the TTS service.

        Returns ``(phrase_text, AudioResult)``.  The server guarantees no
        repeat of *last_phrase*.
        """
        resp = await self._request(
            "POST", "/v1/tts/select-phrase",
            json={"last_phrase": last_phrase},
        )
        phrase = urllib.parse.unquote(resp.headers.get("X-Phrase", ""))
        return phrase, AudioResult.from_response(resp)

    async def get_phrase(self, key: str) -> AudioResult:
        """Fetch a specific pre-synthesised phrase by key (greeting/reminder/farewell).

        Returns ``AudioResult`` with PCM bytes and metadata.
        """
        resp = await self._request("POST", "/v1/tts/phrase", json={"key": key})
        return AudioResult.from_response(resp)

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# RemoteTTS — LiveKit TTS plugin (universal HTTP client, no engine-specific code)
# ---------------------------------------------------------------------------


class _RemoteChunkedStream(agents_tts.ChunkedStream):
    """ChunkedStream for synthesising speech via the remote TTS service."""

    async def _run(self, emitter: AudioEmitter) -> None:
        plugin: RemoteTTS = self._tts  # type: ignore[assignment]
        text = self._input_text.strip()

        if not text:
            logger.warning("empty text, nothing to synthesize")
            return

        start_time = time.perf_counter()
        logger.info(
            "tts synthesizing: len=%d chars text=%s",
            len(text), text,
        )

        result = await plugin.http_client.synthesize(text)

        elapsed = (time.perf_counter() - start_time) * 1000
        logger.info(
            "tts done: %.0fms for %d chars (%.0f KB @ %d Hz)",
            elapsed, len(text), len(result.pcm) / 1024, result.sample_rate,
        )

        emitter.initialize(
            request_id=str(uuid.uuid4()),
            sample_rate=result.sample_rate,
            num_channels=result.num_channels,
            mime_type="audio/pcm",
        )
        emitter.push(result.pcm)
        emitter.flush()


class RemoteTTS(agents_tts.TTS):
    """LiveKit TTS plugin for a remote HTTP TTS service.

    All synthesis requests go to ``TTS_SERVICE_URL`` (``POST /v1/tts``).
    The service owns all audio configuration (voice, sample rate, device).

    Required env: ``TTS_SERVICE_URL``.
    """

    def __init__(self) -> None:
        tts_service_url = os.getenv("TTS_SERVICE_URL", "").strip()
        if not tts_service_url:
            raise RuntimeError(
                "TTS_SERVICE_URL is required. "
                "Set TTS_SERVICE_URL=http://127.0.0.1:8090 (or your TTS service address)."
            )

        # sample_rate is a placeholder — real values come from HTTP
        # response headers (X-Sample-Rate) in _RemoteChunkedStream.
        super().__init__(
            capabilities=agents_tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )
        self._http_client = TtsHttpClient(tts_service_url)

    @property
    def http_client(self) -> TtsHttpClient:
        """Public access to the underlying HTTP client."""
        return self._http_client

    async def aclose(self) -> None:
        logger.info("tts client closed")
        await self._http_client.aclose()

    @property
    def model(self) -> str:
        return "remote-tts-service"

    @property
    def provider(self) -> str:
        return "Remote TTS (HTTP)"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions | None = None,
    ) -> agents_tts.ChunkedStream:
        if conn_options is None:
            conn_options = APIConnectOptions()

        logger.debug("synthesizing (%d chars): %s...", len(text), text[:60])
        return _RemoteChunkedStream(
            tts=self,
            input_text=text,
            conn_options=conn_options,
        )


# ---------------------------------------------------------------------------
# Plugin factory (StreamAdapter + AggressiveSentenceTokenizer)
# ---------------------------------------------------------------------------

@dataclass
class TtsPluginPack:
    """Container for the TTS plugin and its underlying engine.

    Attributes:
        plugin: StreamAdapter-wrapped RemoteTTS — passed to ``Agent(tts=...)``.
        engine: Raw ``RemoteTTS`` instance — exposes ``http_client`` and
            ``sample_rate`` used by ``ConfirmationPlayer``.
    """

    plugin: agents_tts.TTS
    engine: RemoteTTS


def create_tts_plugin() -> TtsPluginPack:
    """Build the TTS pipeline: ``RemoteTTS`` > ``StreamAdapter``.

    The TTS service owns all audio configuration (voice, sample rate).
    The plugin simply wraps ``RemoteTTS`` in a ``StreamAdapter`` with
    an ``AggressiveSentenceTokenizer``.
    """
    engine = RemoteTTS()

    plugin: agents_tts.TTS = agents_tts.StreamAdapter(
        tts=engine,
        sentence_tokenizer=AggressiveSentenceTokenizer(
            min_sentence_len=15,
            stream_context_len=10,
        ),
    )

    logger.info("tts plugin created (engine=%s)", engine.model)

    return TtsPluginPack(plugin=plugin, engine=engine)


async def aclose_tts(pack: TtsPluginPack) -> None:
    """Close the TTS pipeline.

    ``StreamAdapter.aclose()`` already closes the wrapped ``RemoteTTS``,
    so we only close the plugin.
    """
    await pack.plugin.aclose()


# ---------------------------------------------------------------------------
# ConfirmationPlayer — standalone AudioTrack playback
# ---------------------------------------------------------------------------
# Bypasses the AgentSession speech pipeline because session.say() blocks the
# auto-generated LLM reply when allow_interruptions=False (see AGENTS.md #16).
# Instead, publishes a separate LocalAudioTrack directly into the Room.
#
# Sample rate and channel count are obtained from the HTTP response headers
# (X-Sample-Rate, X-Channels), not from local configuration.


class ConfirmationPlayer:
    """Play short phrases as a standalone AudioTrack (bypass AgentSession).

    ``play()``        — random cached phrase from the TTS service (no-repeat).
    ``play_text()``   — arbitrary text via ``POST /v1/tts`` + playback.
    """

    _FRAME_MS = 20  # AudioSource frame duration

    def __init__(
        self,
        room: rtc.Room,
        http_client: TtsHttpClient,
    ) -> None:
        self._room = room
        self._http_client = http_client
        self._last_phrase: str | None = None
        # Serialise playback so two phrases (e.g. a confirmation and an
        # apology) cannot publish overlapping "confirmation" tracks into
        # the SIP mix at the same time.
        self._play_lock = asyncio.Lock()

    async def play(self) -> None:
        """Play a random cached confirmation phrase (no repeat)."""
        try:
            phrase, result = await self._http_client.select_phrase(
                last_phrase=self._last_phrase,
            )
            self._last_phrase = phrase
            logger.debug("confirmation: selected %r (%.0f KB)", phrase, len(result.pcm) / 1024)
            await self._play_pcm(result)
        except Exception:
            logger.exception("confirmation play failed")

    async def play_phrase(self, key: str) -> None:
        """Play a specific pre-cached phrase by key (greeting/reminder/farewell).

        The PCM is already on the service side — zero synthesis latency.
        """
        logger.info("phrase: %s", key)
        try:
            result = await self._http_client.get_phrase(key)
            await self._play_pcm(result)
        except Exception:
            logger.exception("phrase playback failed: %s", key)

    async def play_text(self, text: str) -> None:
        """Synthesise arbitrary text and play it immediately."""
        logger.info("confirmation play_text: %r", text)
        try:
            result = await self._http_client.synthesize(text)
            await self._play_pcm(result)
        except Exception:
            logger.exception("confirmation play_text failed: %r", text)

    async def _play_pcm(self, result: AudioResult) -> None:
        """Low-level playback of PCM bytes via a standalone AudioTrack.

        Sample rate and channel count come from ``result`` (HTTP response
        headers), ensuring compatibility with any TTS engine.

        Serialised via ``_play_lock`` so concurrent callers play back-to-back
        instead of publishing overlapping tracks.
        """
        async with self._play_lock:
            spc = result.sample_rate * self._FRAME_MS // 1000
            frame_sz = spc * 2  # int16 → 2 bytes / sample
            pcm = result.pcm

            # The source is created BEFORE the try so that ``aclose()`` is
            # always reached in ``finally`` — even when ``publish_track``
            # raises (e.g. the room is being torn down). Otherwise the
            # native AudioSource would leak.
            source = rtc.AudioSource(
                sample_rate=result.sample_rate,
                num_channels=result.num_channels,
            )
            pub = None
            try:
                track = rtc.LocalAudioTrack.create_audio_track("confirmation", source)
                pub = await self._room.local_participant.publish_track(
                    track,
                    TrackPublishOptions(source=TrackSource.SOURCE_MICROPHONE),
                )

                logger.debug(
                    "confirmation track published: sid=%s len=%d KB @ %d Hz",
                    pub.sid, len(pcm) / 1024, result.sample_rate,
                )

                offset = 0
                while offset < len(pcm):
                    chunk = pcm[offset: offset + frame_sz]
                    if len(chunk) < frame_sz:
                        chunk = chunk + b"\x00" * (frame_sz - len(chunk))
                    frame = rtc.AudioFrame(
                        data=chunk,
                        sample_rate=result.sample_rate,
                        num_channels=result.num_channels,
                        samples_per_channel=spc,
                    )
                    await source.capture_frame(frame)
                    offset += frame_sz

                await source.wait_for_playout()
                logger.debug(
                    "confirmation playback done: sid=%s duration=%.0fms",
                    pub.sid, len(pcm) / (result.sample_rate * 2 / 1000),
                )
            except Exception:
                logger.exception("confirmation playback error")
            finally:
                if pub is not None:
                    try:
                        await self._room.local_participant.unpublish_track(pub.sid)
                        logger.debug("confirmation track unpublished: sid=%s", pub.sid)
                    except Exception:
                        logger.debug("confirmation track unpublish skipped (call ended)")
                await source.aclose()
