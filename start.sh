#!/bin/bash
# 启动 Muse x GPT 通信桥 (本地, 仅 127.0.0.1:8003).
# BRIDGE_TOKEN 必须由环境变量传入 (>=32 字符), 不写进文件.
# 用法 (两行, 不要写在一行里):
#   export BRIDGE_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
#   ./start.sh
# 依赖: python3 -m venv .venv && .venv/bin/pip install -r bridge/requirements.txt
#       (或直接用系统 Python 3.10+, 已装好依赖)
set -euo pipefail
cd "$(dirname "$0")"
: "${BRIDGE_TOKEN:?BRIDGE_TOKEN 未设置, 拒绝启动}"
export BRIDGE_DB="${BRIDGE_DB:-$PWD/data/bridge.sqlite}"
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python -m bridge.server
else
  exec python3 -m bridge.server
fi
