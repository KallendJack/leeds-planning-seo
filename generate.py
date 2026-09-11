#!/usr/bin/env python3
"""Leeds Planning SEO — Static Site Generator. Reads SQLite → Jinja2 → static HTML."""

import asyncio, aiosqlite, math, shutil, time
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

DB_PATH = Path(__file__).parent / "data" / "leeds_planning.db"
TEMPLATES_DIR = Path(__file__).parent / "templates"
OUTPUT_DIR = Path(__file__).parent / "output"
STATIC_SRC = Path(__file__).parent / "static"   # source assets copied into the build
PAGE_SIZE = 50
SITE_URL = "https://leedsplanning.org.uk"

# Affiliate link — drop in your Bark.com / Awin link here when ready
AFFILIATE_LINK = ""

# Keywords that trigger the "get quotes" affiliate CTA on application detail pages
CONSTRUCTION_KEYWORDS = [
    "extension", "loft", "conversion", "dwelling", "demolition", "erection",
    "new build", "residential", "garage", "porch", "dormer", "roof",
    "kitchen", "bathroom", "conservatory", "orangery", "render", "cladding"
]

env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))


def slugify(s: str) -> str:
    return s.lower().replace(" ", "-").replace("/", "-")


def is_construction(app: dict) -> bool:
    """Check if this application is a trade-eligible construction project."""
    text = f"{app.get('description','')} {app.get('app_type','')}".lower()
    return any(kw in text for kw in CONSTRUCTION_KEYWORDS)


async def get_dynamic_types(db: aiosqlite.Connection) -> list:
    rows = await db.execute_fetchall("""
        SELECT app_type, COUNT(*) as cnt FROM applications
        WHERE app_type != ''
        GROUP BY app_type ORDER BY cnt DESC LIMIT 6
    """)
    return [{"slug": slugify(r[0]), "label": r[0], "count": r[1]} for r in rows]


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def write_html(p: Path, content: str):
    ensure_dir(p.parent)
    p.write_text(content)


