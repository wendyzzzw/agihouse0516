#!/bin/bash
# Fast rule-mode sanity: one topology, < 1 second.
set -euo pipefail
cd "$(dirname "$0")/../backend"
PY="python3"; [[ -x .venv/bin/python3 ]] && PY=".venv/bin/python3"
TOPO="${1:-small_world}"
"$PY" run.py --topology "$TOPO" --mode rule --seed "${2:-42}"
echo ""
"$PY" - <<EOF
import json
with open("../runs/${TOPO}.json") as f: d = json.load(f)
s = d["summary"]
print(f"  topology     : {d['topology']}")
print(f"  events       : {len(d['events'])}")
print(f"  prices points: {len(d['prices_over_time'])} (first={d['prices_over_time'][0]}, last={d['prices_over_time'][-1]})")
print(f"  bought       : {s['n_bought']}/15  missed: {s['n_missed']}")
print(f"  avg price    : \${s['avg_price']}")
print(f"  by profile   : {s['by_profile']}")
print(f"  total msgs   : {s['total_messages']}")
EOF
