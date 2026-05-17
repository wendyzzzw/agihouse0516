#!/bin/bash
# Verify the config layer:
#   1. every configs/*.yaml loads + validates
#   2. each preset's comm matrix round-trips to the legacy topology generator
#   3. the `explicit` edge-list mode produces the matrix you'd expect
#   4. malformed configs are rejected (negative test)
set -euo pipefail
cd "$(dirname "$0")/../backend"
python3 - <<'EOF'
import os, sys, tempfile
from config import load_scenario, load_named, build_comm_matrix, ConfigError, CONFIGS_DIR
from topology import generate_graph, graph_to_matrix, TOPOLOGIES

ok = True

# --- 1 + 2: every preset loads and round-trips to the legacy generator ---
for fn in sorted(os.listdir(CONFIGS_DIR)):
    if not fn.endswith((".yaml", ".yml")):
        continue
    path = os.path.join(CONFIGS_DIR, fn)
    try:
        sc = load_scenario(path)
    except ConfigError as e:
        print(f"  FAIL  {fn}: {e}"); ok = False; continue

    matrix = build_comm_matrix(sc)
    if sc.buyer_buyer in TOPOLOGIES:
        legacy = graph_to_matrix(generate_graph(sc.buyer_buyer, sc.buyer_ids, sc.seller_ids, sc.seed))
        if matrix != legacy:
            print(f"  FAIL  {fn}: matrix does not match legacy generator '{sc.buyer_buyer}'")
            ok = False; continue
        note = f"round-trips '{sc.buyer_buyer}'"
    else:
        note = f"explicit, {len(sc.explicit_edges)} edges"
    print(f"  OK    {fn:>22}  agents={len(sc.agents):>2}  sellers={len(sc.sellers)}  {note}")

# --- 3: explicit mode produces exactly the requested buyer-buyer edges ---
explicit_yaml = """
scenario: {name: t, seed: 1, rounds: 3}
market: {sellers: [{id: S1, inventory: 2, base_price: 100}]}
topology: {buyer_buyer: explicit, edges: [[A, B], [B, C]]}
agents:
  defaults: {goal: "buy", tools: [buy]}
  roster: [{persona: budget, count: 3}]
"""
with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
    f.write(explicit_yaml); tmp = f.name
sc = load_scenario(tmp); m = build_comm_matrix(sc)
expect = {("A", "B"), ("B", "C")}
got = {(a, b) for a in "ABC" for b in "ABC" if a < b and m[a][b]}
if got == expect:
    print(f"  OK    {'explicit-mode':>22}  buyer edges = {sorted(got)}")
else:
    print(f"  FAIL  explicit-mode: expected {expect}, got {got}"); ok = False
os.unlink(tmp)

# --- 4: malformed configs must raise ConfigError ---
bad_cases = {
    "unknown persona": """
scenario: {name: t}
market: {sellers: [{id: S1, inventory: 2, base_price: 100}]}
topology: {buyer_buyer: isolated}
agents: {defaults: {goal: g, tools: []}, roster: [{persona: wizard, count: 1}]}
""",
    "edge to missing agent": """
scenario: {name: t}
market: {sellers: [{id: S1, inventory: 2, base_price: 100}]}
topology: {buyer_buyer: explicit, edges: [[A, Z]]}
agents: {defaults: {goal: g, tools: []}, roster: [{persona: budget, count: 2}]}
""",
    "no sellers": """
scenario: {name: t}
market: {sellers: []}
topology: {buyer_buyer: isolated}
agents: {defaults: {goal: g, tools: []}, roster: [{persona: budget, count: 1}]}
""",
}
for label, body in bad_cases.items():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(body); tmp = f.name
    try:
        load_scenario(tmp)
        print(f"  FAIL  negative '{label}': should have raised ConfigError"); ok = False
    except ConfigError:
        print(f"  OK    {'reject: ' + label:>22}  raised ConfigError as expected")
    finally:
        os.unlink(tmp)

print()
print("config layer: ALL OK" if ok else "config layer: FAILURES ABOVE")
sys.exit(0 if ok else 1)
EOF
