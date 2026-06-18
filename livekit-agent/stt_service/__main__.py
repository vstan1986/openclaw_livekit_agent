"""
Entrypoint for the STT Service microservice.

Usage::

    python -m stt_service
    # or
    uvicorn stt_service.server:app --host 0.0.0.0 --port 8092
"""

from __future__ import annotations

import uvicorn

from .config import SttServiceConfig


def main() -> None:
    cfg = SttServiceConfig.from_env()
    log_level = cfg.log_level.lower()
    uvicorn.run(
        "stt_service.server:app",
        host=cfg.host,
        port=cfg.port,
        log_level=log_level,
    )


if __name__ == "__main__":
    main()
