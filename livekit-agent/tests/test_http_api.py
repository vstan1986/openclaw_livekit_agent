"""Unit tests for the call-management HTTP API.

Guards against ``POST /call`` raising an unhandled ``KeyError`` (HTTP 500)
when the ``phone`` field is missing (#9).
"""

from __future__ import annotations

import pytest

pytest.importorskip("livekit")

from my_agent.http_api import make_call  # noqa: E402


async def test_call_without_phone_returns_error() -> None:
    result = await make_call({})
    assert result == {"ok": False, "error": "phone is required"}


async def test_call_with_empty_phone_returns_error() -> None:
    result = await make_call({"phone": ""})
    assert result == {"ok": False, "error": "phone is required"}
