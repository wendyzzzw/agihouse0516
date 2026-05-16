#!/bin/bash
# Validate that `claude -p` decision path actually works (1 agent, 1 call).
set -euo pipefail
cd "$(dirname "$0")/../backend"
python3 - <<'EOF'
from agent_runtime import decide_via_claude
import time, json, sys

agent = {
    "id": "A",
    "persona": {
        "profile": "budget",
        "description": "Frugal patient buyer who hates overpaying.",
        "traits": {"patience": 0.85, "social": 0.5, "honesty": 0.85, "risk_aversion": 0.7},
    },
    "budget": 320,
    "ticks_waited": 3,
    "beliefs": {"Airline_A": {"last_price": 360, "turn": 1}},
    "inbox": [{"sender": "C", "content": "I saw $280 on B yesterday", "turn": 2}],
    "bought": False,
}
wv = {
    "sellers": {
        "Airline_A": {"price": 360, "inventory": 6},
        "Airline_B": {"price": 290, "inventory": 4},
    },
    "neighbors": ["B", "C", "D"],
    "ticks_remaining": 50,
}
t0 = time.time()
result = decide_via_claude(agent, wv, model="haiku", timeout=60)
elapsed = time.time() - t0
print(f"elapsed: {elapsed:.1f}s")
print(json.dumps(result, indent=2))
if "fallback" in (result.get("reasoning") or ""):
    print("\nWARN: fell back to rule-based — claude -p may have failed.", file=sys.stderr)
    sys.exit(2)
EOF
