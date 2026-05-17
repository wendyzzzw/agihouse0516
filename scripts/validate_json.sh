#!/bin/bash
# Sanity-check runs/*.json: legacy schema (demo.html contract) + new ReAct schema.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 - <<'EOF'
import json, os, sys

REQUIRED_KEYS = {"topology", "seed", "ticks", "summary", "prices_over_time",
                 "events", "agents", "comm_matrix"}
EVENT_KEYS_BUY = {"from", "msg", "cls", "letter", "price", "sat", "turn"}
# Lifecycle event kinds the round-based engine must emit (frontend live viz).
REQUIRED_KINDS = {"round_start", "agent_activated", "agent_thinking", "goal_reached"}

all_ok = True
for fn in sorted(os.listdir("runs")):
    if not fn.endswith(".json"):
        continue
    path = os.path.join("runs", fn)
    try:
        d = json.load(open(path))
    except Exception as e:
        print(f"  FAIL  {fn}: cannot parse: {e}"); all_ok = False; continue

    missing = REQUIRED_KEYS - set(d.keys())
    if missing:
        print(f"  FAIL  {fn}: missing keys {missing}"); all_ok = False; continue

    events = d["events"]

    # --- legacy: buy events keep the demo.html shape ---
    n_buy = sum(1 for e in events if e.get("cls") == "log-buy")
    if n_buy != d["summary"]["n_bought"]:
        print(f"  WARN  {fn}: buy-events ({n_buy}) != summary.n_bought ({d['summary']['n_bought']})")
    for e in events:
        if e.get("cls") == "log-buy":
            miss = EVENT_KEYS_BUY - set(e.keys())
            if miss:
                print(f"  FAIL  {fn}: buy-event missing {miss}: {e}"); all_ok = False
                break

    # --- new: every event carries a `kind`; lifecycle kinds are present ---
    if any("kind" not in e for e in events):
        print(f"  FAIL  {fn}: some events have no `kind` field"); all_ok = False; continue
    kinds = {e["kind"] for e in events}
    missing_kinds = REQUIRED_KINDS - kinds
    if missing_kinds:
        print(f"  FAIL  {fn}: missing lifecycle event kinds {missing_kinds}")
        all_ok = False; continue

    # --- new: goal-verification integrity ---
    # every goal_reached event must name an agent that ends 'verified' AND bought.
    agents = d["agents"]
    bad = []
    for e in events:
        if e["kind"] == "goal_reached":
            a = agents.get(e.get("from"))
            if not a or a.get("goal_status") != "verified" or not a.get("bought"):
                bad.append(e.get("from"))
    if bad:
        print(f"  FAIL  {fn}: goal_reached for un-verified/un-bought agents {bad}")
        all_ok = False; continue
    # 'verified' must imply 'bought' — the engine never verifies a false DONE.
    liars = [aid for aid, a in agents.items()
             if a.get("goal_status") == "verified" and not a.get("bought")]
    if liars:
        print(f"  FAIL  {fn}: agents verified without buying {liars}"); all_ok = False; continue

    n_verified = sum(1 for a in agents.values() if a.get("goal_status") == "verified")
    print(f"  OK    {fn}  events={len(events):>4}  prices={len(d['prices_over_time'])}  "
          f"avg=${d['summary']['avg_price']}  verified={n_verified}/{len(agents)}")

sys.exit(0 if all_ok else 1)
EOF
