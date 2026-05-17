#!/bin/bash
# Verify the SSE stream is genuinely LIVE — events must arrive spread over time,
# not in one burst. Starts the server, replays a cached run round-paced, and
# timestamps every frame as it arrives (see stream_analyze.py).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../backend"
PY="python3"; [[ -x .venv/bin/python3 ]] && PY=".venv/bin/python3"
PORT="${1:-8077}"
DELAY="${2:-0.1}"

"$PY" -m uvicorn app:app --port "$PORT" --log-level warning &>/tmp/agentarena_srv.log &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

up=0
for _ in $(seq 1 40); do
  if curl -s "localhost:$PORT/api/sim/topologies" >/dev/null 2>&1; then up=1; break; fi
  sleep 0.25
done
if [[ "$up" != "1" ]]; then
  echo "  FAIL  server did not start"; cat /tmp/agentarena_srv.log; exit 1
fi

# curl streams the SSE; stream_analyze.py reads it from stdin (its own file, so
# the script source does NOT collide with the piped stdin).
curl -sN "localhost:$PORT/api/sim/replay/small_world?delay=$DELAY" \
  | "$PY" "$HERE/stream_analyze.py" "$DELAY"
