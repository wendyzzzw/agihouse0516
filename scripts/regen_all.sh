#!/bin/bash
# Regenerate all 5 topology JSONs in runs/  — commit these to GitHub for the static demo.
set -euo pipefail
cd "$(dirname "$0")/../backend"
PY="python3"; [[ -x .venv/bin/python3 ]] && PY=".venv/bin/python3"
MODE="${1:-rule}"      # rule | claude
SEED="${2:-42}"
TICKS="${3:-55}"
"$PY" run.py --all --mode "$MODE" --seed "$SEED" --ticks "$TICKS"
echo ""
echo "=== runs/ ==="
ls -la ../runs/
