#!/usr/bin/env python3
"""Careful PlanIt ingestion — handles rate limits, small pages."""
import asyncio, json, aiosqlite, re, ssl, time
import aiohttp
from pathlib import Path

DB = Path("/workspace/dev/leeds-planning-seo/data/leeds_planning.db")
PAGE, DELAY = 100, 3

async def main():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    async with aiohttp.ClientSession(connector=connector) as s:
        db = await aiosqlite.connect(str(DB))
        await db.execute("DROP TABLE IF EXISTS applications")
        await db.executescript("""
            CREATE TABLE applications (
                uid TEXT PRIMARY KEY, address TEXT, postcode TEXT,
                description TEXT, app_type TEXT, app_size TEXT, app_state TEXT,
                start_date TEXT, decided_date TEXT, agent_name TEXT,
                agent_company TEXT, latitude REAL, longitude REAL,
                url TEXT, planit_url TEXT, last_changed TEXT,
                json_raw TEXT
            );
            CREATE INDEX idx_apps_type ON applications(app_type);
            CREATE INDEX idx_apps_state ON applications(app_state);
            CREATE INDEX idx_apps_postcode ON applications(postcode);
        """)
        await db.commit()

        total, offset = 0, 0
        grand = None

        while True:
            url = f"https://planit.org.uk/api/applics/geojson?auth=Leeds&limit={PAGE}&pg_sz={PAGE}&recent=365&offset={offset}"
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                d = json.loads(await resp.text())

            err = d.get("error", "")
            if "Rate limit" in err:
                m = re.search(r"in (\d+)s", err)
                wait = int(m.group(1)) + 2 if m else 10
                print(f"  Rate limited, waiting {wait}s...")
                await asyncio.sleep(wait)
                continue

            if grand is None:
                grand = d.get("total", 0)
                print(f"Total: {grand} across {grand // PAGE + 1} pages")

            feats = d.get("features", [])
            if not feats:
                break

            q = "INSERT OR REPLACE INTO applications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            await db.executemany(q, [
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
            ])

            total += len(feats)
            offset += PAGE
            print(f"  {total}/{grand} (+{len(feats)})")
            if offset >= grand:
                break
            await asyncio.sleep(DELAY)

        await db.commit()

        # Verify
        r = await db.execute("SELECT COUNT(*) FROM applications")
        count = (await r.fetchone())[0]
        r = await db.execute("SELECT app_type, COUNT(*) c FROM applications GROUP BY app_type ORDER BY c DESC LIMIT 10")
        types = await r.fetchall()
        r = await db.execute("SELECT app_state, COUNT(*) c FROM applications GROUP BY app_state")
        states = await r.fetchall()

        print(f"\nDone: {count}")
        print("Types:", {t: c for t, c in types})
        print("States:", {s: c for s, c in states})

        await db.close()

asyncio.run(main())