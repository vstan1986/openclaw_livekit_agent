"""
SileroEngine — self-hosted Silero TTS wrapper (lazy-load, singleton-like).

This is a standalone copy extracted so the microservice does not depend
on the agent codebase.
"""

from __future__ import annotations

import logging
import re
import time
import warnings

logger = logging.getLogger("tts_service.engine")


def _sanitize_tts_text(text: str) -> str:
    """Prepare text for Silero v5_5_ru: transliterate Latin + strip digits.

    Args:
        text: Raw input (may contain Latin, Cyrillic, digits).

    Returns:
        Text safe to pass to Silero v5_5_ru.
    """
    if not text:
        return ""

    # 1. Latin → Cyrillic transliteration
    from tts_service.translit import latin_to_cyrillic

    text = latin_to_cyrillic(text)

    # 2. Strip everything except Cyrillic and basic punctuation (digits included)
    clean = re.sub(
        r"[^а-яА-ЯёЁ№\s\.\,\!\?\:\;\"\'\(\)\[\]«»\+\-\–\—…]",
        "", text,
    )
    # 3. Collapse whitespace
    clean = " ".join(clean.split())
    # 4. If only non-Cyrillic chars remain → empty result
    if clean and not re.search(r"[а-яА-ЯёЁ]", clean):
        logger.warning("sanitize: only non-cyrillic chars remain: %r", clean)
        return ""
    return clean


class SileroEngine:
    """Lazy-loaded Silero TTS engine.

    The underlying torch model is loaded on first ``synthesize()`` call,
    not at construction time.  This keeps startup fast and avoids pulling
    torch into memory if the engine is never used.
    """

    def __init__(
        self,
        *,
        model_id: str,
        language: str,
        speaker: str,
        sample_rate: int,
        device: str,
        num_threads: int,
    ) -> None:
        self._model_id = model_id
        self._language = language
        self._speaker = speaker
        self._sample_rate = sample_rate
        self._device = device
        self._num_threads = num_threads
        self._model = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        warnings.filterwarnings("ignore", ".*NNPACK.*")
        from silero_tts.silero_tts import SileroTTS as _SileroTTS

        logger.info(
            "loading silero model: %s speaker=%s device=%s sr=%d",
            self._model_id, self._speaker, self._device, self._sample_rate,
        )
        t0 = time.monotonic()
        self._model = _SileroTTS(
            model_id=self._model_id,
            language=self._language,
            speaker=self._speaker,
            sample_rate=self._sample_rate,
            device=self._device,
            num_threads=self._num_threads,
        )
        elapsed = time.monotonic() - t0
        logger.info("silero model loaded in %.2fs", elapsed)

    def synthesize(self, text: str) -> bytes:
        """Synthesise text into PCM int16 mono bytes.

        Args:
            text: Input text to synthesise.

        Returns:
            Raw PCM bytes (int16, mono, sample_rate Hz).
        """
        self._ensure_loaded()
        t0 = time.monotonic()

        # Sanitization: transliterate Latin to Cyrillic, strip digits
        safe_text = _sanitize_tts_text(text)
        if not safe_text:
            logger.warning("synthesize: empty after sanitization — returning silence")
            return b"\x00\x00" * int(self._sample_rate * 0.3)  # 300ms of silence

        import numpy as np

        try:
            audio = self._model.tts_model.apply_tts(
                text=safe_text,
                speaker=self._speaker,
                sample_rate=self._sample_rate,
                put_accent=True,
                put_yo=True,
            )
        except ValueError as exc:
            logger.warning(
                "synthesize: ValueError for %r — returning silence: %s",
                safe_text, exc,
            )
            return b"\x00\x00" * int(self._sample_rate * 0.3)

        elapsed = time.monotonic() - t0
        pcm: bytes = (audio.cpu().numpy() * 32767).astype(np.int16).tobytes()
        logger.debug(
            "synthesized %d chars → %d bytes in %.0fms",
            len(safe_text), len(pcm), elapsed * 1000,
        )
        return pcm

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def model_name(self) -> str:
        return f"silero-{self._model_id}-{self._speaker}"
