# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it
privately by opening a **draft security advisory** on GitHub:

https://github.com/vstan1986/openclaw_livekit_agent/security/advisories/new

Do **not** open a public issue for security vulnerabilities.

## Best Practices for Users

- **Use environment variables for secrets** rather than storing them in code
  or committing `.env` files.
- **Keep your LiveKit API keys and Sber credentials secure** — never commit
  them to version control.
- **Run services behind a firewall** — restrict SIP and API access to trusted
  IP ranges only.
- **Use a dedicated LLM API key** with appropriate rate limits and usage
  monitoring.
