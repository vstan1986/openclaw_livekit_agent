"""Unit tests for the STT-service WebSocket connection (``WsSttConnection``).

Guards:
- the audio receive loop never blocks on a full queue (drop-oldest), so
  control commands (``start``/``stop``) are always processed even when the
  agent floods audio during a pause — otherwise the loop deadlocks;
- ``start`` flushes stale audio and opens the listening gate.

The service imports FastAPI / grpc proto stubs at import time, so the suite
is skipped where those are unavailable.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("grpc")

from stt_service.server import WsSttConnection  # noqa: E402


class _FakeWebSocket:
    """Minimal ASGI-style WebSocket double for ``_handle_user_message``."""

    def __init__(self, incoming: list[dict]) -> None:
        self._incoming = list(incoming)
        self.sent: list[dict] = []

    async def receive(self) -> dict:
        if self._incoming:
            return self._incoming.pop(0)
        # Nothing left → emulate the peer closing the socket.
        return {"type": "websocket.disconnect"}

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


def _bin(n: int) -> dict:
    return {"type": "websocket.receive", "bytes": b"\x00\x01" * n}


def _cmd(obj: dict) -> dict:
    return {"type": "websocket.receive", "text": json.dumps(obj)}


def _make_conn(incoming: list[dict]) -> WsSttConnection:
    ws = _FakeWebSocket(incoming)
    # cfg / token_manager are unused by _handle_user_message.
    return WsSttConnection(ws, cfg=object(), tm=object())


async def test_audio_flood_does_not_block_commands() -> None:
    # 600 audio frames (> queue maxsize 500) arrive BEFORE any start, while
    # the gate is closed and nothing drains the queue. A blocking put would
    # wedge the loop here and never reach the start command.
    incoming = [_bin(8) for _ in range(600)]
    incoming.append(_cmd({"type": "start"}))
    conn = _make_conn(incoming)

    await asyncio.wait_for(conn._handle_user_message(), timeout=5.0)

    # The start command was processed despite the flood (no deadlock).
    types_sent = [m.get("type") for m in conn._ws.sent]
    assert "started" in types_sent
    # start() drains the queue, so it ends empty.
    assert conn._audio_q.qsize() == 0


async def test_queue_is_bounded_under_flood() -> None:
    # Flood with no start at all: the queue must stay bounded (drop-oldest),
    # never exceeding its declared maxsize.
    incoming = [_bin(8) for _ in range(1200)]
    conn = _make_conn(incoming)

    await asyncio.wait_for(conn._handle_user_message(), timeout=5.0)

    assert conn._audio_q.qsize() <= 500


async def test_start_then_stop_toggle_gate() -> None:
    incoming = [
        _cmd({"type": "start"}),
        _cmd({"type": "stop"}),
    ]
    conn = _make_conn(incoming)

    await asyncio.wait_for(conn._handle_user_message(), timeout=5.0)

    types_sent = [m.get("type") for m in conn._ws.sent]
    assert types_sent == ["started", "stopped"]
    # The handler clears the gate on exit.
    assert not conn._listening_ev.is_set()


async def test_start_flushes_stale_audio() -> None:
    # Stale audio queued before start must be dropped so the next gRPC
    # session only sees fresh speech.
    incoming = [_bin(8) for _ in range(10)]
    incoming.append(_cmd({"type": "start"}))
    conn = _make_conn(incoming)

    await asyncio.wait_for(conn._handle_user_message(), timeout=5.0)

    assert conn._audio_q.qsize() == 0
