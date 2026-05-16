#!/bin/bash
# Start the FastAPI backend (+ static frontend at http://localhost:8000/demo.html).
set -euo pipefail
cd "$(dirname "$0")/../backend"
exec python3 -m uvicorn app:app --host 0.0.0.0 --port "${1:-8000}" --reload
