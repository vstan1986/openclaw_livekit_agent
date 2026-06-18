"""
Tests for RemoteTTS — universal HTTP-client TTS plugin.

TTS_SERVICE_URL must be set (can point to a mock).
"""

from __future__ import annotations

import pytest

from my_agent.plugin_tts import RemoteTTS


def test_properties() -> None:
    tts = RemoteTTS()
    assert tts.provider == "Remote TTS (HTTP)"
    assert "remote-tts-service" in tts.model


async def test_synthesize_returns_chunked_stream() -> None:
    # Must run inside an event loop: ChunkedStream.__init__ spawns a metrics
    # task via asyncio.create_task(), exactly as it does in production where
    # synthesize() is always called from AgentSession's running loop.
    tts = RemoteTTS()
    result = tts.synthesize("привет")
    assert result is not None
    from livekit.agents import tts as tts_base
    assert isinstance(result, tts_base.ChunkedStream)
    await result.aclose()


def test_requires_tts_service_url(monkeypatch) -> None:
    monkeypatch.delenv("TTS_SERVICE_URL", raising=False)
    with pytest.raises(RuntimeError, match="TTS_SERVICE_URL"):
        RemoteTTS()
