#!/bin/bash
# Leeds Planning - weekly full reconcile (runs 4am UTC Sunday).
# Re-fetches the whole recent=365 window and replaces the live table, so
# applications that have aged out of the window are dropped. ~24 paced requests.
set -eo pipefail

PROJ=/workspace/dev/leeds-planning-seo
LOCK=/tmp/leeds-planning-pipeline.lock

# Guard 1: kill stale ingest processes left over from a previous crash/hang.
if pgrep -f "ingest_careful.py" >/dev/null 2>&1; then
    echo "[guard] Killing stale ingest_careful.py process(es) from a previous run..."
    pkill -f "ingest_careful.py" || true
    sleep 3
fi

# Guard 2: never allow two runs to overlap (shared with the daily job).
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[guard] Another leeds-planning run is already in progress - exiting."
    exit 0
fi

cd "$PROJ"
source .venv/bin/activate
python ingest_careful.py --full
python generate.py
echo "  Done."