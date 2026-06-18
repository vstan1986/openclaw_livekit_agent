"""Unit tests for ``CallSession._on_error`` apology gating.

These guard against the regression where a recoverable LLM error set
``_llm_errored_this_turn`` before the recoverability check, swallowing the
apology on a genuine LLM failure and leaving the caller in total silence.

The session module imports the LiveKit Agents SDK at import time, so the
whole suite is skipped where that dependency is unavailable.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("livekit")

from my_agent.session import CallSession  # noqa: E402


class LLM:
    """Source stand-in.

    ``_on_error`` reads ``type(ev.source).__name__``. In production the
    source is the ``livekit.plugins.openai.LLM`` instance whose class is
    named ``LLM``, so the stand-in must share that exact name.
    """


class _Other:
    pass


class _LLMError(RuntimeError):
    """Mirror of ``llm.LLMError`` — carries ``recoverable`` like production."""

    def __init__(self, *, recoverable: bool) -> None:
        super().__init__("boom")
        self.recoverable = recoverable


class _ErrorEvent:
    """Mirror of ``ErrorEvent``: ``recoverable`` lives on ``error``, not here."""

    def __init__(self, *, source: object, recoverable: bool) -> None:
        self.error = _LLMError(recoverable=recoverable)
        self.source = source


class _FakeConfPlayer:
    def __init__(self) -> None:
        self.apologies = 0

    async def play_text(self, _text: str) -> None:
        self.apologies += 1


def _make_session(agent_state: str = "thinking") -> tuple[CallSession, _FakeConfPlayer]:
    cs = CallSession(ctx=object())
    player = _FakeConfPlayer()
    cs._conf_player = player
    cs._agent_state = agent_state
    return cs, player


async def _drain(cs: CallSession) -> None:
    if cs._bg_tasks:
        await asyncio.gather(*list(cs._bg_tasks), return_exceptions=True)


async def test_non_recoverable_error_plays_apology() -> None:
    cs, player = _make_session(agent_state="thinking")
    cs._on_error(_ErrorEvent(source=LLM(), recoverable=False))
    await _drain(cs)
    assert player.apologies == 1


async def test_recoverable_while_speaking_skips_apology() -> None:
    cs, player = _make_session(agent_state="speaking")
    cs._on_error(_ErrorEvent(source=LLM(), recoverable=True))
    await _drain(cs)
    assert player.apologies == 0
    # Not marked handled, so a later failure in the turn can still apologise.
    assert cs._llm_errored_this_turn is False


async def test_recoverable_without_audio_plays_apology() -> None:
    # recoverable=True but nothing is being spoken → caller is in silence.
    cs, player = _make_session(agent_state="thinking")
    cs._on_error(_ErrorEvent(source=LLM(), recoverable=True))
    await _drain(cs)
    assert player.apologies == 1


async def test_recoverable_then_non_recoverable_apologises_once() -> None:
    """The regression case: a recoverable error must not swallow the
    apology owed for a subsequent non-recoverable failure."""
    cs, player = _make_session(agent_state="speaking")
    # First, recoverable while speaking → skipped, flag NOT set.
    cs._on_error(_ErrorEvent(source=LLM(), recoverable=True))
    await _drain(cs)
    assert player.apologies == 0
    # TTS finished, agent now idle; retry exhausted → non-recoverable.
    cs._agent_state = "thinking"
    cs._on_error(_ErrorEvent(source=LLM(), recoverable=False))
    await _drain(cs)
    assert player.apologies == 1


async def test_only_one_apology_per_turn() -> None:
    cs, player = _make_session(agent_state="thinking")
    cs._on_error(_ErrorEvent(source=LLM(), recoverable=False))
    cs._on_error(_ErrorEvent(source=LLM(), recoverable=False))
    await _drain(cs)
    assert player.apologies == 1


async def test_non_llm_error_ignored() -> None:
    cs, player = _make_session(agent_state="thinking")
    cs._on_error(_ErrorEvent(source=_Other(), recoverable=False))
    await _drain(cs)
    assert player.apologies == 0
