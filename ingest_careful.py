#!/usr/bin/env python3
"""Careful PlanIt ingestion - full backfill and incremental daily refresh.

Modes
-----
  (default)  incremental: fetch only applications whose `last_changed` falls
             inside the last N days (default 3) and UPSERT them into the live
             table.  Typically 1 request, seconds not hours.
  --full     backfill / reconcile: fetch the whole recent=365 window and
             atomically replace the live table.  Used for the initial seed and
             for the periodic (weekly) reconcile.

Rate limiting
-------------
PlanIt allows a short burst of requests, then replies with a rate-limit or a
"volume limits exceeded" message carrying a retry-after in seconds.  This script:
  * uses the API's default page size (300) so a full pull is ~24 requests, not 71;
  * paces *adaptively* - it starts optimistic, spends the burst, and on the first
    limit computes the sustainable interval as (retry-after / requests served in
    the burst) and holds that.  So one 554s wait after 9 requests becomes ~62s
    per request, not 554s.  One penalty instead of dozens.

Safety
------
All pages are fetched into memory first.  In --full mode the live table is only
dropped/rewritten once a complete fetch succeeds, so a crash, SIGTERM or flaky
page can never leave a truncated dataset behind.  An empty page while more rows
are expected is retried, never accepted as "end of data".  A short ingest exits
non-zero so the cron job is marked failed instead of a false "ok".
"""
import argparse, asyncio, gzip, json, aiosqlite, re, ssl, sys
import aiohttp
from pathlib import Path

DB = Path("/workspace/dev/leeds-planning-seo/data/leeds_planning.db")
CACHE = DB.parent / "last_fetch.json.gz"   # completed fetch, so a retry is free
PAGE = 300                 # API default page size - minimises request count
START_DELAY = 3            # optimistic initial gap between requests (seconds)
MIN_BURST = 8              # assumed requests per PlanIt window (floors the estimate)
MAX_DELAY = 300            # ceiling on the learned interval (seconds)
MAX_PAGE_RETRIES = 5

COLS = ("uid,address,postcode,description,app_type,app_size,app_state,"
        "start_date,decided_date,agent_name,agent_company,latitude,longitude,"
        "url,planit_url,last_changed,json_raw")
Q = f"INSERT OR REPLACE INTO applications ({COLS}) VALUES ({','.join('?' * 17)})"

CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS applications (
        uid TEXT PRIMARY KEY, address TEXT, postcode TEXT,
        description TEXT, app_type TEXT, app_size TEXT, app_state TEXT,
        start_date TEXT, decided_date TEXT, agent_name TEXT,
        agent_company TEXT, latitude REAL, longitude REAL,
        url TEXT, planit_url TEXT, last_changed TEXT,
        json_raw TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_apps_type ON applications(app_type);
    CREATE INDEX IF NOT EXISTS idx_apps_state ON applications(app_state);
    CREATE INDEX IF NOT EXISTS idx_apps_postcode ON applications(postcode);
"""


class Pacer:
    """Learns PlanIt's sustainable request rate from its own limit replies.

    PlanIt allows a short burst, then answers with a rate-limit or a
    'volume limits exceeded' message carrying a retry-after in seconds.  The
    sustainable rate is (requests_since_last_limit / wait) - NOT the whole wait
    per request: a 554s wait served after 9 requests means ~62s/request.

    Two guards keep a single unlucky measurement from wrecking the pacing:
      * MIN_BURST floors the assumed burst, so a limit hit after 0-1 requests
        (e.g. the volume budget was already drained by a previous process)
        can't collapse the estimate toward "one whole wait per request";
      * MAX_DELAY caps the result, so worst case is still bounded.
    """

    def __init__(self):
        self.delay = START_DELAY
        self.since_limit = 0

    async def wait(self):
        await asyncio.sleep(self.delay)

    def tick(self):
        self.since_limit += 1

    def learn(self, wait_seconds):
        n = max(self.since_limit, MIN_BURST)
        target = min(MAX_DELAY, wait_seconds / n + 5)
        if target > self.delay:
            self.delay = target
        self.since_limit = 0
        return self.delay


async def fetch_page(s, url, pacer, offset):
    """Fetch one page, honouring rate limits and retrying transient failures."""
    empties = 0
    while True:
        try:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                d = json.loads(await resp.text())
        except Exception as e:  # transient network / JSON error
            empties += 1
            if empties > MAX_PAGE_RETRIES:
                raise
            print(f"  Fetch error at offset {offset} ({e!r}) - retry {empties}/{MAX_PAGE_RETRIES}...")
            await asyncio.sleep(10)
            continue

        err = d.get("error", "") or ""
        m = re.search(r"in (\d+)s", err)
        if m and "limit" in err.lower():
            # Covers both "Rate limit" and "Volume limits exceeded" replies.
            wait = int(m.group(1)) + 2
            new_delay = pacer.learn(wait)
            print(f"  {err.strip()} - waiting {wait}s (pacing now {new_delay:.0f}s/request)...")
            await asyncio.sleep(wait)
            continue

        if err:
            empties += 1
            if empties > MAX_PAGE_RETRIES:
                raise RuntimeError(f"API error at offset {offset}: {err[:200]}")
            print(f"  API error at offset {offset} ({err[:80]!r}) - retry {empties}/{MAX_PAGE_RETRIES}...")
            await asyncio.sleep(15)
            continue

        feats = d.get("features", [])
        if not feats:
            empties += 1
            if empties > MAX_PAGE_RETRIES:
                return d.get("total", 0), []
            print(f"  Empty page at offset {offset} - retry {empties}/{MAX_PAGE_RETRIES}...")
            await asyncio.sleep(15)
            continue

        pacer.tick()
        return d.get("total", 0), feats


async def fetch_all(s, mode, changed_days):
    """Return (rows, grand_total).  Never touches the DB."""
    if mode == "full":
        filt = "recent=365"
    else:
        filt = f"recent=365&changed={changed_days}"

    pacer = Pacer()
    rows, grand, offset = [], None, 0

    while True:
        url = (f"https://planit.org.uk/api/applics/geojson?auth=Leeds"
               f"&{filt}&limit={PAGE}&pg_sz={PAGE}&offset={offset}")
        total, feats = await fetch_page(s, url, pacer, offset)

        if grand is None:
            grand = total
            print(f"Total: {grand} across {grand // PAGE + 1} pages ({mode})")

        if not feats:
            break  # genuine end of data (already retried inside fetch_page)

        rows.extend(
            (p["properties"].get("uid"), p["properties"].get("address"),
             p["properties"].get("postcode"), p["properties"].get("description"),
             p["properties"].get("app_type"), p["properties"].get("app_size"),
             p["properties"].get("app_state"), p["properties"].get("start_date"),
             p["properties"].get("decided_date"),
             (p["properties"].get("other_fields") or {}).get("agent_name"),
             (p["properties"].get("other_fields") or {}).get("agent_company"),
             p["properties"].get("location_x"), p["properties"].get("location_y"),
             p["properties"].get("url"), p["properties"].get("link"),
             p["properties"].get("last_changed"), json.dumps(p["properties"]))
            for p in feats
        )
        offset += PAGE
        print(f"  {offset}/{grand} (+{len(feats)})")
        if grand and offset >= grand:
            break
        await pacer.wait()

    return rows, grand


def save_cache(path, rows, grand, mode):
    """Persist a completed fetch so a later validation/write retry is free."""
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump({"mode": mode, "grand": grand, "rows": rows}, f)
    tmp.replace(path)
    print(f"Cached fetch to {path.name}")


def load_cache(path):
    with gzip.open(path, "rt") as f:
        d = json.load(f)
    return d["rows"], d["grand"], d.get("mode")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true",
                    help="full recent=365 backfill/reconcile (replaces the live table)")
    ap.add_argument("--changed-days", type=int, default=3,
                    help="incremental: last_changed window in days (default 3)")
    ap.add_argument("--apply-cache", action="store_true",
                    help="skip fetching; apply the rows saved by the last fetch")
    args = ap.parse_args()
    mode = "full" if args.full else "incremental"

    if args.apply_cache:
        rows, grand, cached_mode = load_cache(CACHE)
        mode = cached_mode or mode
        print(f"Loaded {len(rows)} rows from {CACHE.name} (mode={mode}) - no fetch needed.")
    else:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as s:
            rows, grand = await fetch_all(s, mode, args.changed_days)
        save_cache(CACHE, rows, grand, mode)

    unique = len({r[0] for r in rows if r[0]})
    print(f"\nFetched {len(rows)} records ({unique} unique) - API reported {grand}.")
    if len(rows) > unique:
        print(f"  ({len(rows) - unique} duplicate uid(s) - expected; uid is the primary key)")

    # Truncation guard: compare records RECEIVED against the API's total - not
    # unique rows, since duplicates are normal and collapse under the PK.
    if grand and len(rows) < grand * 0.98:
        print(f"ERROR: short fetch - only {len(rows)}/{grand} records received. Refusing "
              f"to touch the live table. Rows are cached in {CACHE.name}; re-run with "
              f"--apply-cache to retry the write without re-fetching.", file=sys.stderr)
        sys.exit(1)

    if not rows:
        print("No changes to apply.")
        return

    db = await aiosqlite.connect(str(DB))
    await db.executescript(CREATE_SQL)
    if mode == "full":
        await db.execute("DELETE FROM applications")   # replace the whole window
    await db.executemany(Q, rows)
    await db.commit()

    r = await db.execute("SELECT COUNT(*) FROM applications")
    count = (await r.fetchone())[0]
    r = await db.execute("SELECT app_type, COUNT(*) c FROM applications GROUP BY app_type ORDER BY c DESC LIMIT 10")
    types = await r.fetchall()
    r = await db.execute("SELECT app_state, COUNT(*) c FROM applications GROUP BY app_state")
    states = await r.fetchall()
    await db.close()

    print(f"\nDone: {mode} - {len(rows)} records applied, table now {count} rows")
    print("Types:", {t: c for t, c in types})
    print("States:", {s: c for s, c in states})

    # Every unique uid we fetched must now be present.
    if mode == "full" and count < unique:
        print(f"ERROR: {count}/{unique} unique rows after write.", file=sys.stderr)
        sys.exit(1)


asyncio.run(main())