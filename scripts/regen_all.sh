#!/bin/bash
# Regenerate all 5 topology JSONs in runs/. ticks/seed come from the YAML config.
set -euo pipefail
cd "$(dirname "$0")/../backend"
MODE="${1:-rule}"      # rule | claude
SEED="${2:-42}"
python3 run.py --all --mode "$MODE" --seed "$SEED"
echo ""
echo "=== runs/ ==="
ls -la ../runs/
