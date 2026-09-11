#!/bin/bash
# Leeds Planning — daily refresh (runs at 6am UTC)
# Fetches new applications from PlanIt, rebuilds site.
set -eo pipefail

PROJ=/workspace/dev/leeds-planning-seo
LOCK=/tmp/leeds-planning-pipeline.lock

# Guard 1: kill stale ingest processes left over from a previous crash/hang.
# A hung ingest holds the SQLite lock and wedges every future run.
if pgrep -f "ingest_careful.py" >/dev/null 2>&1; then
    echo "[guard] Killing stale ingest_careful.py process(es) from a previous run..."
    pkill -f "ingest_careful.py" || true
    sleep 3
fi

# Guard 2: never allow two runs to overlap.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[guard] Another leeds-planning run is already in progress — exiting."
    exit 0
fi

# Run the pipeline
cd "$PROJ"
source .venv/bin/activate
python ingest_careful.py
python generate.py
echo "  Done."
