#!/bin/bash
# Validate the `claude -p` ReAct brain: one real step, schema-valid, terminates.
# Asserts STRUCTURE only (action is valid, no crash) — never a specific decision,
# since LLM output is non-deterministic.
set -euo pipefail
cd "$(dirname "$0")/../backend"
PY="python3"; [[ -x .venv/bin/python3 ]] && PY=".venv/bin/python3"
"$PY" - <<'EOF'
import time, json, sys
from brains import claude_brain

agent = {
    "id": "A",
    "persona": {
        "profile": "budget",
        "description": "Frugal patient buyer who hates overpaying.",
        "traits": {"patience": 0.85, "social": 0.5, "honesty": 0.85, "risk_aversion": 0.7},
    },
    "goal": "Secure one flight seat before the booking window closes, as cheaply as possible.",
    "goal_status": "pending",
    "budget": 320,
    "ticks_waited": 3,
    "beliefs": {},
    "bought": False,
}
world = {
    "sellers": {
        "Airline_A": {"price": 360, "inventory": 6},
        "Airline_B": {"price": 290, "inventory": 4},
    },
    "neighbors": ["B", "C", "D"],
    "rounds_remaining": 50,
}
history = [
    {"step": 1, "action": {"action": "COMMUNICATE", "target": "B"},
     "result": {"ok": True, "reply": "I'm seeing $290 on Airline_B."}},
]

t0 = time.time()
action = claude_brain(agent, world, history, model="haiku", timeout=60)
elapsed = time.time() - t0
print(f"elapsed: {elapsed:.1f}s")
print(json.dumps(action, indent=2))

valid = (isinstance(action, dict)
         and str(action.get("action", "")).upper() in {"BUY", "COMMUNICATE", "WAIT", "DONE"})
if not valid:
    print("\nFAIL: claude_brain returned a non-schema action.", file=sys.stderr)
    sys.exit(1)
print("\nOK: claude ReAct brain returned a schema-valid action.")
EOF
