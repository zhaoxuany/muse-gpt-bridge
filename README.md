# muse-gpt-bridge

> A small local bridge for multi-round discussions between an assistant and GPT over HTTP, with history in SQLite.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r bridge/requirements.txt
```

## Usage

```bash
export BRIDGE_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
./start.sh
curl http://127.0.0.1:8003/api/muse-gpt/health
```

The bridge binds only `127.0.0.1:8003`. All endpoints except health need `Authorization: Bearer <BRIDGE_TOKEN>`.

Adapters: `stub` (default, placeholder replies), `siwc` (ChatGPT Plus plan usage), `openai` (disabled in this build). History lives in `data/bridge.sqlite`.

## Tests

```bash
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest tests/ -q
```

## License

Apache-2.0. Copyright 2026 四季.
