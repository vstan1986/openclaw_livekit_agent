"""Unit tests for ``WsSttStream._handle_json`` (STT plugin event dispatch).

Guards the plugin↔service contract:
- the service sends ``final`` then ``eou`` with the same text — the plugin
  must emit exactly ONE ``FINAL_TRANSCRIPT`` plus one ``END_OF_SPEECH``
  (no duplicate FINAL into AgentSession);
- a backend that sends ``eou`` alone still gets a FINAL;
- an empty ``eou`` (silence) emits no transcript events and drives the
  local silence counter / handler exactly once.

The plugin imports the LiveKit Agents SDK at import time, so the suite is
skipped where that dependency is unavailable.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("livekit")

from livekit.agents import stt as lkstt  # noqa: E402

from my_agent.plugin_stt import SttWsPlugin  # noqa: E402

SpeechEventType = lkstt.SpeechEventType


def _make_stream():
    plugin = SttWsPlugin(language="ru-RU")
    return plugin, plugin.stream()


def _drain(stream) -> list:
    """Pull every queued SpeechEvent out of the stream's event channel."""
    events = []
    while True:
        try:
            events.append(stream._event_ch.recv_nowait())
        except Exception:
            break
    return events


def _types(events) -> list:
    return [e.type for e in events]


def _texts(events) -> list:
    return [e.alternatives[0].text for e in events]


async def test_final_then_eou_emits_single_final() -> None:
    _plugin, s = _make_stream()
    # Service sends ``final`` then ``eou`` with identical text.
    s._handle_json({"type": "final", "text": "привет"})
    s._handle_json({"type": "eou", "text": "привет"})

    events = _drain(s)
    assert _types(events) == [
        SpeechEventType.FINAL_TRANSCRIPT,
        SpeechEventType.END_OF_SPEECH,
    ]
    assert _texts(events) == ["привет", "привет"]
    assert s._has_seen_eou is True


async def test_eou_alone_still_emits_final() -> None:
    _plugin, s = _make_stream()
    # A backend that only sends ``eou`` (no preceding ``final``).
    s._handle_json({"type": "eou", "text": "да"})

    events = _drain(s)
    assert _types(events) == [
        SpeechEventType.FINAL_TRANSCRIPT,
        SpeechEventType.END_OF_SPEECH,
    ]


async def test_two_turns_each_emit_single_final() -> None:
    _plugin, s = _make_stream()
    s._handle_json({"type": "final", "text": "раз"})
    s._handle_json({"type": "eou", "text": "раз"})
    first = _drain(s)
    # Second turn with different text — dedup state must have reset.
    s._handle_json({"type": "final", "text": "два"})
    s._handle_json({"type": "eou", "text": "два"})
    second = _drain(s)

    assert _types(first) == [SpeechEventType.FINAL_TRANSCRIPT, SpeechEventType.END_OF_SPEECH]
    assert _types(second) == [SpeechEventType.FINAL_TRANSCRIPT, SpeechEventType.END_OF_SPEECH]
    assert _texts(second) == ["два", "два"]


async def test_interim_emits_interim_only() -> None:
    _plugin, s = _make_stream()
    s._handle_json({"type": "interim", "text": "при"})
    events = _drain(s)
    assert _types(events) == [SpeechEventType.INTERIM_TRANSCRIPT]
    assert _texts(events) == ["при"]


async def test_empty_eou_counts_silence_once_no_events() -> None:
    plugin, s = _make_stream()
    calls: list[int] = []

    async def handler(count: int) -> None:
        calls.append(count)

    plugin.set_silence_timeout_handler(handler)

    s._handle_json({"type": "eou", "text": ""})
    # The handler is fired as a fire-and-forget task; let it run.
    await asyncio.sleep(0)
    if s._bg_tasks:
        await asyncio.gather(*list(s._bg_tasks), return_exceptions=True)

    # No transcript events for an empty EOU.
    assert _drain(s) == []
    # Plugin is the single source of silence counting (gotcha #12/#20).
    assert plugin._silence_counter == 1
    assert calls == [1]
    # Empty EOU must NOT mark a real end-of-utterance.
    assert s._has_seen_eou is False


async def test_real_eou_resets_silence_counter() -> None:
    plugin, s = _make_stream()
    plugin._silence_counter = 3
    s._handle_json({"type": "final", "text": "стоп"})
    s._handle_json({"type": "eou", "text": "стоп"})
    assert plugin._silence_counter == 0
