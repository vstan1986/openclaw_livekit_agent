# SIP Connector for WebRTC (LiveKit + Sber Voice)

Voice assistant for WebRTC using LiveKit SIP, Sber Voice (TTS/STT) and an LLM.

## Services

| Service | Purpose |
|---------|---------|
| **livekit** | WebRTC SFU (Signal), v1.12 |
| **sip** | SIP gateway (SIP ↔ WebRTC) |
| **redis** | LiveKit coordination |
| **lk-tts** | Silero TTS microservice (HTTP) |
| **lk-auth** | Sber Voice OAuth 2.0 token management (HTTP) |
| **lk-inbound** | LiveKit Agent — inbound (SIP → WebRTC) |

## Quick Start

```bash
cp .env.example .env   # fill in variables
docker compose up -d
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LIVEKIT_URL` | ✅ | ws://\<host\>:7880 |
| `LIVEKIT_API_KEY` | ✅ | From livekit.yaml |
| `LIVEKIT_API_SECRET` | ✅ | From livekit.yaml |
| `EXTERNAL_IP` | ✅ | Server external IP |
| `LLM_BASE_URL` | ✅ | LLM URL (OpenAI-compatible) |
| `LLM_API_KEY` | ❌ | LLM API key |
| `SBER_CLIENT_ID` | ✅ | Sber RCE key Client ID |
| `SBER_CLIENT_SECRET` | ✅ | Sber RCE key secret part (base64) |
| `SIP_OUTBOUND_TRUNK_ID` | ❌ | Outbound trunk ID (for outbound calls) |

---

## Agent architecture

The agent runs in **two modes**, determined automatically:

| Mode | Condition | Description |
|------|-----------|-------------|
| **inbound** | metadata is empty or has no `phone_number` | Agent waits for an incoming SIP call |
| **outbound** | metadata contains `"phone_number": "+7..."` | Agent dials out and speaks |

---

## LiveKit SIP Setup (CLI)

### 1. Inbound trunk — receive calls from SIP provider

```bash
lk sip inbound create inbound-trunk.json
lk sip inbound list   # save trunk_id
```

### 2. Outbound trunk — outbound calls

For Mango:
```bash
MANGO_PASSWORD=$MANGO_PASSWORD lk sip outbound create outbound-trunk.json
lk sip outbound list   # save trunk_id → SIP_OUTBOUND_TRUNK_ID
```

For B2BUA (if used):
```bash
lk sip outbound create outbound-trunk.json   # address: "${EXTERNAL_IP}:5062"
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

The agent will create a SIP participant using the configured `SIP_OUTBOUND_TRUNK_ID`.

### 5. Direct SIP call (without agent, for debugging)

```bash
lk sip participant create \
  --trunk <SIP_OUTBOUND_TRUNK_ID> \
  --room <ROOM_NAME> \
  --call +71234567890 \
  --identity sip-caller \
  --wait
```

Or via JSON file:
```bash
lk sip participant create participant.json
```

Where `participant.json`:
```json
{
  "sip_trunk_id": "<SIP_OUTBOUND_TRUNK_ID>",
  "sip_call_to": "+71234567890",
  "room_name": "<ROOM_NAME>",
  "participant_identity": "sip-caller",
  "wait_until_answered": true
}
```

### 6. Debugging

```bash
lk sip inbound list
lk sip outbound list
lk sip dispatch list
lk room list
lk dispatch list <room_name>
lk token create --join --room "test-room" --identity "debug-user" --open meet
```

## Troubleshooting

| Error | Cause | Solution |
|-------|-------|----------|
| `missing sip trunk id` | `SIP_OUTBOUND_TRUNK_ID` not set in agent container | `lk sip outbound list` → copy ID into `.env` |
| `SIP status: 403` | Invalid credentials or IP not allowed | Check login/password, whitelist IP |
| `SIP status: 486` | Callee busy | Try again later |
| `SIP status: 480` | Callee unavailable | Check the phone number |
| `no response from servers` | Agent not running or dispatch without `--agent-name` | Check `docker logs`, use `--agent-name sber-voice-assistant` |
| `not dispatching agent job since no worker is available` | `--agent-name` does not match agent name | Use `sber-voice-assistant` |
| `agent_name mismatch` | Agent name mismatch | `--agent-name` must match `agent_name` in `WorkerOptions` |
| `TypeError: object NoneType can't be used in 'await' expression` | `ctx.shutdown()` is not async | Remove `await` (fixed in code) |

## License

MIT
