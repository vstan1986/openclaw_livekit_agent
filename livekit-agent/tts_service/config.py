"""
TtsServiceConfig — configuration for the TTS microservice.
All values come from environment variables with sensible defaults.

Warmup phrases (``_DEFAULT_WARMUP_PHRASES``) are always synthesised at
startup and served from cache by ``POST /v1/tts/select-phrase``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


# Warmup phrases synthesised at service startup.
# Used by ConfirmationPlayer and POST /v1/tts/select-phrase.
_DEFAULT_WARMUP_PHRASES = [
    "One moment",
    "Just a second",
    "Hang on",
    "Give me a moment",
    "Almost there",
]

# Named phrases for fixed scenarios (greeting, reminder, farewell).
# Synthesised at startup, NOT included in the select-phrase random pool.
_NAMED_PHRASES: dict[str, str] = {
    "greeting": "Hello! How can I help you?",
    "reminder": "Hello, I cannot hear you",
    "farewell": "Goodbye",
}


@dataclass
class TtsServiceConfig:
    port: int = 8090
    host: str = "0.0.0.0"
    model_id: str = "v5_5_ru"
    language: str = "ru"
    speaker: str = "eugene"
    sample_rate: int = 24000
    device: str = "cpu"
    num_threads: int = 4
    warmup_phrases: list[str] = field(default_factory=lambda: list(_DEFAULT_WARMUP_PHRASES))
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> TtsServiceConfig:
        return cls(
            port=int(os.getenv("TTS_SERVICE_PORT", "8090")),
            host=os.getenv("TTS_SERVICE_HOST", "0.0.0.0"),
            model_id=os.getenv("SILERO_MODEL_ID", "v5_5_ru"),
            language=os.getenv("SILERO_LANGUAGE", "ru"),
            speaker=os.getenv("SILERO_SPEAKER", "eugene"),
            sample_rate=int(os.getenv("SILERO_SAMPLE_RATE", "24000")),
            device=os.getenv("SILERO_DEVICE", "cpu"),
            num_threads=int(os.getenv("SILERO_NUM_THREADS", "4")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
