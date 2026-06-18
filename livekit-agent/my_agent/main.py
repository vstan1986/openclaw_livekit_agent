"""
LiveKit Agent entrypoint — thin orchestrator.

Responsibilities
---------------
- Configure logging.
- Start the HTTP API server (``/call``, ``/hangup``) in a background thread.
- Dispatch inbound / outbound jobs to ``CallSession``.
- Launch the LiveKit worker.

No business logic (prompts, audio processing, timeouts) lives here —
everything is delegated to ``CallSession``, ``config``, or ``http_api``.
"""

from __future__ import annotations

import json
import logging
import threading

from livekit import api
from livekit.agents import (
    JobContext,
    WorkerOptions,
    cli,
)

from my_agent import config
from my_agent.http_api import http_app
from my_agent.session import CallSession
from my_agent.utils.logger import setup_app_logger, silence_noisy_loggers

# ---------------------------------------------------------------------------
# Logging (must happen early to suppress noisy framework loggers)
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)-8s | %(message)s",
    level=logging.WARNING,
    force=True,
)
silence_noisy_loggers()
logger = setup_app_logger("agent")

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def entrypoint_inbound(ctx: JobContext, instructions: str = "") -> None:
    """Handle an inbound SIP → WebRTC call."""
    logger.info("[inbound] joining room: %s", ctx.room.name)
    session = CallSession(ctx, instructions=instructions)
    await session.run()


async def entrypoint_outbound(ctx: JobContext) -> None:
    """Handle an outbound WebRTC → SIP call (agent dials out)."""
    dial_info = json.loads(ctx.job.metadata or "{}")
    phone_number = dial_info.get("phone_number")
    instructions = dial_info.get("instructions", "")
    if not phone_number:
        logger.error("[outbound] phone_number not in metadata: %s", ctx.job.metadata)
        ctx.shutdown()
        return

    logger.info("[outbound] connecting to room: %s → calling %s", ctx.room.name, phone_number)
    await ctx.connect()

    try:
        await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=config.SIP_OUTBOUND_TRUNK_ID,
                sip_call_to=phone_number,
                participant_identity=phone_number,
                participant_name=phone_number,
                wait_until_answered=True,
            )
        )
    except api.TwirpError as e:
        logger.error(
            "[outbound] call failed: %s (SIP status: %s)",
            e.message,
            e.metadata.get("sip_status_code"),
        )
        ctx.shutdown()
        return

    try:
        participant = await ctx.wait_for_participant(identity=phone_number)
        logger.info("[outbound] %s answered the call", participant.identity)
    except TimeoutError:
        logger.error("[outbound] participant %s never joined", phone_number)
        ctx.shutdown()
        return

    session = CallSession(ctx, instructions=instructions)
    await session.run()


async def entrypoint(ctx: JobContext) -> None:
    """Top-level dispatch: decide inbound vs outbound from metadata."""
    metadata = ctx.job.metadata or ""
    try:
        dial_info = json.loads(metadata)
    except json.JSONDecodeError:
        dial_info = {}

    instructions = dial_info.get("instructions", "")

    if dial_info.get("phone_number"):
        await entrypoint_outbound(ctx)
    else:
        await entrypoint_inbound(ctx, instructions=instructions)


# ---------------------------------------------------------------------------
# HTTP server thread
# ---------------------------------------------------------------------------


def _start_http_server() -> None:
    """Run uvicorn (FastAPI /call, /hangup) in a separate thread."""
    import uvicorn

    config_ = uvicorn.Config(
        http_app,
        host="0.0.0.0",
        port=config.CALL_API_PORT,
        log_level="info",
    )
    server = uvicorn.Server(config_)
    server.run()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("HTTP API server starting on 127.0.0.1:%d", config.CALL_API_PORT)
    http_thread = threading.Thread(
        target=_start_http_server,
        name="http-api",
        daemon=True,
    )
    http_thread.start()

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=config.AGENT_NAME,
            port=config.AGENT_PORT,
        ),
    )
