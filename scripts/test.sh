#!/bin/bash
# Full smoke test: unit tests + regen + validate + compare + (optional) claude check.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$HERE/../backend"
PY="python3"
[[ -x "$BACKEND/.venv/bin/python3" ]] && PY="$BACKEND/.venv/bin/python3"

echo "=== [1/6] config layer ==="
"$HERE/validate_config.sh"

echo ""
echo "=== [2/6] mailbox unit tests ==="
( cd "$BACKEND" && "$PY" test_mailbox.py )

echo ""
echo "=== [3/6] ReAct loop unit tests ==="
( cd "$BACKEND" && "$PY" test_react.py )

echo ""
echo "=== [4/6] regen all 5 topology JSONs (rule mode) ==="
"$HERE/regen_all.sh" rule

echo ""
echo "=== [5/6] validate JSON schema ==="
"$HERE/validate_json.sh"

echo ""
echo "=== [6/6] compare across topologies (5 seeds) ==="
"$HERE/compare.sh" 5

echo ""
echo "=== claude -p sanity (optional, 1 call ~25s) ==="
if [[ "${SKIP_CLAUDE:-}" == "1" ]]; then
  echo "  (skipped, SKIP_CLAUDE=1)"
else
  "$HERE/claude_check.sh" || echo "  (claude check failed or fell back)"
fi
echo ""
echo "=== all smoke tests done ==="
