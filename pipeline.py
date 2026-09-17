#!/usr/bin/env python3
"""
Leeds Planning SEO - Data Pipeline
Fetches planning applications from PlanIt.org.uk API → SQLite.

PlanIt is a free aggregator of all UK council planning portals.
No auth, no rate limit (but be courteous).

Usage:
    python pipeline.py              # Fetch this year's Leeds applications
    python pipeline.py --full       # Fetch all available history
    python pipeline.py --recent 7   # Last 7 days only (for cron)
"""

import asyncio
import aiohttp
import aiosqlite
import json
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path(__file__).parent / "data" / "leeds_planning.db"
API_BASE = "https://planit.org.uk/api/applics/geojson"
REQUEST_DELAY = 1.0
PAGE_SIZE = 200  # PlanIt rate limits aggressively above 200
MAX_RETRIES = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("pipeline")

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    uid TEXT PRIMARY KEY,
    address TEXT,
    postcode TEXT,
    description TEXT,
    app_type TEXT,
    app_size TEXT,
    app_state TEXT,
    start_date TEXT,
    decided_date TEXT,
    consulted_date TEXT,
    agent_name TEXT,
    agent_address TEXT,
    agent_company TEXT,
    applicant_name TEXT,
    applicant_address TEXT,
    latitude REAL,
    longitude REAL,
    area_name TEXT DEFAULT 'Leeds',
    url TEXT,
    planit_url TEXT,
    last_scraped TEXT,
    last_changed TEXT,
    json_raw TEXT,
    fetched_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_apps_type ON applications(app_type);
