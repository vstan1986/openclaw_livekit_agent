"""
TokenManager — OAuth 2.0 token management for Sber Voice (SaluteSpeech).

Manages fetching and caching of access tokens. Tokens are refreshed
automatically 5 minutes before expiry. Uses exponential backoff on 429.

Uses an RCE key (client_id:base64(client_id:secret)) for Basic auth.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import ssl
import time
import uuid
from pathlib import Path

import httpx

from .config import SttServiceConfig

logger = logging.getLogger("stt_service.token")

_CERT_PATH = Path(__file__).resolve().parent / "russian_trusted_root_ca_pem.crt"
_MAX_AUTH_RETRIES = 5
_AUTH_RETRY_BASE_DELAY = 1.0


def _ssl_context() -> ssl.SSLContext:
    cert_path = _CERT_PATH
    if cert_path.exists():
        return ssl.create_default_context(cafile=str(cert_path))
    return ssl.create_default_context()


def _rce_to_basic(api_key: str) -> str:
    """Convert an RCE key to a Basic base64 string."""
    parts = api_key.split(":", 1)
    if len(parts) != 2:
        raise ValueError("Invalid RCE key format: expected client_id:base64_secret")
    _client_id, b64_secret = parts
    try:
        raw = base64.b64decode(b64_secret).decode()
    except Exception as exc:
        raise ValueError(f"Failed to decode RCE key secret part: {exc}")
    return base64.b64encode(raw.encode()).decode()


class TokenManager:
    """Async OAuth token manager with caching and background refresh.

    Args:
        cfg: Service configuration (RCE key, auth URL, scope).
    """

    def __init__(self, cfg: SttServiceConfig) -> None:
        self._cfg = cfg
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self, force: bool = False) -> str:
        """Return current token, fetching if needed.

        Args:
            force: If True, force-refresh even if cached token is still valid.

        Returns:
            OAuth access_token string.
        """
        async with self._lock:
            now = time.time()
            if not force and self._token and now < self._token_expires_at - 300:
                remaining = int(self._token_expires_at - now)
                logger.debug("token cache hit, expires in %ds", remaining)
                return self._token

            logger.info("token fetch triggered (force=%s, had_cached=%s)", force, bool(self._token))
            token, expires_in = await self._fetch_token()
            self._token = token
            self._token_expires_at = now + expires_in - 300  # 5 min buffer
            logger.info("token cached, expires in %ds (at %.0f)", expires_in, self._token_expires_at)
            return self._token

    async def _fetch_token(self) -> tuple[str, int]:
        """Request a new token from Sber via Basic auth.

        Returns:
            (access_token, expires_in_seconds)
        """
        basic_value = _rce_to_basic(self._cfg.rce_key)
        async with httpx.AsyncClient(verify=_ssl_context(), timeout=30) as client:
            for attempt in range(1, _MAX_AUTH_RETRIES + 1):
                rq_uid = str(uuid.uuid4())
                headers = {
                    "Authorization": f"Basic {basic_value}",
                    "RqUID": rq_uid,
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                data = {"scope": self._cfg.scope}

                logger.debug(
                    "fetching sber token (rquid=%s, attempt=%d/%d)",
                    rq_uid, attempt, _MAX_AUTH_RETRIES,
                )

                try:
                    resp = await client.post(self._cfg.auth_url, headers=headers, data=data)
                except httpx.TimeoutException:
                    raise RuntimeError("auth request timed out")
                except httpx.RequestError as e:
                    raise RuntimeError(f"auth request failed: {e}")

                if resp.status_code == 429 and attempt < _MAX_AUTH_RETRIES:
                    delay = _AUTH_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        "sber token 429 (attempt %d/%d), retry in %.1fs",
                        attempt, _MAX_AUTH_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code != 200:
                    logger.error(
                        "sber token error: http=%d body=%s",
                        resp.status_code, resp.text[:500],
                    )
                    raise RuntimeError(
                        f"auth failed (http {resp.status_code}): {resp.text[:200]}"
                    )

                body = resp.json()
                token = body.get("access_token")
                expires_in = body.get("expires_in", 3600)

                token_preview = token[:12] + "..." if token else "<empty>"
                logger.info(
                    "sber token acquired: preview=%s expires_in=%ds",
                    token_preview, expires_in,
                )
                return token, expires_in

            raise RuntimeError(
                f"auth failed after {_MAX_AUTH_RETRIES} attempts (http 429)"
            )
