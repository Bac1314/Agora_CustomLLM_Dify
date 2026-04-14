#!/usr/bin/env bash
# Local development runner.
# Usage: ./run.sh [--reload]
set -e

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Copy .env.example → .env and fill in your keys."
  exit 1
fi

# Load env (for local shell context; uvicorn also reads .env via pydantic-settings)
export $(grep -v '^#' .env | xargs)

PORT="${APP_PORT:-8000}"
HOST="${APP_HOST:-0.0.0.0}"

if [[ "$1" == "--reload" ]]; then
  echo "Starting with auto-reload on port $PORT..."
  uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
else
  echo "Starting on port $PORT..."
  uvicorn app.main:app --host "$HOST" --port "$PORT"
fi
