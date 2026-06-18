"""
Entrypoint for the TTS microservice.

Usage::

    python -m tts_service
    # or
    uvicorn tts_service.server:app --host 0.0.0.0 --port 8090
"""

from __future__ import annotations

import uvicorn

from .config import TtsServiceConfig


def main() -> None:
    cfg = TtsServiceConfig.from_env()
    log_level = cfg.log_level.lower()
    uvicorn.run(
        "tts_service.server:app",
        host=cfg.host,
        port=cfg.port,
        log_level=log_level,
    )


if __name__ == "__main__":
    main()
