#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export APP_ENV="${APP_ENV:-production}"
export APP_HOST="${APP_HOST:-0.0.0.0}"
export APP_PORT="${APP_PORT:-8000}"
export UVICORN_WORKERS="${UVICORN_WORKERS:-1}"
export ALLOW_LEGACY_LOCAL_WORKSPACE="${ALLOW_LEGACY_LOCAL_WORKSPACE:-0}"

cd "$ROOT_DIR/packages/agent/src"
exec python main.py
