"""
Configuration layer — all environment variables and static prompts.
No business logic, no LiveKit imports.
"""

from __future__ import annotations

import os

import httpx

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

LLM_BASE_URL: str = os.environ["LLM_BASE_URL"]
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
LLM_MODEL: str = os.getenv("LLM_MODEL", "llama3.1:8b")

# ⚠️ IMPORTANT: with livekit-plugins-openai 1.5.x the streaming request
# timeout is NOT taken from this httpx.Timeout. The plugin's stream lives in
# ``livekit.agents.inference.llm.LLMStream._run`` and overrides it per call:
#
#     stream = await client.chat.completions.create(
#         ..., timeout=httpx.Timeout(self._conn_options.timeout))
#
# i.e. the EFFECTIVE connect/read/write timeout for every LLM turn is
# ``llm_conn_options.timeout`` (see LLM_REQUEST_TIMEOUT below), whose LiveKit
# default is only 10s. That 10s is too short for Ollama's time-to-first-token
# on long contexts → APITimeoutError every turn. ``LLM_TIMEOUT`` here is kept
# for the client default / non-streaming paths and connection pooling.
LLM_CONNECT_TIMEOUT: float = float(os.getenv("LLM_CONNECT_TIMEOUT", "10"))
LLM_READ_TIMEOUT: float = float(os.getenv("LLM_READ_TIMEOUT", "120"))
LLM_TIMEOUT = httpx.Timeout(
    connect=LLM_CONNECT_TIMEOUT,
    read=LLM_READ_TIMEOUT,
    write=10.0,
    pool=5.0,
)

# LiveKit per-turn retry/limits for the LLM node (SessionConnectOptions).
#
# ``LLM_REQUEST_TIMEOUT`` is the EFFECTIVE per-attempt timeout (incl. the gap
# until the first streamed token). Raised well above the 10s LiveKit default
# so Ollama can answer on long contexts. A connection refusal still fails
# instantly — this ceiling only applies while waiting for tokens, so it does
# not slow down a healthy backend.
#
# ``LLM_MAX_RETRY`` → attempts per turn = LLM_MAX_RETRY + 1. Kept low so a
# stalled backend cannot stack several full timeouts back to back (worst case
# ≈ (LLM_MAX_RETRY + 1) × LLM_REQUEST_TIMEOUT before the turn gives up).
#
# ``LLM_MAX_UNRECOVERABLE_ERRORS`` → after this many *consecutive* failed
# turns AgentSession force-closes the whole call (CloseReason.ERROR). The
# counter only resets when the agent actually SPEAKS a real reply (our
# ConfirmationPlayer apology does not count), so a transient Ollama blip can
# otherwise drop a call before it recovers. Raised from the LiveKit default
# (3) to ride out short outages.
LLM_REQUEST_TIMEOUT: float = float(os.getenv("LLM_REQUEST_TIMEOUT", "60"))
LLM_MAX_RETRY: int = int(os.getenv("LLM_MAX_RETRY", "1"))
LLM_MAX_UNRECOVERABLE_ERRORS: int = int(os.getenv("LLM_MAX_UNRECOVERABLE_ERRORS", "6"))

# ---------------------------------------------------------------------------
# SIP
# ---------------------------------------------------------------------------

SIP_OUTBOUND_TRUNK_ID: str = os.getenv("SIP_OUTBOUND_TRUNK_ID", "")

# ---------------------------------------------------------------------------
# Agent / worker
# ---------------------------------------------------------------------------

AGENT_NAME: str = "sber-voice-assistant"
CALL_API_PORT: int = int(os.getenv("CALL_API_PORT", "8083"))
AGENT_PORT: int = int(os.getenv("AGENT_PORT", "8081"))

# ---------------------------------------------------------------------------
# STT silence handling
# ---------------------------------------------------------------------------

STT_SILENCE_MAX_COUNT: int = int(os.getenv("STT_SILENCE_MAX_COUNT", "3"))
STT_NO_SPEECH_TIMEOUT: float = float(os.getenv("STT_NO_SPEECH_TIMEOUT", "7.0"))

# An "early close" is a stream that closes before STT_NO_SPEECH_TIMEOUT — it is
# normally spurious (not real silence) and is retried without bumping the
# silence counter. But if Sber keeps closing the stream early forever, the call
# would hang with responses=0. After this many *consecutive* early closes the
# silence handler escalates to farewell + hangup instead of retrying forever.
STT_EARLY_CLOSE_MAX_COUNT: int = int(os.getenv("STT_EARLY_CLOSE_MAX_COUNT", "10"))

# ---------------------------------------------------------------------------
# LiveKit API (for HTTP API — /call, /hangup)
# ---------------------------------------------------------------------------

LIVEKIT_URL: str = os.environ["LIVEKIT_URL"]
LIVEKIT_API_KEY: str = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET: str = os.environ["LIVEKIT_API_SECRET"]

# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

TAP_AUDIO: bool = os.getenv("TAP_AUDIO", "0") not in ("", "0", "false", "False")

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT: str = (
    "You are a Russian-language voice assistant.\n"
    "Answer briefly and to the point — 1-2 sentences.\n"
    "Be friendly and natural in your speech.\n"
    "IMPORTANT: do not use English words or digits. "
    "Write numbers as words (e.g. «пятьсот рублей», «двадцатое мая»).\n"
    "IMPORTANT: if you need to call a function (search, weather lookup, etc.), "
    "first tell the user what you are doing — e.g. «Сейчас проверю...», "
    "«Одну секунду, ищу...», «Давай посмотрю...». "
    "Do not call functions silently — otherwise the user hears silence and thinks "
    "you have frozen."
)
