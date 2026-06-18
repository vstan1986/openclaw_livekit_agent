"""
stt-service — Speech-to-text microservice for LiveKit Agent.

Combines auth_service (OAuth 2.0 token management for Sber) with gRPC streaming
to Sber SaluteSpeech in a single service. The agent communicates via WebSocket.

    python -m stt_service
    # or
    uvicorn stt_service.server:app --host 0.0.0.0 --port 8092
"""
