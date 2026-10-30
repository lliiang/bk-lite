#!/bin/bash
set -e

WORKERS="${SANIC_WORKERS:-4}"
DEBUG="${SANIC_DEBUG:-false}"

if [ "$DEBUG" = "true" ]; then
    exec uv run sanic server:app --host=0.0.0.0 --port=8083 --workers="$WORKERS" --debug
else
    exec uv run sanic server:app --host=0.0.0.0 --port=8083 --workers="$WORKERS"
fi
