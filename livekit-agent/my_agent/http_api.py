"""
HTTP API for external call management (POST /call, POST /hangup).

Owns the global active-call registry (thread-safe).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from fastapi import FastAPI
from livekit import api

from my_agent import config

logger = logging.getLogger("agent.http_api")

http_app = FastAPI()

# ---------------------------------------------------------------------------
# Active calls registry (thread-safe)
# ---------------------------------------------------------------------------
# room_name → (close_event, event_loop)
_active_calls: dict[str, tuple[asyncio.Event, asyncio.AbstractEventLoop]] = {}
_lock = threading.Lock()


def register_call(
    room_name: str,
    close_ev: asyncio.Event,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Register an active call so ``POST /hangup`` can find it."""
    with _lock:
        _active_calls[room_name] = (close_ev, loop)
    logger.info("[registry] registered: room=%s", room_name)


def unregister_call(room_name: str) -> None:
    """Remove a call from the registry (usually after it ends)."""
    with _lock:
        _active_calls.pop(room_name, None)
    logger.info("[registry] unregistered: room=%s", room_name)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@http_app.post("/call")
async def make_call(data: dict):
    """Dispatch an outbound agent to call a phone number.

    Body: ``{"phone": "...", "instructions": "..."}``
    """
    phone = data.get("phone", "")
    if not phone:
        return {"ok": False, "error": "phone is required"}
    instructions = data.get("instructions", "")
    room_name = f"call-outbound-{phone}-{int(time.time())}"
    lk_api = api.LiveKitAPI(
        url=config.LIVEKIT_URL,
        api_key=config.LIVEKIT_API_KEY,
        api_secret=config.LIVEKIT_API_SECRET,
    )
    try:
        await lk_api.room.create_room(
            api.CreateRoomRequest(name=room_name),
        )
        metadata = {"phone_number": phone}
        if instructions:
            metadata["instructions"] = instructions
        await lk_api.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=config.AGENT_NAME,
                room=room_name,
                metadata=json.dumps(metadata),
            ),
        )
        return {"ok": True, "room": room_name}
    except Exception:
        logger.exception("[http] POST /call failed")
        return {"ok": False, "error": "internal error"}
    finally:
        await lk_api.aclose()


@http_app.post("/hangup")
async def hangup_call(data: dict):
    """Terminate a call by room name.

    Body: ``{"room": "call-outbound-79991234567-1234567890"}``
    """
    room_name = data.get("room", "")
    if not room_name:
        return {"ok": False, "error": "room is required"}

    with _lock:
        entry = _active_calls.get(room_name)

    if entry is None:
        logger.warning("[http] POST /hangup: room %s not found (already ended?)", room_name)
        return {"ok": False, "error": "room not found"}

    close_ev, loop = entry
    logger.info("[http] POST /hangup: signalling close for room %s", room_name)
    loop.call_soon_threadsafe(close_ev.set)
    return {"ok": True, "room": room_name}