CREATE INDEX IF NOT EXISTS idx_apps_state ON applications(app_state);
CREATE INDEX IF NOT EXISTS idx_apps_postcode ON applications(postcode);
CREATE INDEX IF NOT EXISTS idx_apps_date ON applications(start_date);
CREATE INDEX IF NOT EXISTS idx_apps_combo ON applications(app_type, app_state, postcode);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    applications_fetched INTEGER DEFAULT 0,
    applications_new INTEGER DEFAULT 0,
    applications_updated INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'
);
"""


async def init_db(db: aiosqlite.Connection):
    await db.executescript(SCHEMA)
    await db.commit()


async def fetch_page(session: aiohttp.ClientSession, params: dict) -> dict:
    url = f"{API_BASE}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                text = await resp.text()
                data = json.loads(text)

                # PlanIt returns rate-limit messages in the body (often with HTTP 200)
                err = data.get("error", "")
                if "Rate limit" in err or "rate limit" in err.lower():
                    import re
                    m = re.search(r"in (\d+)s", err)
                    wait = int(m.group(1)) if m else 60
                    log.warning(f"Rate limited: '{err}' - waiting {wait}s")
                    await asyncio.sleep(min(wait, 300))
                    continue
                if err:
                    log.warning(f"API error on attempt {attempt}: {err}")
                    await asyncio.sleep(5)
                    continue

                resp.raise_for_status()
                return data
        except Exception as e:
            log.warning(f"Attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(5)
            else:
                raise


async def store_application(db: aiosqlite.Connection, props: dict) -> str:
    uid = props.get("uid", "")
    if not uid:
        return "skipped"

    other = props.get("other_fields") or {}

    # Check if exists and changed
    cursor = await db.execute(
        "SELECT app_state, start_date, last_changed FROM applications WHERE uid = ?", (uid,)
    )
    existing = await cursor.fetchone()

    if existing:
        old_state, old_date, old_changed = existing
        new_changed = props.get("last_changed", "")
        if new_changed and old_changed == new_changed:
            return "skipped"
        action = "updated"
    else:
        action = "new"

    await db.execute(
        """
        INSERT INTO applications (
            uid, address, postcode, description, app_type, app_size, app_state,
            start_date, decided_date, consulted_date,
            agent_name, agent_address, agent_company,
            applicant_name, applicant_address,
            latitude, longitude, area_name, url, planit_url,
            last_scraped, last_changed, json_raw, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(uid) DO UPDATE SET
            address=excluded.address, postcode=excluded.postcode,
            description=excluded.description, app_type=excluded.app_type,
            app_size=excluded.app_size, app_state=excluded.app_state,
            start_date=excluded.start_date, decided_date=excluded.decided_date,
            consulted_date=excluded.consulted_date,
            agent_name=excluded.agent_name, agent_address=excluded.agent_address,
            agent_company=excluded.agent_company,
            applicant_name=excluded.applicant_name, applicant_address=excluded.applicant_address,
            latitude=excluded.latitude, longitude=excluded.longitude,
            url=excluded.url, planit_url=excluded.planit_url,
            last_scraped=excluded.last_scraped, last_changed=excluded.last_changed,
            json_raw=excluded.json_raw, updated_at=excluded.updated_at
        """,
        (
            uid,
            props.get("address", ""),
            props.get("postcode", ""),
            props.get("description", ""),
            props.get("app_type", ""),
            props.get("app_size", ""),
            props.get("app_state", ""),
            props.get("start_date", ""),
            props.get("decided_date", ""),
            props.get("consulted_date", ""),
            other.get("agent_name", ""),
            other.get("agent_address", ""),
            other.get("agent_company", ""),
            other.get("applicant_name", ""),
            other.get("applicant_address", ""),
            float(props.get("location_x", 0) or 0),
            float(props.get("location_y", 0) or 0),
            props.get("area_name", "Leeds"),
            props.get("url", ""),
            props.get("link", ""),
            props.get("last_scraped", ""),
            props.get("last_changed", ""),
            json.dumps(props),
        ),
    )
    return action


async def ingest(session: aiohttp.ClientSession, db: aiosqlite.Connection, start_date: str, end_date: str) -> dict:
    stats = {"fetched": 0, "new": 0, "updated": 0, "skipped": 0}
    offset = 0
    total = None

    params = {
        "auth": "Leeds",
        "limit": PAGE_SIZE,
        "pg_sz": PAGE_SIZE,
        "recent": (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days,
        "sort": "start_date",
    }

    while True:
        params["offset"] = offset
        data = await fetch_page(session, params)

        if total is None:
            total = data.get("total", 0)
            log.info(f"Total applications: {total}")

        features = data.get("features", [])
        if not features:
            break

        for feat in features:
            props = feat.get("properties", {})
            action = await store_application(db, props)
            stats["fetched"] += 1
            if action == "new":
                stats["new"] += 1
            elif action == "updated":
                stats["updated"] += 1
            else:
                stats["skipped"] += 1

        log.info(f"Offset {offset}: {stats['fetched']}/{total} | new:{stats['new']} upd:{stats['updated']}")
        offset += PAGE_SIZE
        if offset >= total:
            break
        await asyncio.sleep(REQUEST_DELAY)

    await db.commit()
    return stats


async def verify(db: aiosqlite.Connection):
    cursor = await db.execute("SELECT COUNT(*) FROM applications")
    total = (await cursor.fetchone())[0]

    cursor = await db.execute("""
        SELECT app_type, COUNT(*) as cnt FROM applications
        GROUP BY app_type ORDER BY cnt DESC LIMIT 15
    """)
    types = await cursor.fetchall()

    cursor = await db.execute("""
        SELECT app_state, COUNT(*) as cnt FROM applications
        GROUP BY app_state ORDER BY cnt DESC
    """)
    states = await cursor.fetchall()

    log.info(f"Total: {total}")
    log.info(f"Types: {dict(types)}")
    log.info(f"States: {dict(states)}")


async def main():
    import sys, ssl

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Determine date range
    end_date = datetime.now().strftime("%Y-%m-%d")

    if "--full" in sys.argv:
        start_date = "2020-01-01"
        log.info(f"Full fetch: {start_date} to {end_date}")
    elif "--recent" in sys.argv:
        idx = sys.argv.index("--recent")
        days = int(sys.argv[idx + 1])
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        log.info(f"Recent {days} days: {start_date} to {end_date}")
    else:
        start_date = f"{datetime.now().year}-01-01"
        log.info(f"This year: {start_date} to {end_date}")

    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_context)

    async with aiohttp.ClientSession(connector=connector) as session:
        db = await aiosqlite.connect(str(DB_PATH))
        await db.execute("PRAGMA journal_mode=WAL")
        await init_db(db)

        try:
            # Record run
            await db.execute("INSERT INTO pipeline_runs (started_at, status) VALUES (datetime('now'), 'running')")
            await db.commit()

            stats = await ingest(session, db, start_date, end_date)

            await verify(db)

            await db.execute(
                "UPDATE pipeline_runs SET finished_at=datetime('now'), applications_fetched=?, applications_new=?, applications_updated=?, status='success' WHERE id=(SELECT MAX(id) FROM pipeline_runs)",
                (stats["fetched"], stats["new"], stats["updated"]),
            )
            await db.commit()

            log.info(f"Done: {stats['fetched']} fetched, {stats['new']} new, {stats['updated']} updated")
        except Exception as e:
            log.error(f"Failed: {e}", exc_info=True)
            await db.execute(
                "UPDATE pipeline_runs SET finished_at=datetime('now'), status='failed' WHERE id=(SELECT MAX(id) FROM pipeline_runs)"
            )
            await db.commit()
            raise
        finally:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())