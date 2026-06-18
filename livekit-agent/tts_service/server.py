"""
FastAPI application for the TTS microservice.

Endpoints
---------
- ``POST /v1/tts``               — synthesise text → PCM bytes
- ``POST /v1/tts/select-phrase`` — random pre-synthesised phrase (no repeat)
- ``POST /v1/tts/phrase``        — specific named phrase by key
"""

from __future__ import annotations

import asyncio
import logging
import random
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .config import TtsServiceConfig, _NAMED_PHRASES
from .tts_engine import SileroEngine

logger = logging.getLogger("tts_service.server")

# ---------------------------------------------------------------------------
# Global state (initialised in lifespan)
# ---------------------------------------------------------------------------
cfg: TtsServiceConfig | None = None
engine: SileroEngine | None = None
_warmup_cache: dict[str, bytes] = {}
_warmup_list: list[str] = []
_phrase_cache: dict[str, bytes] = {}  # Named phrases (greeting, reminder, farewell)

# Single-worker executor: Silero/torch inference on one shared model object is
# NOT guaranteed thread-safe, and each call already uses SILERO_NUM_THREADS
# intra-op threads. Routing every synthesis through a 1-worker pool serialises
# calls (no concurrent apply_tts on the same model, no CPU oversubscription).
_synth_executor: ThreadPoolExecutor | None = None


def _get_engine() -> SileroEngine:
    """FastAPI dependency: return the engine or raise 503."""
    if engine is None:
        raise HTTPException(503, "engine not ready")
    return engine


async def _synthesize(eng: SileroEngine, text: str) -> bytes:
    """Run blocking synthesis on the dedicated single-worker executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_synth_executor, eng.synthesize, text)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global cfg, engine, _synth_executor
    cfg = TtsServiceConfig.from_env()

    # Dedicated single-worker pool — all synthesis is serialised through it.
    _synth_executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="tts-synth",
    )

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    _log_level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(asctime)s | %(name)s | %(levelname)-8s | %(message)s",
        level=_log_level,
        force=True,
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # ------------------------------------------------------------------
    # Load engine
    # ------------------------------------------------------------------
    engine = SileroEngine(
        model_id=cfg.model_id,
        language=cfg.language,
        speaker=cfg.speaker,
        sample_rate=cfg.sample_rate,
        device=cfg.device,
        num_threads=cfg.num_threads,
    )

    # ------------------------------------------------------------------
    # Warmup: pre-synthesise confirmation phrases
    # ------------------------------------------------------------------
    if cfg.warmup_phrases:
        for phrase in cfg.warmup_phrases:
            pcm = await _synthesize(engine, phrase)
            _warmup_cache[phrase] = pcm
            _warmup_list.append(phrase)
            logger.info(
                "warmup: %r → %.0f KB", phrase, len(pcm) / 1024,
            )

    # ------------------------------------------------------------------
    # Warmup: named phrases (greeting, reminder, farewell)
    # ------------------------------------------------------------------
    if _NAMED_PHRASES:
        for key, text in _NAMED_PHRASES.items():
            pcm = await _synthesize(engine, text)
            _phrase_cache[key] = pcm
            logger.info(
                "phrase %r: %r → %.0f KB", key, text, len(pcm) / 1024,
            )

    logger.info(
        "tts-service ready: model=%s port=%d warmup=%d",
        engine.model_name, cfg.port, len(_warmup_list),
    )
    try:
        yield
    finally:
        _synth_executor.shutdown(wait=True)


app = FastAPI(
    title="TTS Service",
    version="0.1.0",
    lifespan=_lifespan,
)


# ======================================================================
# Request models
# ======================================================================


class TtsRequest(BaseModel):
    text: str


class SelectPhraseRequest(BaseModel):
    last_phrase: str | None = None


class PhraseRequest(BaseModel):
    key: str


# ======================================================================
# Endpoints
# ======================================================================


@app.post("/v1/tts")
async def synthesize(
    req: TtsRequest,
    eng: SileroEngine = Depends(_get_engine),
) -> Response:
    """Synthesise a single text string into PCM int16 audio.

    Returns raw binary audio (``audio/pcm``, int16 LE). If the text was
    pre-synthesised during warmup the result is served from cache.
    """
    text = req.text.strip()
    if not text:
        raise HTTPException(422, "text is empty")

    # Cache hit?
    pcm = _warmup_cache.get(text)
    if pcm is None:
        pcm = await _synthesize(eng, text)

    return Response(
        content=pcm,
        media_type="audio/pcm",
        headers={
            "X-Sample-Rate": str(eng.sample_rate),
            "X-Channels": "1",
        },
    )


@app.post("/v1/tts/select-phrase")
async def select_phrase(
    req: SelectPhraseRequest,
    eng: SileroEngine = Depends(_get_engine),
) -> Response:
    """Return a random pre-synthesised phrase as PCM bytes (no repeat).

    The client sends ``last_phrase`` (the phrase it just played) and the
    server guarantees a different phrase.  Falls back to any phrase when
    *last_phrase* is the only one in cache.
    """
    if not _warmup_list:
        raise HTTPException(404, "no cached phrases — warmup is empty")

    candidates = [p for p in _warmup_list if p != req.last_phrase]
    if not candidates:
        candidates = _warmup_list

    phrase = random.choice(candidates)
    pcm = _warmup_cache[phrase]

    return Response(
        content=pcm,
        media_type="audio/pcm",
        headers={
            "X-Phrase": urllib.parse.quote(phrase, safe=""),
            "X-Sample-Rate": str(eng.sample_rate),
            "X-Channels": "1",
        },
    )


@app.post("/v1/tts/phrase")
async def get_phrase(
    req: PhraseRequest,
    eng: SileroEngine = Depends(_get_engine),
) -> Response:
    """Return a specific pre-synthesised phrase by key (greeting/reminder/farewell).

    Named phrases are synthesised at startup and stored separately from
    the random pool used by ``POST /v1/tts/select-phrase``.
    """
    pcm = _phrase_cache.get(req.key)
    if pcm is None:
        raise HTTPException(404, f"unknown phrase key: {req.key}")
    text = _NAMED_PHRASES.get(req.key, req.key)

    return Response(
        content=pcm,
        media_type="audio/pcm",
        headers={
            "X-Phrase": urllib.parse.quote(text, safe=""),
            "X-Sample-Rate": str(eng.sample_rate),
            "X-Channels": "1",
        },
    )
