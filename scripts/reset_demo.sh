#!/usr/bin/env bash
# FIX (code freeze, item 5): reset ONLY the demo database so the demo can be
# repeated. Deletes backend/data/campuspilot.db (+ -wal/-shm) and reseeds.
#
#   ./scripts/reset_demo.sh
#
# Stop the server first. This script never touches code, .env, git history,
# or any other file.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="$ROOT/backend/data/campuspilot.db"

echo "CampusPilot demo reset"
echo "  deleting: $DB (and -wal / -shm)"

for f in "$DB" "$DB-wal" "$DB-shm"; do
  if [ -e "$f" ]; then
    rm -f "$f"
    echo "  removed  $(basename "$f")"
  else
    echo "  absent   $(basename "$f")"
  fi
done

if [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="python3"
fi

(cd "$ROOT/backend" && "$PYTHON" -m services.seed_data)

echo "Done. Start the server again: cd backend && python -m uvicorn server:app --host 127.0.0.1 --port 8000"
