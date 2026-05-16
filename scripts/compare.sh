#!/bin/bash
# Run all 5 topologies with N seeds each, print spread table.
set -euo pipefail
cd "$(dirname "$0")/../backend"
SEEDS="${1:-10}"
python3 - <<EOF
from app import compare
import json
r = compare(seeds=${SEEDS}, ticks=55)
print(f"=== Topology comparison ({${SEEDS}} seeds each) ===")
print(f"{'topology':>16}  {'mean_price':>12}  {'sat':>6}  {'missed':>7}  {'overpay':>10}")
for t, v in r.items():
    op = v.get('overpay_pct')
    op_str = f"+{op}%" if op else "best"
    print(f"{t:>16}  \${v['mean_price']:>10}  {v['mean_satisfaction']:>5.1f}%  {v['mean_missed']:>6.1f}  {op_str:>10}")
EOF
