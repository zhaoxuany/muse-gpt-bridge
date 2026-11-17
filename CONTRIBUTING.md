# Contributing

Thanks for considering a contribution to muse-gpt-bridge.

## Ground rules

- This project is a small local bridge. Keep changes small and focused.
- Never include credentials in code, tests, issues, or pull requests:
  no tokens, API keys, passwords, private keys, or personal data.
- Do not add new network destinations. The bridge only talks to the
  official OpenAI endpoints (`api.openai.com`, `auth.openai.com`) and
  `127.0.0.1`. Any new outbound host needs a separate discussion.
- Do not silently fall back to a different adapter or a placeholder
  reply when a real call fails. Failures must fail loudly.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r bridge/requirements.txt
.venv/bin/pip install -r requirements-test.txt
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

All tests use simulated streams — no real API calls, no cost. A change
that needs a real call must say so in the pull request and must not be
part of the default test run.

## Pull requests

- Describe what changed, why, and how it was tested.
- Update the README if behavior, configuration, or the file list changes.
- Keep the Apache-2.0 license header on new source files:

```
# Copyright 2026 四季
# Licensed under the Apache License, Version 2.0 (the "License");
```

## Reporting issues

Include the bridge version, Python version, operating system, the
command you ran, and the (redacted) error output. Never paste tokens
or credential file contents.
