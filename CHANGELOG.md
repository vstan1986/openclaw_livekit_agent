# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] - 2026-06-18

### Added
- Initial public release
- LiveKit Agent with SIP inbound/outbound call support
- Sber SaluteSpeech STT via gRPC (Russian language, EOU detection)
- Silero TTS microservice (self-hosted, CPU)
- OpenAI-compatible LLM integration (Ollama, GPT, Claude, etc.)
- Confirmation phrase playback while LLM processes
- Turn-taking FSM with strict no-overlap semantics
- FastAPI HTTP API for outbound calls (`/call`, `/hangup`)
- Sentence splitter with aggressive split strategy for TTS
- Digit-to-words transforms for natural TTS output
- Resilience: retry, apology on error, silence timeout → hangup
- Docker Compose setup (livekit, redis, agent, tts-service, stt-service)
- Test suite: 52 tests
