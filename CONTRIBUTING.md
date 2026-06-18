# Contributing to LiveKit Voice Agent

Thank you for your interest! This project is small but useful — contributions
are welcome.

## How to Contribute

1. **Fork** the repository.
2. **Create a branch** for your change:
   ```bash
   git checkout -b feat/my-feature
   ```
3. **Make your changes.** Keep them focused — one change per PR.
4. **Test** that everything still works:
   ```bash
   cd livekit-agent
   pip install -e ".[dev]"
   python -m pytest tests/
   ```
5. **Open a Pull Request** with a clear description of what you changed and why.

## Guidelines

- **Keep it simple.** Minimal dependencies and simple code are preferred.
- **Don't break existing configs.** Backward compatibility is important.
- **Python 3.11** is the target runtime.
- **Update README.md** if you change configuration or add features.
- **52+ tests must pass** — add tests for new functionality.

## Reporting Issues

Open a GitHub issue with:
- Your `.env` configuration (redact secrets)
- Docker build / run logs
- What you expected vs what happened

Thank you! 🚀
