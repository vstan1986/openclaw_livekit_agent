"""Unit test for ``ConfirmationPlayer`` playback serialisation (#8).

Two concurrent playbacks (e.g. a confirmation and an apology) must not
publish overlapping "confirmation" tracks into the SIP mix at the same
time. The ``_play_lock`` must serialise them.

LiveKit ``rtc`` audio primitives need native bindings, so they are
replaced with lightweight fakes that track how many tracks are published
simultaneously.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("livekit")

from my_agent import plugin_tts  # noqa: E402
from my_agent.plugin_tts import AudioResult, ConfirmationPlayer  # noqa: E402


class _FakeParticipant:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def publish_track(self, track, opts):  # noqa: ANN001
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        return SimpleNamespace(sid="sid-x")

    async def unpublish_track(self, sid):  # noqa: ANN001
        self.active -= 1


class _FakeSource:
    instances: list["_FakeSource"] = []

    def __init__(self, sample_rate, num_channels):  # noqa: ANN001
        self.closed = False
        _FakeSource.instances.append(self)

    async def capture_frame(self, frame):  # noqa: ANN001
        await asyncio.sleep(0)

    async def wait_for_playout(self) -> None:
        await asyncio.sleep(0.02)  # hold the "track" open long enough to overlap

    async def aclose(self) -> None:
        self.closed = True


class _FakeLocalAudioTrack:
    @staticmethod
    def create_audio_track(name, source):  # noqa: ANN001
        return SimpleNamespace(name=name)


class _FakeHttpClient:
    async def synthesize(self, text: str) -> AudioResult:
        return AudioResult(pcm=b"\x00\x01" * 480, sample_rate=24000, num_channels=1)


@pytest.fixture(autouse=True)
def _patch_rtc(monkeypatch):
    monkeypatch.setattr(plugin_tts.rtc, "AudioSource", _FakeSource)
    monkeypatch.setattr(plugin_tts.rtc, "LocalAudioTrack", _FakeLocalAudioTrack)
    monkeypatch.setattr(
        plugin_tts.rtc,
        "AudioFrame",
        lambda **kw: SimpleNamespace(**kw),
    )


async def test_concurrent_playbacks_do_not_overlap() -> None:
    _FakeSource.instances.clear()
    participant = _FakeParticipant()
    room = SimpleNamespace(local_participant=participant)
    player = ConfirmationPlayer(room, _FakeHttpClient())

    await asyncio.gather(
        player.play_text("одну секунду"),
        player.play_text("извините"),
    )

    # With the lock, at most one track is ever published at a time.
    assert participant.max_active == 1


class _FailingParticipant:
    """publish_track always fails (e.g. room being torn down)."""

    async def publish_track(self, track, opts):  # noqa: ANN001
        raise RuntimeError("room closed")

    async def unpublish_track(self, sid):  # noqa: ANN001
        raise AssertionError("unpublish must not be called when publish failed")


async def test_source_closed_when_publish_fails() -> None:
    """The native AudioSource must be aclose()d even if publish_track raises."""
    _FakeSource.instances.clear()
    room = SimpleNamespace(local_participant=_FailingParticipant())
    player = ConfirmationPlayer(room, _FakeHttpClient())

    # play_text swallows the playback error internally — it must not raise.
    await player.play_text("одну секунду")

    assert len(_FakeSource.instances) == 1
    assert _FakeSource.instances[0].closed is True
