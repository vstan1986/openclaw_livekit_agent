# syntax=docker/dockerfile:1
# STT Service — self-hosted Sber SaluteSpeech gRPC client + OAuth token management
# + WebSocket transport for LiveKit agent.
#
# Build:
#   docker build -f livekit-agent/stt_service/Dockerfile -t stt-service livekit-agent/
#
# Usage:
#   docker run --rm -p 8092:8092 \
#     -e SBER_CLIENT_ID=... \
#     -e SBER_CLIENT_SECRET=... \
#     stt-service

FROM python:3.11-slim

WORKDIR /app

# Python dependencies
COPY stt_service/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Service code (including russian_trusted_root_ca_pem.crt)
COPY stt_service/ stt_service/

ENV PYTHONUNBUFFERED=1

EXPOSE 8092

CMD ["python", "-m", "stt_service"]
