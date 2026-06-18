"""
Sber SaluteSpeech gRPC streaming STT.

Isolated module: communicates with Sber via gRPC, receives PCM chunks
through asyncio.Queue, delivers results via callbacks.

No LiveKit Agents dependency — only grpcio and protobuf.
"""

from __future__ import annotations

import asyncio
import collections.abc
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import grpc
from google.protobuf import duration_pb2

from .config import SttServiceConfig
from .audio import apply_gain, peak, VOICE_THRESHOLD
from .token_manager import TokenManager
from .proto.recognition_pb2 import (
    RecognitionOptions,
    RecognitionRequest,
    RecognitionResponse,
    OptionalBool,
    NormalizationOptions,
    Hints,
)
from .proto.recognition_pb2_grpc import SmartSpeechStub

logger = logging.getLogger("stt_service.sber_stt")


def _duration(seconds: float) -> duration_pb2.Duration:
    """Duration with sub-second precision."""
    return duration_pb2.Duration(seconds=int(seconds), nanos=int((seconds % 1) * 1e9))


@dataclass
class SttResult:
    """Result of a single STT session (final/eou)."""
    text: str
    eou: bool
    eou_reason: str = ""


@dataclass
class SberSttSession:
    """A single gRPC recognition session.

    Receives PCM chunks (bytes) via ``audio_q``, invokes callbacks
    as results arrive from Sber.

    Args:
        config: Service configuration.
        token_manager: OAuth token manager.
        audio_q: Queue with PCM chunks (bytes). Can be fed externally.
        on_interim: Callback for interim results.
        on_final: Callback for final/eou results.
    """

    config: SttServiceConfig
    token_manager: TokenManager
    audio_q: asyncio.Queue = field(default_factory=asyncio.Queue)

    on_interim: collections.abc.Callable[[str], collections.abc.Awaitable[None]] | None = None
    on_final: collections.abc.Callable[[SttResult], collections.abc.Awaitable[None]] | None = None

    # Diagnostics (read after run())
    voice_chunks: int = 0
    silence_chunks: int = 0
    responses_count: int = 0
    had_text: bool = False  # True if any EOU had non-empty text

    _chunk_num: int = 0

    async def run(self, listening_ev: asyncio.Event) -> str:
        """Run a single gRPC stream to Sber.

        Args:
            listening_ev: Event controlling the pause. If cleared,
                          the stream is terminated (expected pause).

        Returns:
            stop_reason: reason for termination ("unknown", "listening cleared",
                         or gRPC error code).
        """
        self._chunk_num = 0
        self.voice_chunks = 0
        self.silence_chunks = 0
        self.responses_count = 0
        self.had_text = False

        token = await self.token_manager.get_token()
        call_cred = grpc.access_token_call_credentials(token)
        ssl_creds = _get_ssl_creds()
        channel_cred = grpc.composite_channel_credentials(ssl_creds, call_cred)

        channel_opts = [
            ('grpc.max_send_message_length', 1024 * 1024 * 100),
            ('grpc.max_receive_message_length', 1024 * 1024 * 100),
            ('grpc.keepalive_time_ms', 10000),
            ('grpc.keepalive_timeout_ms', 5000),
            ('grpc.keepalive_permit_without_calls', 1),
            ('grpc.http2.max_pings_without_data', 0),
        ]

        channel = grpc.aio.secure_channel(
            self.config.grpc_host, channel_cred, options=channel_opts,
        )

        stop_reason = "unknown"

        try:
            stub = SmartSpeechStub(channel)

            async def request_iter():
                nonlocal stop_reason

                opts = RecognitionOptions(
                    audio_encoding=RecognitionOptions.PCM_S16LE,
                    sample_rate=self.config.sample_rate,
                    channels_count=1,
                    language=self.config.language,
                    enable_multi_utterance=OptionalBool(enable=False),
                    enable_partial_results=OptionalBool(enable=True),
                    hypotheses_count=1,
                    no_speech_timeout=_duration(self.config.no_speech_timeout),
                    max_speech_timeout=_duration(self.config.max_speech_timeout),
                    hints=Hints(
                        enable_letters=False,
                        eou_timeout=_duration(self.config.eou_timeout),
                    ),
                    enable_vad=OptionalBool(enable=False),
                    normalization_options=NormalizationOptions(
                        enable=OptionalBool(enable=True),
                        punctuation=OptionalBool(enable=True),
                        capitalization=OptionalBool(enable=True),
                        question=OptionalBool(enable=True),
                        profanity_filter=OptionalBool(enable=True),
                    ),
                )
                yield RecognitionRequest(options=opts)

                while True:
                    if not listening_ev.is_set():
                        stop_reason = "listening cleared"
                        break

                    try:
                        data = await asyncio.wait_for(self.audio_q.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        continue

                    self._chunk_num += 1
                    raw = data
                    p = peak(raw)
                    chunk = apply_gain(raw, self.config.gain)

                    if p >= VOICE_THRESHOLD:
                        self.voice_chunks += 1
                    else:
                        self.silence_chunks += 1

                    if self._chunk_num == 1:
                        logger.info(
                            "stt FIRST input frame: %d bytes peak=%d",
                            len(raw), p,
                        )

                    yield RecognitionRequest(audio_chunk=chunk)

                logger.debug("stt request_iter ENDED (sent %d chunks)", self._chunk_num)

            stream_start_t = time.monotonic()
            async for response in stub.Recognize(request_iter()):
                self.responses_count += 1
                elapsed = time.monotonic() - stream_start_t
                if self.responses_count == 1:
                    logger.info("stt FIRST response after %.2fs", elapsed)

                if not response.HasField("transcription"):
                    continue

                tx = response.transcription
                eou_reason_map = {
                    0: "UNSPECIFIED", 1: "ORGANIC",
                    2: "NO_SPEECH_TIMEOUT", 3: "MAX_SPEECH_TIMEOUT",
                }
                eou_reason = eou_reason_map.get(tx.eou_reason, f"UNKNOWN({tx.eou_reason})")

                text = ""
                if tx.results:
                    hypothesis = tx.results[0]
                    text = hypothesis.normalized_text or hypothesis.text or ""

                if tx.eou:
                    logger.info("stt FINAL eou_reason=%s text=%r", eou_reason, text)
                    if text:
                        self.had_text = True
                    if self.on_final:
                        await self.on_final(SttResult(text=text, eou=True, eou_reason=eou_reason))
                elif text:
                    logger.info("stt interim text=%r", text)
                    if self.on_interim:
                        await self.on_interim(text)

        finally:
            await channel.close()

        return stop_reason


def _get_ssl_creds() -> grpc.ChannelCredentials:
    """Load SSL credentials with the Russian CA certificate."""
    cert_path = Path(__file__).resolve().parent / "russian_trusted_root_ca_pem.crt"
    if cert_path.exists():
        with open(cert_path, "rb") as f:
            ca_cert = f.read()
        return grpc.ssl_channel_credentials(root_certificates=ca_cert)
    return grpc.ssl_channel_credentials()

