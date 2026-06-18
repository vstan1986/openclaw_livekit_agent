<p align="center">
  <img src="https://img.shields.io/badge/python-3.11-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/Silero%20TTS-v1.0-green" alt="Silero TTS">
  <img src="https://img.shields.io/badge/LiveKit%20Agent-v1.6-purple?logo=livekit" alt="LiveKit">
  <img src="https://img.shields.io/badge/Sber%20STT-SaluteSpeech-blueviolet" alt="Sber STT">
  <img src="https://img.shields.io/badge/OpenClaw-plugin-orange" alt="OpenClaw Plugin">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

# LiveKit Voice Agent — Voice Channel for OpenClaw AI

**A voice AI assistant that answers calls over SIP and speaks back using Sber Voice (STT/TTS) and any OpenAI-compatible LLM. Works perfectly as a real-time voice channel for [OpenClaw](https://github.com/vstan1986/openclaw) AI agents via OpenClaw's OpenAI-compatible API.**

Callers speak to the assistant over a regular phone line (SIP). The assistant transcribes their speech with Sber STT, generates a response with any OpenAI-compatible LLM (including OpenClaw's built-in API), and speaks it back using Silero TTS — all in real time.

```
Caller ◄──SIP──► LiveKit SIP Trunk ◄──WebRTC──► Agent (LiveKit SDK)
                                                    │
                                           ┌────────┼────────┐
                                           ▼        ▼        ▼
                                        Sber STT  LLM   Silero TTS
                                                   │
                                          OpenClaw │ OpenAI-compatible
                                          AI Agent │ (Ollama, GPT, ...)
```

## ✨ Features

- **🤖 OpenClaw AI voice channel** — use any [OpenClaw](https://github.com/vstan1986/openclaw) agent as the LLM brain via its OpenAI-compatible API
- **📞 SIP telephony** — inbound + outbound calls via LiveKit SIP Trunk
- **🎙️ Sber SaluteSpeech STT** — gRPC streaming speech recognition (Russian language)
- **🗣️ Silero TTS** — self-hosted neural text-to-speech (HTTP microservice)
- **🧠 Any LLM** — OpenAI-compatible API (OpenClaw, Ollama, GPT, Claude, etc.)
- **🔇 No VAD needed** — server-side endpointing via Sber's EOU detection
- **🛡️ Confirmation phrases** — instant "one moment" playback while LLM thinks (no silence gaps)
- **🔌 Modular services** — STT, TTS, auth all run as separate microservices
- **🐳 Docker Compose** — single `up -d` to start everything
- **⚠️ Resilience** — configurable retry, apology playback on LLM errors, silence timeout → hangup

## 📦 Quick Start

```bash
# 1. Copy and fill in environment
cp .env.example .env

# 2. Start all services
docker compose up -d
```

### Required Environment Variables

| Variable | Description |
|----------|-------------|
| `LIVEKIT_URL` | `ws://<host>:7880` |
| `LIVEKIT_API_KEY` | From `livekit.yaml` |
| `LIVEKIT_API_SECRET` | From `livekit.yaml` |
| `EXTERNAL_IP` | Server public IP |
| `LLM_BASE_URL` | OpenAI-compatible LLM endpoint (e.g. `http://openclaw:8080/v1` for OpenClaw) |
| `LLM_API_KEY` | LLM API key (use `ollama` for Ollama, `openclaw` for OpenClaw) |
| `SBER_CLIENT_ID` | Sber RCE key Client ID |
| `SBER_CLIENT_SECRET` | Sber RCE key secret (base64) |
| `SIP_OUTBOUND_TRUNK_ID` | LiveKit outbound trunk ID (for outbound calls) |

See [`.env.example`](.env.example) for the full list.

## 🧠 How It Works

### Agent Architecture

The LiveKit agent runs in **two modes**, determined automatically from dispatch metadata:

| Mode | Trigger | Behaviour |
|------|---------|-----------|
| **inbound** | metadata has no `phone_number` | Agent waits for an incoming SIP call |
| **outbound** | metadata has `"phone_number": "+7..."` | Agent dials out and starts speaking |

### Call Flow

```
 ┌──────────┐     ┌──────────────┐     ┌───────────┐     ┌──────────┐     ┌───────────┐
 │  Caller  │ SIP │ LiveKit SIP  │ WS  │  Agent    │ gRPC │ Sber STT │     │   LLM     │
 │          │────►│   Trunk      │────►│(LiveKit   │─────►│(Salute-  │     │(OpenClaw /│
 │          │     │              │     │  SDK)     │      │ Speech)  │     │ Ollama /  │
 │          │◄────│              │◄────│           │◄─────│          │     │  GPT)     │
 │          │ SIP │              │ WS  │           │HTTP │          │     │           │
 └──────────┘     └──────────────┘     │           │     └──────────┘     └───────────┘
                                       │           │◄────── HTTP ─────────┐
                                       │           │                      │
                                       └───────────┘          ┌───────────┴───────────┐
                                                              │     Silero TTS        │
                                                              │    (tts-service)      │
                                                              └───────────────────────┘
```

1. **Call arrives** — SIP provider rings LiveKit SIP Trunk
2. **Agent joins** — LiveKit dispatches the call to the agent
3. **Listening** — agent opens a gRPC stream to Sber STT and listens for speech
4. **User speaks** — audio is streamed to Sber, which detects end-of-utterance (EOU)
5. **Confirmation** — agent instantly plays a short "one moment" phrase via Silero TTS
6. **LLM turn** — transcript is sent to the LLM (OpenClaw, Ollama, GPT, etc.); the response is streamed back
7. **Response spoken** — LLM text is synthesised by Silero TTS and played to the caller
8. **Loop** — agent returns to listening state for the next turn

### Turn-taking (Strict FSM)

Turn handling is strict — no overlap between user and agent speech:

- `allow_interruptions=False` — Sber transcripts received during agent TTS are ignored
- `discard_audio_if_uninterruptible=False` — no audio filtering, Sber decides
- Server-side endpointing via Sber's EOU signal, no client-side VAD

## 🗺️ Service Map

| Service | Container | Role |
|---------|-----------|------|
| **livekit** | `livekit` | WebRTC SFU (signalling + media), v1.12 |
| **redis** | `redis` | LiveKit coordination |
| **lk-tts** | `tts-service` | Silero TTS HTTP microservice |
| **lk-auth** | `auth-service` | Sber OAuth 2.0 token management |
| **lk-inbound** | `agent` | LiveKit Agent — SIP ↔ LLM orchestration |

## 🔧 LiveKit SIP Setup

### 1. Inbound trunk — receive calls from SIP provider

```bash
lk sip inbound create inbound-trunk.json
lk sip inbound list   # save trunk_id
```

### 2. Outbound trunk — outbound calls

```bash
MANGO_PASSWORD=$MANGO_PASSWORD lk sip outbound create outbound-trunk.json
lk sip outbound list   # save trunk_id → SIP_OUTBOUND_TRUNK_ID
```

### 3. Dispatch rule — route inbound call to agent

```bash
lk sip dispatch create dispatch-rule.json
```

### 4. Outbound call (via agent)

```bash
lk dispatch create \
  --new-room \
  --agent-name sber-voice-assistant \
  --metadata '{"phone_number": "+71234567890"}'
```

## 📊 Architecture

```
livekit-agent/
├── my_agent/                # LiveKit agent package
│   ├── session.py           # CallSession — turn orchestration
│   ├── plugin_stt.py        # WebSocket STT plugin (→ Sber)
│   ├── plugin_tts.py        # HTTP TTS plugin (→ Silero)
│   ├── plugin_tts_transforms.py  # Text transforms (digits → words)
│   ├── sentence_splitter.py # Aggressive sentence tokenizer for TTS
│   ├── http_api.py          # FastAPI (/call, /hangup)
│   └── config.py            # Centralised configuration
├── stt_service/             # STT microservice
│   ├── server.py            # HTTP/WebSocket entrypoint
│   ├── sber_stt.py          # gRPC streaming client (Sber v2)
│   └── token_manager.py     # Sber OAuth 2.0 token management
├── tts_service/             # TTS microservice
│   ├── server.py            # HTTP entrypoint
│   ├── tts_engine.py        # Silero TTS wrapper
│   └── translit.py          # Latin → Cyrillic transliteration
└── tests/                   # Pytest suite (52 tests)
```

## 🤝 Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md).

## 🔒 Security

See [SECURITY.md](SECURITY.md) for our security policy and vulnerability reporting process.

## 📄 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

### Third-Party Licenses

This project uses several open-source components with different licenses.
See [NOTICE.md](NOTICE.md) for full attribution and license information,
including:

- **Apache 2.0** — livekit-agents, livekit-plugins-openai, requests, grpcio, protobuf
- **LGPL** — num2words
- **CC BY-NC-SA 4.0** — Silero TTS model weights (non-commercial use only)
