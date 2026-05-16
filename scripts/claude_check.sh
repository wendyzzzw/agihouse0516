#!/bin/bash
# Validate that `claude -p` decision path actually works (1 agent, 1 call).
# Uses the new agent shape: archetype + role + goal.
set -euo pipefail
cd "$(dirname "$0")/../backend"
python3 - <<'EOF'
from agent_runtime import decide_via_claude
import time, json, sys

agent = {
    "id": "buyer_1",
    "letter": "A",
    "archetype": "patient_value_buyer",
    "persona": {
        "archetype": "patient_value_buyer",
        "role": "buyer",
        "description": "Waits for attractive prices and is happy to walk away. Frugal, hates overpaying. Long horizon.",
    },
    "budget": 320,
    "goal": {"type": "buy_if_below_price", "max_price_per_item": 280, "max_quantity": 1},
    "items_owned": 0,
    "target_items": 1,
    "ticks_waited": 3,
    "beliefs": {"Airline_A": {"last_price": 360, "turn": 1}},
    "inbox": [{"sender": "buyer_3", "content": "I saw $280 on Airline_B yesterday", "turn": 2}],
    "bought": False,
}
wv = {
    "sellers": {
        "Airline_A": {"price": 360, "inventory": 6},
        "Airline_B": {"price": 290, "inventory": 4},
    },
    "neighbors": ["buyer_2", "buyer_3", "buyer_4"],
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
