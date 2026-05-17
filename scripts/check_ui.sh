#!/bin/bash
# Verify the live-viz frontend with a headless browser: starts the server, runs
# the Playwright DOM-liveness test (see check_ui.py).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../backend"
PY="python3"; [[ -x .venv/bin/python3 ]] && PY=".venv/bin/python3"
PORT="${1:-8081}"

"$PY" -m uvicorn app:app --port "$PORT" --log-level warning &>/tmp/agentarena_ui.log &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

up=0
for _ in $(seq 1 40); do
  if curl -s "localhost:$PORT/api/sim/topologies" >/dev/null 2>&1; then up=1; break; fi
  sleep 0.25
done
if [[ "$up" != "1" ]]; then
  echo "  FAIL  server did not start"; cat /tmp/agentarena_ui.log; exit 1
fi

"$PY" "$HERE/check_ui.py" "http://localhost:$PORT"
