#!/usr/bin/env sh
# 本地一键启动
set -e
cd "$(dirname "$0")"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
echo "SimpleURL 启动中 → http://${HOST}:${PORT}"
exec python -m uvicorn app.main:app --host "$HOST" --port "$PORT" "${@}"
