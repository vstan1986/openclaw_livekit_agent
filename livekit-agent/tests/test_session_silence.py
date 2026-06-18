"""Unit tests for ``CallSession._on_silence_timeout``.

Guards against:
- re-entrancy: concurrent empty-EOU / silence_timeout events must not
  double-play phrases or race the listening-gate (#3);
- runaway early closes: a stream that keeps closing before the no-speech
  timeout must eventually escalate to hangup instead of looping forever (#4).

The session module imports the LiveKit Agents SDK at import time, so the
whole suite is skipped where that dependency is unavailable.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("livekit")

from my_agent import config  # noqa: E402
from my_agent.session import CallSession  # noqa: E402


class _FakePlugin:
    def __init__(self) -> None:
        self.listening: list[bool] = []
        self.reset_calls = 0

    def set_listening(self, value: bool) -> None:
        self.listening.append(value)

    def reset_silence_counter(self) -> None:
        self.reset_calls += 1


class _FakeConf:
    def __init__(self) -> None:
        self.phrases: list[str] = []

    async def play_phrase(self, key: str) -> None:
        self.phrases.append(key)
        await asyncio.sleep(0.01)  # let a concurrent caller observe the lock


def _make_session(*, early: bool) -> CallSession:
    cs = CallSession(ctx=object())
    cs._stt_pack = SimpleNamespace(plugin=_FakePlugin())
    cs._conf_player = _FakeConf()
    # early close → elapsed < STT_NO_SPEECH_TIMEOUT; real silence → elapsed huge
    cs._last_resume_time = time.monotonic() if early else time.monotonic() - 1000
    return cs


async def test_real_silence_below_max_plays_reminder() -> None:
    cs = _make_session(early=False)
    await cs._on_silence_timeout(1)
    assert cs._conf_player.phrases == ["reminder"]
    # listening toggled off during playback, back on afterwards
    assert cs._stt_pack.plugin.listening == [False, True]
    assert not cs.close_ev.is_set()


async def test_real_silence_at_max_plays_farewell_and_hangs_up() -> None:
    cs = _make_session(early=False)
    await cs._on_silence_timeout(config.STT_SILENCE_MAX_COUNT)
    assert cs._conf_player.phrases == ["farewell"]
    assert cs.close_ev.is_set()


async def test_early_close_below_cap_retries() -> None:
    cs = _make_session(early=True)
    await cs._on_silence_timeout(0)
    # No phrase played, counter reset, listening re-enabled, streak counted.
    assert cs._conf_player.phrases == []
    assert cs._stt_pack.plugin.reset_calls == 1
    assert cs._stt_pack.plugin.listening == [True]
    assert cs._early_close_count == 1
    assert not cs.close_ev.is_set()


async def test_early_close_reaching_cap_hangs_up() -> None:
    cs = _make_session(early=True)
    cs._early_close_count = config.STT_EARLY_CLOSE_MAX_COUNT - 1
    await cs._on_silence_timeout(0)
    # Crossing the cap escalates to farewell + hangup even though it is "early".
    assert cs._conf_player.phrases == ["farewell"]
    assert cs.close_ev.is_set()


async def test_real_silence_resets_early_close_streak() -> None:
    cs = _make_session(early=False)
    cs._early_close_count = 5
    await cs._on_silence_timeout(1)
    assert cs._early_close_count == 0


async def test_close_event_set_is_noop() -> None:
    cs = _make_session(early=False)
    cs.close_ev.set()
    await cs._on_silence_timeout(config.STT_SILENCE_MAX_COUNT)
    assert cs._conf_player.phrases == []


async def test_concurrent_events_play_once() -> None:
    cs = _make_session(early=False)
    # Two events fire "simultaneously"; the busy guard must drop the duplicate.
    await asyncio.gather(
        cs._on_silence_timeout(1),
        cs._on_silence_timeout(1),
    )
    assert cs._conf_player.phrases == ["reminder"]
