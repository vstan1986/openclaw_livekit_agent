"""
Isolated logger setup — prevents LiveKit JSON-handler duplication.
"""

from __future__ import annotations

import logging

from my_agent.config import LOG_LEVEL

_LOG_FORMAT = "%(asctime)s | %(name)s | %(levelname)-8s | %(message)s"


def setup_app_logger(name: str) -> logging.Logger:
    """Return a logger with a local StreamHandler and propagate=False.

    LiveKit Agents hooks a StructuredFormatter on the root logger.
    Without this isolation every message would be duplicated.
    """
    lg = logging.getLogger(name)
    lg.setLevel(getattr(logging, LOG_LEVEL))
    lg.propagate = False
    if not lg.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        lg.addHandler(handler)
    return lg


def silence_noisy_loggers() -> None:
    """Suppress verbose framework loggers (LiveKit, HTTP clients)."""
    for name in ("livekit", "livekit.agents", "livekit.plugins", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.ERROR)
