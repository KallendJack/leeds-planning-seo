#!/bin/bash
# Waits for the full backfill to finish, then regenerates the site and writes a
# verification report to /tmp/after-backfill.txt.  Temporary utility.
set -uo pipefail
cd /workspace/dev/leeds-planning-seo
source .venv/bin/activate

# Wait for the full ingest to exit (the pipeline's bash wrapper matches too,
# but it exits at the same moment).
while pgrep -f "ingest_careful.py --full" >/dev/null 2>&1; do sleep 20; done
sleep 3

{
  echo "=== ingest finished at $(date -u) ==="
  echo "--- rows in DB ---"
  python3 -c "import sqlite3;c=sqlite3.connect('data/leeds_planning.db');print('rows:',c.execute('select count(*) from applications').fetchone()[0])"
  echo "--- regenerate ---"
  python generate.py
  echo "--- application pages ---"
  ls output/application/*.html 2>/dev/null | wc -l
  echo "--- sitemap urls ---"
  grep -c "<url>" output/sitemap.xml
  echo "--- index.html ---"
  ls -la output/index.html
} > /tmp/after-backfill.txt 2>&1
echo "POST-BACKFILL DONE"
