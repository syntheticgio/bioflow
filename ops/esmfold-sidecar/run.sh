#!/bin/bash
# Start the ESMFold sidecar on port 21235
# Usage: ./run.sh [port]
PORT=${1:-21235}
cd "$(dirname "$0")"
exec uvicorn main:app --host 0.0.0.0 --port "$PORT" --log-level info
