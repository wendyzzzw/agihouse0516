#!/bin/bash
# Sanity-check that runs/*.json conforms to what demo.html expects.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 - <<'EOF'
import json, os, sys
REQUIRED_KEYS = {"topology", "seed", "ticks", "summary", "prices_over_time", "events", "agents", "comm_matrix"}
EVENT_KEYS_BUY = {"from", "msg", "cls", "letter", "price", "sat", "turn"}
all_ok = True
for fn in sorted(os.listdir("runs")):
    if not fn.endswith(".json"): continue
    path = os.path.join("runs", fn)
    try:
        d = json.load(open(path))
    except Exception as e:
        print(f"  FAIL  {fn}: cannot parse: {e}"); all_ok = False; continue
    missing = REQUIRED_KEYS - set(d.keys())
    if missing:
        print(f"  FAIL  {fn}: missing keys {missing}"); all_ok = False; continue
    n_buy = sum(1 for e in d["events"] if e.get("cls") == "log-buy")
    if n_buy != d["summary"]["n_bought"]:
        print(f"  WARN  {fn}: buy-events ({n_buy}) ≠ summary.n_bought ({d['summary']['n_bought']})")
    for e in d["events"]:
        if e.get("cls") == "log-buy":
            miss = EVENT_KEYS_BUY - set(e.keys())
            if miss:
                print(f"  FAIL  {fn}: buy-event missing {miss}: {e}"); all_ok = False
                break
    print(f"  OK    {fn}  events={len(d['events']):>3}  prices={len(d['prices_over_time'])}  avg=${d['summary']['avg_price']}")
sys.exit(0 if all_ok else 1)
EOF