async def build_homepage(db: aiosqlite.Connection):
    print("Building homepage...")
    tpl = env.get_template("home.html")

    stats = dict(await (await db.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN app_state='Undecided' THEN 1 ELSE 0 END) as undecided,
               SUM(CASE WHEN app_state LIKE '%Permitted%' OR app_state LIKE '%Approved%' THEN 1 ELSE 0 END) as permitted,
               SUM(CASE WHEN app_state LIKE '%Refused%' THEN 1 ELSE 0 END) as refused,
               SUM(CASE WHEN app_type='Full' THEN 1 ELSE 0 END) as full
        FROM applications
    """)).fetchone())

    type_icons = {"Full": "🏠", "Trees": "🌳", "Conditions": "📋", "Amendment": "📝",
                  "Outline": "📐", "Heritage": "🏛️", "Advertising": "📢", "Other": "📄", "Telecoms": "📡"}
    top_types = await get_dynamic_types(db)
    for t in top_types:
        t["icon"] = type_icons.get(t["label"], "📄")

    postcode_rows = await db.execute_fetchall("""
        SELECT SUBSTR(postcode,1,3) as code, COUNT(*) as cnt
        FROM applications WHERE postcode LIKE 'LS%' AND postcode != ''
        GROUP BY code ORDER BY cnt DESC LIMIT 12
    """)
    postcode_areas = [{"code": r[0], "total": r[1]} for r in postcode_rows]

    recent_rows = await db.execute_fetchall("""
        SELECT uid, address, description, app_type, app_size, app_state, start_date, postcode
        FROM applications WHERE start_date != '' ORDER BY start_date DESC LIMIT 10
    """)
    recent = [dict(zip(["uid","address","description","app_type","app_size","app_state","start_date","postcode"], r)) for r in recent_rows]

    html = tpl.render(title="Leeds Planning Applications — Track What's Being Built",
                      meta_description=f"Track {stats['total']} planning applications in Leeds this year. Extensions, new builds, loft conversions, and more.",
                      canonical_url="/", stats=stats, top_types=top_types,
                      postcode_areas=postcode_areas, recent=recent, breadcrumbs=[],
                      hide_breadcrumb=True)
    write_html(OUTPUT_DIR / "index.html", html)


async def build_application_pages(db: aiosqlite.Connection):
    print("Building application detail pages...")
    tpl = env.get_template("application.html")
    rows = await db.execute_fetchall("SELECT * FROM applications")
    cols = ["uid","address","postcode","description","app_type","app_size","app_state","start_date","decided_date","agent_name","agent_company","latitude","longitude","url","planit_url","last_changed","json_raw"]

    count = 0
    for row in rows:
        app = dict(zip(cols, row))

        nearby = []
        if app.get("postcode") and len(app["postcode"]) >= 3:
            prefix = app["postcode"][:3]
            near_rows = await db.execute_fetchall(
                "SELECT uid, address, app_type FROM applications WHERE postcode LIKE ? AND uid != ? LIMIT 5",
                (f"{prefix}%", app["uid"])
            )
            nearby = [{"uid": r[0], "address": r[1], "app_type": r[2]} for r in near_rows]

        html = tpl.render(
            title=f"{app.get('address','Planning Application')[:40]} — {app.get('app_type','')} | Leeds Planning",
            meta_description=f"{app.get('description','')[:150]}. {app.get('app_state','')}. Reference: {app.get('uid','')}.",
            canonical_url=f"/application/{app['uid'].replace('/','_')}/",
            app=app, nearby=nearby, breadcrumbs=[],
            is_construction=is_construction(app),
            affiliate_link=AFFILIATE_LINK,
        )
        write_html(OUTPUT_DIR / "application" / app["uid"].replace("/", "_") / "index.html", html)
        count += 1
    print(f"  {count} application pages")


async def build_listing_pages(db: aiosqlite.Connection):
    print("Building listing pages...")
    tpl = env.get_template("listing.html")

    types = await db.execute_fetchall("SELECT app_type, COUNT(*) FROM applications GROUP BY app_type ORDER BY COUNT(*) DESC")

    for app_type, total in types:
        slug = slugify(app_type)
        rows = await db.execute_fetchall(
            "SELECT uid, address, description, app_type, app_size, app_state, start_date, decided_date, postcode FROM applications WHERE app_type=? ORDER BY start_date DESC",
            (app_type,)
        )
        dir = OUTPUT_DIR / slug

        for page in range(1, max(1, math.ceil(len(rows) / PAGE_SIZE)) + 1):
            batch = rows[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
            subdir = f"page/{page}" if page > 1 else ""

            html = tpl.render(
                title=f"{app_type} Planning Applications — Leeds",
                meta_description=f"{total} {app_type.lower()} planning applications in Leeds this year. Track decisions and see locations.",
                canonical_url=f"/{slug}/" + (f"page/{page}/" if page > 1 else ""),
                heading=f"{app_type} Applications in Leeds",
                total=len(rows), current_page=page,
                total_pages=max(1, math.ceil(len(rows)/PAGE_SIZE)),
                base_url=f"/{slug}/",
                applications=[dict(zip(["uid","address","description","app_type","app_size","app_state","start_date","decided_date","postcode"], b)) for b in batch],
                breadcrumbs=[], current_crumb=app_type,
                sorted_by="date (newest first)",
                related_links=[{"url": f"/postcode/", "label": "Browse by postcode area"}],
            )
            write_html(dir / subdir / "index.html" if subdir else dir / "index.html", html)
    print(f"  {len(types)} type pages")


async def build_postcode_pages(db: aiosqlite.Connection):
    print("Building postcode pages...")
    tpl = env.get_template("listing.html")

    areas = await db.execute_fetchall("""
        SELECT SUBSTR(postcode,1,3) as code, COUNT(*) as cnt
        FROM applications WHERE postcode LIKE 'LS%' AND postcode != ''
        GROUP BY code ORDER BY cnt DESC
    """)

    # Build postcode index as a proper grid page — not using listing template
    cards = "\n".join(
        f'<a href="/postcode/{code.lower()}/" class="postcode-card"><strong>{code}</strong><span>{cnt} applications</span></a>'
        for code, cnt in areas
    )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Planning Applications by Postcode — Leeds</title>
    <meta name="description" content="Browse planning applications by Leeds postcode area. Find what's being built in LS1, LS6, LS8 and more.">
    <link rel="canonical" href="{SITE_URL}/postcode/">
    <link rel="stylesheet" href="/static/style.css?v={int(time.time())}">
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🏗️</text></svg>">
</head>
<body>
    <header><div class="container">
        <nav><a href="/" class="logo">🏗️ Leeds Planning</a><div class="nav-links">"""
    for t in env.globals.get("nav_types", []):
        html += f'<a href="/{t["slug"]}/">{t["label"]}</a>\n'
    html += f"""<a href="/postcode/">By Postcode</a><a href="/about/">About</a></div></nav>
    </div></header>
    <main class="container">
        <nav class="breadcrumb"><a href="/">Home</a> &rsaquo; Postcodes</nav>
        <h1>Planning Applications by Postcode</h1>
        <p class="results-summary">{sum(r[1] for r in areas)} applications across {len(areas)} postcode areas.</p>
        <div class="postcode-grid">{cards}</div>
    </main>
    <footer><div class="container">
        <p class="footer-bottom">Data from <a href="https://planit.org.uk">PlanIt.org.uk</a> and <a href="https://publicaccess.leeds.gov.uk">Leeds City Council</a> under the Open Government Licence.</p>
        <div class="legal-disclaimer" style="margin-top:2rem;padding-top:1.5rem;border-top:1px solid var(--gray-200);font-size:0.8rem;color:var(--gray-600);line-height:1.7;">
            <p style="margin-bottom:0.75rem;"><strong>Independence Notice:</strong> leedsplanning.org.uk is an independent, privately operated community resource. Not affiliated with or endorsed by Leeds City Council.</p>
            <p><strong>Affiliate Disclosure:</strong> We may earn a referral commission from partner links at no cost to you.</p>
        </div>
    </div></footer>
</body>
</html>"""
    write_html(OUTPUT_DIR / "postcode" / "index.html", html)

    for code, total in areas:
        rows = await db.execute_fetchall(
            "SELECT uid, address, description, app_type, app_size, app_state, start_date, decided_date, postcode FROM applications WHERE postcode LIKE ? ORDER BY start_date DESC",
            (f"{code}%",)
        )
        dir = OUTPUT_DIR / "postcode" / code.lower()

        for page in range(1, max(1, math.ceil(len(rows) / PAGE_SIZE)) + 1):
            batch = rows[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
            subdir = f"page/{page}" if page > 1 else ""

            html = tpl.render(
                title=f"Planning Applications in {code.upper()} — Leeds",
                meta_description=f"{len(rows)} planning applications in {code.upper()} Leeds. Extensions, new builds, tree works and more.",
                canonical_url=f"/postcode/{code.lower()}/" + (f"page/{page}/" if page > 1 else ""),
                heading=f"Planning in {code.upper()}",
                total=len(rows), current_page=page,
                total_pages=max(1, math.ceil(len(rows)/PAGE_SIZE)),
                base_url=f"/postcode/{code.lower()}/",
                applications=[dict(zip(["uid","address","description","app_type","app_size","app_state","start_date","decided_date","postcode"], b)) for b in batch],
                breadcrumbs=[{"url":"/postcode/","label":"Postcodes"}], current_crumb=code.upper(),
                sorted_by="date (newest first)",
                related_links=[],
            )
            write_html(dir / subdir / "index.html" if subdir else dir / "index.html", html)
    print(f"  {len(areas)} postcode area pages")


async def build_about_page():
    """About page matching the full site template with footer + legal."""
    ensure_dir(OUTPUT_DIR / "about")
    ts = int(time.time())
    nav_links = ""
    for t in env.globals.get("nav_types", []):
        nav_links += f'<a href="/{t["slug"]}/">{t["label"]}</a>\n'
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>About — Leeds Planning</title>
    <meta name="description" content="About Leeds Planning — tracking planning applications across Leeds from public council data.">
    <link rel="canonical" href="{SITE_URL}/about/">
    <link rel="stylesheet" href="/static/style.css?v={ts}">
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🏗️</text></svg>">
</head>
<body>
    <header>
        <div class="container">
            <nav>
                <a href="/" class="logo">🏗️ Leeds Planning</a>
                <div class="nav-links">
                    {nav_links}
                    <a href="/postcode/">By Postcode</a>
                    <a href="/about/">About</a>
                </div>
            </nav>
        </div>
    </header>
    <main class="container" style="padding:2rem 0">
        <h1>About Leeds Planning</h1>
        <p>This site tracks planning applications submitted to Leeds City Council. All data is public record, sourced from PlanIt.org.uk and Leeds City Council's planning portal, under the Open Government Licence.</p>
        <p>We make it easy to browse applications by type, postcode, or status — no forms, no logins, no council portal frustration.</p>
        <p>Some pages contain affiliate links to Bark.com. If you request quotes through these links, we may earn a small commission at no cost to you. This helps keep the site free and ad-free.</p>
        <p><a href="/">← Back to applications</a></p>
    </main>
    <footer>
        <div class="container">
            <p class="footer-bottom">Data from <a href="https://planit.org.uk">PlanIt.org.uk</a> and <a href="https://publicaccess.leeds.gov.uk">Leeds City Council</a> under the Open Government Licence.</p>
            <div class="legal-disclaimer" style="margin-top:2rem;padding-top:1.5rem;border-top:1px solid var(--gray-200);font-size:0.8rem;color:var(--gray-600);line-height:1.7;">
                <p style="margin-bottom:0.75rem;"><strong>Independence Notice:</strong> leedsplanning.org.uk is an independent, privately operated community resource that aggregates public planning data. Not affiliated with or endorsed by Leeds City Council.</p>
                <p><strong>Affiliate Disclosure:</strong> We may earn a referral commission from partner links at no cost to you.</p>
            </div>
        </div>
    </footer>
</body>
</html>""".format(SITE_URL=SITE_URL, ts=ts, nav_links=nav_links)
    write_html(OUTPUT_DIR / "about" / "index.html", html)


async def build_sitemap(db: aiosqlite.Connection):
    print("Building sitemap...")
    urls = [f"{SITE_URL}/", f"{SITE_URL}/about/"]

    rows = await db.execute_fetchall("SELECT uid FROM applications")
    for (uid,) in rows:
        urls.append(f"{SITE_URL}/application/{uid.replace('/','_')}/")

    types = await db.execute_fetchall("SELECT DISTINCT app_type FROM applications")
    for (t,) in types:
        urls.append(f"{SITE_URL}/{slugify(t)}/")

    areas = await db.execute_fetchall("SELECT DISTINCT SUBSTR(postcode,1,3) FROM applications WHERE postcode LIKE 'LS%' AND postcode != ''")
    for (code,) in areas:
        urls.append(f"{SITE_URL}/postcode/{code.lower()}/")

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for url in sorted(set(urls)):
        xml += f"  <url><loc>{url}</loc></url>\n"
    xml += "</urlset>\n"
    write_html(OUTPUT_DIR / "sitemap.xml", xml)
    print(f"  {len(set(urls))} URLs")


def clean_output():
    """Empty the output dir before a build.

    Without this, pages for applications that have dropped out of the 365-day
    window (or been re-referenced) linger on disk and keep being served even
    though they are no longer in the DB or the sitemap.  We clear the CONTENTS
    rather than removing OUTPUT_DIR itself, so a bind mount / serving path
    stays intact.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for child in OUTPUT_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def copy_static():
    """Copy source assets (static/) into the build.

    style.css used to live only in the gitignored output dir, so a clean
    rebuild would have silently produced a site with no stylesheet.
    """
    if not STATIC_SRC.exists():
        return
    dest = OUTPUT_DIR / "static"
    dest.mkdir(parents=True, exist_ok=True)
    for f in STATIC_SRC.iterdir():
        if f.is_file():
            shutil.copy2(f, dest / f.name)


async def main():
    start = time.time()
    clean_output()
    copy_static()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row

        dyn_types = await get_dynamic_types(db)
        env.globals["nav_types"] = dyn_types
        env.globals["affiliate_link"] = AFFILIATE_LINK
        env.globals["build_timestamp"] = str(int(time.time()))

        await build_homepage(db)
        await build_application_pages(db)
        await build_listing_pages(db)
        await build_postcode_pages(db)
        await build_about_page()
        await build_sitemap(db)

    total = sum(1 for _ in OUTPUT_DIR.rglob("*.html"))
    print(f"\n✅ {total} HTML pages in {time.time()-start:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())