"""
pytest configuration for livekit-agent tests.
"""

from __future__ import annotations

import os


# Several modules read configuration from the environment at import time
# (see my_agent.config). Tests that do not make real network calls just
# need any non-empty placeholder so the import succeeds.
_ENV_DEFAULTS = {
    "TTS_SERVICE_URL": "http://localhost:18090",
    "STT_SERVICE_URL": "http://localhost:18092",
    "LLM_BASE_URL": "http://localhost:11434/v1",
    "LIVEKIT_URL": "ws://localhost:7880",
    "LIVEKIT_API_KEY": "devkey",
    "LIVEKIT_API_SECRET": "devsecret",
}
for _key, _val in _ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _val)
