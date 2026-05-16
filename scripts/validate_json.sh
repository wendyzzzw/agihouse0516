#!/bin/bash
# Sanity-check that runs/*.yaml conforms to what demo.html expects.
# (Kept the original filename for backwards compat with test.sh; content is YAML now.)
set -euo pipefail
cd "$(dirname "$0")/.."
python3 - <<'EOF'
import yaml, os, sys
REQUIRED_KEYS = {"topology", "seed", "ticks", "summary", "prices_over_time", "events", "agents", "comm_matrix"}
EVENT_KEYS_BUY = {"from", "msg", "cls", "letter", "price", "sat", "turn"}
all_ok = True
for fn in sorted(os.listdir("runs")):
    if not (fn.endswith(".yaml") or fn.endswith(".yml")): continue
    path = os.path.join("runs", fn)
    try:
        d = yaml.safe_load(open(path))
    except Exception as e:
        print(f"  FAIL  {fn}: cannot parse: {e}"); all_ok = False; continue
    missing = REQUIRED_KEYS - set(d.keys())
    if missing:
        print(f"  FAIL  {fn}: missing keys {missing}"); all_ok = False; continue
    n_buy = sum(1 for e in d["events"] if e.get("cls") == "log-buy")
    if n_buy != d["summary"]["n_bought"]:
        print(f"  WARN  {fn}: buy-events ({n_buy}) != summary.n_bought ({d['summary']['n_bought']})")
    for e in d["events"]:
        if e.get("cls") == "log-buy":
            miss = EVENT_KEYS_BUY - set(e.keys())
            if miss:
                print(f"  FAIL  {fn}: buy-event missing {miss}: {e}"); all_ok = False
                break
    tokens_present = "tokens" in next(iter(d["agents"].values()))
    print(f"  OK    {fn}  events={len(d['events']):>3}  prices={len(d['prices_over_time'])}  avg=${d['summary']['avg_price']}  tokens_field={tokens_present}")
sys.exit(0 if all_ok else 1)
EOF
