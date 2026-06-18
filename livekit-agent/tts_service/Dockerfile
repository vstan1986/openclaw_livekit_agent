# syntax=docker/dockerfile:1
# TTS Microservice — standalone container with Silero TTS + FastAPI.
#
# Build:
#   docker build -f livekit-agent/tts_service/Dockerfile -t tts-service livekit-agent/
#
# Usage:
#   docker run --rm -p 8090:8090 tts-service

FROM python:3.11-slim

WORKDIR /app

# System dependencies for torch/numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install torch with CPU support (no CUDA gigabytes)
RUN pip install --no-cache-dir \
    torch~=2.6.0 \
    torchaudio~=2.6.0 \
    --index-url https://download.pytorch.org/whl/cpu

# Remaining Python dependencies
COPY tts_service/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --no-deps silero-tts>=0.0.5

# Service code
COPY tts_service/ tts_service/

ENV OMP_NUM_THREADS=4 \
    MKL_NUM_THREADS=4 \
    MALLOC_TRIM_THRESHOLD_=131072 \
    PYTHONUNBUFFERED=1

EXPOSE 8090

CMD ["python", "-m", "tts_service"]
