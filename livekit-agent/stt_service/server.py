"""
FastAPI application for the STT Service.

Combines auth (OAuth 2.0 token management) with Sber gRPC streaming STT
into a single microservice with WebSocket transport.

Endpoints
---------
- ``GET  /v1/health``       — readiness + token status
- ``POST /v1/token``        — get current token (auto-refresh if stale)
- ``POST /v1/token/refresh`` — force-refresh token from Sber
- ``WS   /ws``              — WebSocket STT streaming
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .config import SttServiceConfig
from .token_manager import TokenManager
from .sber_stt import SberSttSession, SttResult

logger = logging.getLogger("stt_service.server")

# ---------------------------------------------------------------------------
# Global state (initialised in lifespan)
# ---------------------------------------------------------------------------
cfg: SttServiceConfig | None = None
token_manager: TokenManager | None = None
_background_refresh_task: asyncio.Task | None = None


async def _background_refresh() -> None:
    """Background task: refresh the token every 5 minutes before expiry."""
    global token_manager
    while True:
        try:
            await asyncio.sleep(300)
            if token_manager:
                await token_manager.get_token(force=True)
                logger.info("background token refreshed")
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("background token refresh failed, will retry in 60s")
            await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global cfg, token_manager, _background_refresh_task
    cfg = SttServiceConfig.from_env()

    _log_level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(asctime)s | %(name)s | %(levelname)-8s | %(message)s",
        level=_log_level,
        force=True,
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token_manager = TokenManager(cfg)

    # Fetch first token immediately at startup
    try:
        await token_manager.get_token(force=True)
        logger.info("stt-service ready: port=%d", cfg.port)
    except Exception:
        logger.warning(
            "stt-service started but initial token fetch failed — "
            "will retry on first request"
        )

    _background_refresh_task = asyncio.create_task(
        _background_refresh(), name="stt_token_refresh",
    )

    yield

    if _background_refresh_task:
        _background_refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _background_refresh_task


app = FastAPI(
    title="STT Service",
    version="0.2.0",
    lifespan=_lifespan,
)


# ======================================================================
# REST models
# ======================================================================


class TokenResponse(BaseModel):
    access_token: str
    expires_in: int


class HealthResponse(BaseModel):
    ready: bool
    token_valid: bool
    expires_in: float


# ======================================================================
# REST endpoints (auth — backwards-compatible with auth_service)
# ======================================================================


@app.get("/v1/health")
async def health() -> HealthResponse:
    """Health-check endpoint."""
    if token_manager is None:
        raise HTTPException(503, "not initialized")
    return HealthResponse(
        ready=True,
        token_valid=True,
        expires_in=0,
    )


@app.post("/v1/token")
async def get_token() -> TokenResponse:
    """Return current token (auto-refresh if stale)."""
    if token_manager is None:
        raise HTTPException(503, "service not initialized")
    try:
        token = await token_manager.get_token(force=False)
        # We don't know the exact expires_in from cache, but the token is valid
        return TokenResponse(access_token=token, expires_in=900)
    except RuntimeError as e:
        raise HTTPException(502, detail=str(e))


@app.post("/v1/token/refresh")
async def refresh_token() -> TokenResponse:
    """Force-refresh token from Sber API."""
    if token_manager is None:
        raise HTTPException(503, "service not initialized")
    try:
        token = await token_manager.get_token(force=True)
        return TokenResponse(access_token=token, expires_in=900)
    except RuntimeError as e:
        raise HTTPException(502, detail=str(e))


# ======================================================================
# WebSocket STT endpoint
# ======================================================================


class WsSttConnection:
    """Manages a single WebSocket STT session.

    The agent sends binary PCM chunks (int16, 16kHz, mono).
    The server sends JSON event lines.
    """

    def __init__(self, ws: WebSocket, cfg: SttServiceConfig, tm: TokenManager) -> None:
        self._ws = ws
        self._cfg = cfg
        self._token_manager = tm
        self._listening_ev = asyncio.Event()
        # Audio receive queue (wait for the agent to say "listen")
        self._audio_q: asyncio.Queue = asyncio.Queue(maxsize=500)

    async def _send_json(self, data: dict) -> None:
        """Safely send JSON over WebSocket."""
        try:
            await self._ws.send_json(data)
        except Exception:
            pass

    async def _handle_user_message(self) -> None:
        """Receive messages from the agent: binary PCM or JSON command."""
        try:
            while True:
                msg = await self._ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg["type"] == "websocket.receive":
                    data = msg.get("bytes")
                    if data is not None:
                        # PCM chunk. Enqueue non-blocking with drop-oldest:
                        # when paused (listening cleared) the queue does not
                        # drain, and a blocking put would stall this same loop,
                        # preventing start/stop commands (deadlock).
                        if self._audio_q.full():
                            with contextlib.suppress(asyncio.QueueEmpty):
                                self._audio_q.get_nowait()
                        with contextlib.suppress(asyncio.QueueFull):
                            self._audio_q.put_nowait(data)
                        continue
                    text = msg.get("text")
                    if text is not None:
                        # JSON command
                        try:
                            cmd = json.loads(text)
                        except json.JSONDecodeError:
                            continue
                        cmd_type = cmd.get("type")
                        if cmd_type == "start":
                            self._listening_ev.set()
                            # Drain stale audio from the previous turn
                            # (silence/noise after EOU) so the new gRPC
                            # session only processes fresh speech.
                            while not self._audio_q.empty():
                                try:
                                    self._audio_q.get_nowait()
                                except asyncio.QueueEmpty:
                                    break
                            await self._send_json({"type": "started"})
                        elif cmd_type == "stop":
                            self._listening_ev.clear()
                            await self._send_json({"type": "stopped"})
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("ws user message handler error")
        finally:
            self._listening_ev.clear()

    async def _on_interim(self, text: str) -> None:
        await self._send_json({"type": "interim", "text": text, "confidence": 0.95})

    async def _on_final(self, result: SttResult) -> None:
        await self._send_json({
            "type": "final",
            "text": result.text,
        })
        await self._send_json({
            "type": "eou",
            "text": result.text,
        })

    async def run(self) -> None:
        """Main loop: receive audio → gRPC → send results."""
        logger.info("ws: client connected")

        # Task to receive messages from the agent
        recv_task = asyncio.create_task(self._handle_user_message())

        try:
            # Wait for the first start command
            self._listening_ev.clear()

            while True:
                # Wait for listening signal
                if not self._listening_ev.is_set():
                    try:
                        await asyncio.wait_for(
                            self._listening_ev.wait(), timeout=30,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("ws: no start signal for 30s, closing")
                        await self._send_json({"type": "close", "reason": "timeout"})
                        break
                    # Drain queue at start
                    while not self._audio_q.empty():
                        self._audio_q.get_nowait()

                # Create a gRPC session to Sber
                session = SberSttSession(
                    config=self._cfg,
                    token_manager=self._token_manager,
                    audio_q=self._audio_q,
                    on_interim=self._on_interim,
                    on_final=self._on_final,
                )

                try:
                    stop_reason = await session.run(self._listening_ev)
                except Exception as e:
                    logger.exception("sber gRPC session error")
                    await self._send_json({"type": "error", "code": "grpc_error", "message": str(e)})
                    break

                logger.info(
                    "ws: gRPC session done reason=%s voice=%d silence=%d resp=%d",
                    stop_reason, session.voice_chunks,
                    session.silence_chunks, session.responses_count,
                )

                # No-text guard: Sber returned an EOU with empty text (pure
                # silence, or the user spoke but the audio was too quiet to
                # transcribe). The empty ``eou`` was already sent to the agent
                # via ``_on_final``; the agent's plugin is the single source of
                # silence counting (it increments on every empty EOU — a
                # superset of this server's old ``voice_chunks==0`` case, so a
                # separate ``silence_timeout`` here was redundant double-
                # counting). We only clear the listening gate to stop an
                # infinite loop of empty gRPC sessions; the agent re-opens it
                # with ``start`` on its next listening turn.
                if self._listening_ev.is_set():
                    if session.responses_count > 0 and not session.had_text:
                        logger.info(
                            "ws: empty EOU (resp=%d voice=%d) — clearing listening gate",
                            session.responses_count, session.voice_chunks,
                        )
                        self._listening_ev.clear()

                    # Rate-limit reconnect
                    await asyncio.sleep(0.1)

        except Exception:
            logger.exception("ws: fatal error")
        finally:
            recv_task.cancel()
            with contextlib.suppress(BaseException):
                await recv_task
            logger.info("ws: client disconnected")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """WebSocket endpoint for STT streaming."""
    if cfg is None or token_manager is None:
        await ws.close(code=1011, reason="service not initialized")
        return

    await ws.accept()
    conn = WsSttConnection(ws, cfg, token_manager)
    await conn.run()
