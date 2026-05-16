#!/bin/bash
# Full smoke test: quick run + validate JSON + compare + (optional) claude check.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "=== [1/4] regen all 5 topology JSONs (rule mode) ==="
"$HERE/regen_all.sh" rule

echo ""
echo "=== [2/4] validate JSON schema ==="
"$HERE/validate_json.sh"

echo ""
echo "=== [3/4] compare across topologies (5 seeds) ==="
"$HERE/compare.sh" 5

echo ""
echo "=== [4/4] claude -p sanity (1 call, ~25s) ==="
if [[ "${SKIP_CLAUDE:-}" == "1" ]]; then
  echo "  (skipped, SKIP_CLAUDE=1)"
else
  "$HERE/claude_check.sh" || echo "  (claude check failed or fell back)"
fi
echo ""
echo "=== all smoke tests done ==="
