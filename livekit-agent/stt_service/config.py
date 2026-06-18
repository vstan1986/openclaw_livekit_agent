"""
SttServiceConfig — configuration for the STT microservice.
All values come from environment variables.

Combines auth_service (OAuth) configuration with STT parameters
(gRPC host, gain, silence thresholds).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _resolve_rce_key() -> str:
    """Build RCE key from SBER_CLIENT_ID + SBER_CLIENT_SECRET.

    RCE key format: client_id:base64(client_id:actual_secret)

    The user provides:
      SBER_CLIENT_ID = "my-client"
      SBER_CLIENT_SECRET = base64(my-client:actual-secret)

    This function returns "my-client:base64(my-client:actual-secret)"
    so SberOAuthManager can decode and use it.
    """
    client_id = os.environ.get("SBER_CLIENT_ID", "")
    client_secret = os.environ.get("SBER_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Sber credentials required. Set SBER_CLIENT_ID and "
            "SBER_CLIENT_SECRET"
        )
    return f"{client_id}:{client_secret}"


@dataclass
class SttServiceConfig:
    # --- Server ---
    port: int = 8092
    host: str = "0.0.0.0"
    log_level: str = "INFO"

    # --- Sber OAuth ---
    rce_key: str = ""
    auth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    scope: str = "SALUTE_SPEECH_PERS"

    # --- Sber gRPC ---
    grpc_host: str = "smartspeech.sber.ru:443"

    # --- STT params ---
    language: str = "ru-RU"
    sample_rate: int = 16000
    no_speech_timeout: float = 7.0
    max_speech_timeout: float = 20.0
    eou_timeout: float = 1.0

    # --- Audio pre-processing ---
    gain: float = 15.0

    @classmethod
    def from_env(cls) -> SttServiceConfig:
        return cls(
            port=int(os.getenv("STT_SERVICE_PORT", "8092")),
            host=os.getenv("STT_SERVICE_HOST", "0.0.0.0"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            rce_key=_resolve_rce_key(),
            auth_url=os.getenv(
                "SBER_AUTH_URL",
                "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            ),
            scope=os.getenv("SBER_SCOPE_TYPE", "SALUTE_SPEECH_PERS"),
            grpc_host=os.getenv("SBER_GRPC_HOST", "smartspeech.sber.ru:443"),
            language=os.getenv("STT_LANGUAGE", "ru-RU"),
            sample_rate=int(os.getenv("STT_SAMPLE_RATE", "16000")),
            no_speech_timeout=float(os.getenv("STT_NO_SPEECH_TIMEOUT", "7.0")),
            max_speech_timeout=float(os.getenv("STT_MAX_SPEECH_TIMEOUT", "20.0")),
            eou_timeout=float(os.getenv("STT_EOU_TIMEOUT", "1.0")),
            gain=float(os.getenv("STT_GAIN", "15.0")),
        )
