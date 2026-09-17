#!/usr/bin/env python3
"""Leeds Planning SEO - Static Site Generator. Reads SQLite → Jinja2 → static HTML."""

import asyncio, aiosqlite, json, math, re, shutil, time
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

DB_PATH = Path(__file__).parent / "data" / "leeds_planning.db"
TEMPLATES_DIR = Path(__file__).parent / "templates"
OUTPUT_DIR = Path(__file__).parent / "output"
STATIC_SRC = Path(__file__).parent / "static"   # source assets copied into the build
PAGE_SIZE = 50
SITE_URL = "https://leedsplanning.org.uk"

# A UK postcode's "outcode" is the part before the space: "LS20 8JB" -> "LS20".
# Taking a fixed 3-char prefix is wrong - it folds LS20-LS29 all into "LS2".
OUTCODE_SQL = ("CASE WHEN INSTR(postcode,' ') > 0 "
               "THEN SUBSTR(postcode,1,INSTR(postcode,' ')-1) ELSE postcode END")

# Affiliate link - drop in your Bark.com / Awin link here when ready
AFFILIATE_LINK = ""

# Keywords that trigger the "get quotes" affiliate CTA on application detail pages
CONSTRUCTION_KEYWORDS = [
    "extension", "loft", "conversion", "dwelling", "demolition", "erection",
    "new build", "residential", "garage", "porch", "dormer", "roof",
    "kitchen", "bathroom", "conservatory", "orangery", "render", "cladding"
]

env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))


def outcode(postcode: str) -> str:
    """'LS20 8JB' -> 'LS20'.  Exposed to templates so nothing re-derives the
    postcode area with a wrong fixed-width slice."""
    return (postcode or "").split(" ")[0]


env.filters["outcode"] = outcode


# --- <title> construction -------------------------------------------------
# Full UK postcode at the end of an address, e.g. "... Leeds LS15 4NJ"
POSTCODE_RE = re.compile(r"([A-Z]{1,2}\d{1,2}[A-Z]?)\s*(\d[A-Z]{2})\s*$", re.I)
TITLE_SUFFIX = " | Leeds Planning"
TITLE_LIMIT = 68


def build_title(address: str, app_type: str = "", limit: int = TITLE_LIMIT) -> str:
    """Word-boundary-safe <title> for an application page.

    The old code used ``address[:40]``, which chopped mid-word and produced
    titles like "Fox And Grapes York Road Scholes Leeds L - Full", shipped on
    every one of the ~7k application pages.  Here the address is cut on a
    space and the postcode tail is preserved when it fits, so a long title
    keeps both readable words and a local-search signal.
    """
    addr = " ".join((address or "").split()).strip(" ,")
    if not addr:
        addr = "Planning Application"

    tail = (f" — {app_type}" if app_type else "") + TITLE_SUFFIX
    budget = max(24, limit - len(tail))

    if len(addr) > budget:
        m = POSTCODE_RE.search(addr)
        postcode = f"{m.group(1).upper()} {m.group(2).upper()}" if m else ""
        head = addr[:m.start()].strip(" ,") if m else addr

        if postcode and len(postcode) + 4 <= budget - 10:
            room = budget - len(postcode) - 2      # 2 = "… "
            if len(head) > room:
                head = head[:room]
                head = head[:head.rindex(" ")] if " " in head else head
            addr = f"{head.rstrip(' ,')}… {postcode}"
        else:
            cut = addr[:budget]
            cut = cut[:cut.rindex(" ")] if " " in cut else cut
            addr = cut.rstrip(" ,") + "…"

    return f"{addr}{tail}"


# --- structured data ------------------------------------------------------
def jsonld_dump(obj) -> str:
    """Serialise JSON-LD safely for inline <script> (no </script> breakout)."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return (s.replace("<", "\\u003c").replace(">", "\\u003e")
             .replace("&", "\\u0026"))


def _website_node() -> dict:
    return {"@type": "WebSite", "@id": f"{SITE_URL}/#website", "url": f"{SITE_URL}/",
            "name": "Leeds Planning", "inLanguage": "en-GB"}


def _breadcrumb_node(crumbs: list) -> dict:
    """crumbs: [(name, url_path), ...] - must mirror the visible breadcrumb."""
    return {"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": name, "item": f"{SITE_URL}{path}"}
        for i, (name, path) in enumerate(crumbs)]}


def page_jsonld(title: str, description: str, path: str, crumbs=None, extra=None) -> str:
    url = f"{SITE_URL}{path}"
    graph = [
        _website_node(),
        {"@type": "WebPage", "@id": url, "url": url, "name": title,
         "description": description, "inLanguage": "en-GB",
         "isPartOf": {"@id": f"{SITE_URL}/#website"}},
    ]
    if crumbs:
        graph.append(_breadcrumb_node(crumbs))
    if extra:
        graph.extend(extra)
    return jsonld_dump({"@context": "https://schema.org", "@graph": graph})


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

    postcode_rows = await db.execute_fetchall(f"""
        SELECT {OUTCODE_SQL} as code, COUNT(*) as cnt
        FROM applications WHERE postcode != ''
        GROUP BY code ORDER BY cnt DESC LIMIT 12
    """)
    postcode_areas = [{"code": r[0], "total": r[1]} for r in postcode_rows]

    recent_rows = await db.execute_fetchall("""
        SELECT uid, address, description, app_type, app_size, app_state, start_date, postcode
        FROM applications WHERE start_date != '' ORDER BY start_date DESC LIMIT 10
    """)
    recent = [dict(zip(["uid","address","description","app_type","app_size","app_state","start_date","postcode"], r)) for r in recent_rows]

    home_title = "Leeds Planning Applications — Track What's Being Built"
    home_desc = f"Track {stats['total']} planning applications in Leeds this year. Extensions, new builds, loft conversions, and more."
    html = tpl.render(title=home_title,
                      meta_description=home_desc,
                      canonical_url="/", stats=stats, top_types=top_types,
                      postcode_areas=postcode_areas, recent=recent, breadcrumbs=[],
                      hide_breadcrumb=True,
                      jsonld=page_jsonld(
                          home_title, home_desc, "/",
                          extra=[{"@type": "Place", "name": "Leeds",
                                  "address": {"@type": "PostalAddress",
                                              "addressLocality": "Leeds",
                                              "addressRegion": "West Yorkshire",
                                              "addressCountry": "GB"}}]))
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
        if app.get("postcode"):
            prefix = app["postcode"].split()[0]          # outcode, e.g. "LS20"
            near_rows = await db.execute_fetchall(
                "SELECT uid, address, app_type FROM applications WHERE postcode LIKE ? AND uid != ? LIMIT 5",
                (f"{prefix} %", app["uid"])
            )
            nearby = [{"uid": r[0], "address": r[1], "app_type": r[2]} for r in near_rows]

        title = build_title(app.get("address", ""), app.get("app_type", ""))
        app_path = f"/application/{app['uid'].replace('/','_')}/"
        app_desc = f"{app.get('description','')[:150]}. {app.get('app_state','')}. Reference: {app.get('uid','')}."

        # Visible breadcrumb + matching JSON-LD.  These are ~7k internal links
        # into the postcode and type hubs, which is what we want crawled.
        # base.html renders the leading "Home" link itself, so `crumbs` omits it.
        crumbs = []
        crumb_pairs = [("Home", "/")]
        if app.get("postcode"):
            oc = app["postcode"].split()[0]
            crumbs.append({"url": f"/postcode/{oc.lower()}/", "label": oc.upper()})
            crumb_pairs.append((oc.upper(), f"/postcode/{oc.lower()}/"))
        if app.get("app_type"):
            slug = slugify(app["app_type"])
            crumbs.append({"url": f"/{slug}/", "label": app["app_type"]})
            crumb_pairs.append((app["app_type"], f"/{slug}/"))
        crumb_pairs.append((app["uid"], app_path))

        place = {"@type": "Place", "name": app.get("address") or "Leeds"}
        address = {"@type": "PostalAddress", "addressLocality": "Leeds",
                   "addressRegion": "West Yorkshire", "addressCountry": "GB"}
        if app.get("address"):
            address["streetAddress"] = app["address"]
        if app.get("postcode"):
            address["postalCode"] = app["postcode"]
        place["address"] = address
        try:
            if app.get("latitude") and app.get("longitude"):
                place["geo"] = {"@type": "GeoCoordinates",
                                "latitude": float(app["latitude"]),
                                "longitude": float(app["longitude"])}
        except (TypeError, ValueError):
            pass

        html = tpl.render(
            title=title,
            meta_description=app_desc,
            canonical_url=app_path,
            app=app, nearby=nearby, breadcrumbs=crumbs,
            current_crumb=app["uid"],
            is_construction=is_construction(app),
            affiliate_link=AFFILIATE_LINK,
            jsonld=page_jsonld(title, app_desc, app_path,
                               crumbs=crumb_pairs, extra=[place]),
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

            ld_title = f"{app_type} Planning Applications — Leeds"
            ld_desc = f"{total} {app_type.lower()} planning applications in Leeds this year. Track decisions and see locations."
            ld_path = f"/{slug}/" + (f"page/{page}/" if page > 1 else "")
            html = tpl.render(
                title=ld_title,
                meta_description=ld_desc,
                canonical_url=ld_path,
                heading=f"{app_type} Applications in Leeds",
                total=len(rows), current_page=page,
                total_pages=max(1, math.ceil(len(rows)/PAGE_SIZE)),
                base_url=f"/{slug}/",
                applications=[dict(zip(["uid","address","description","app_type","app_size","app_state","start_date","decided_date","postcode"], b)) for b in batch],
                breadcrumbs=[], current_crumb=app_type,
                sorted_by="date (newest first)",
                related_links=[{"url": f"/postcode/", "label": "Browse by postcode area"}],
                jsonld=page_jsonld(
                    ld_title, ld_desc, ld_path,
                    crumbs=[("Home", "/"), (app_type, f"/{slug}/")]
                           + ([(f"Page {page}", ld_path)] if page > 1 else [])),
            )
            write_html(dir / subdir / "index.html" if subdir else dir / "index.html", html)
    print(f"  {len(types)} type pages")


async def build_postcode_pages(db: aiosqlite.Connection):
    print("Building postcode pages...")
    tpl = env.get_template("listing.html")

    areas = await db.execute_fetchall(f"""
        SELECT {OUTCODE_SQL} as code, COUNT(*) as cnt
        FROM applications WHERE postcode != ''
        GROUP BY code ORDER BY cnt DESC
    """)

    # Build postcode index as a proper grid page - not using listing template
    cards = "\n".join(
        f'<a href="/postcode/{code.lower()}/" class="postcode-card"><strong>{code}</strong><span>{cnt} applications</span></a>'
        for code, cnt in areas
    )
    pc_ld = page_jsonld(
        "Planning Applications by Postcode — Leeds",
        "Browse planning applications by Leeds postcode area. Find what's being built in LS1, LS6, LS8 and more.",
        "/postcode/", crumbs=[("Home", "/"), ("Postcodes", "/postcode/")])
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Planning Applications by Postcode — Leeds</title>
    <meta name="description" content="Browse planning applications by Leeds postcode area. Find what's being built in LS1, LS6, LS8 and more.">
    <link rel="canonical" href="{SITE_URL}/postcode/">
    <script type="application/ld+json">{pc_ld}</script>
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
            "SELECT uid, address, description, app_type, app_size, app_state, start_date, decided_date, postcode FROM applications WHERE postcode = ? OR postcode LIKE ? ORDER BY start_date DESC",
            (code, f"{code} %")
        )
        dir = OUTPUT_DIR / "postcode" / code.lower()

        for page in range(1, max(1, math.ceil(len(rows) / PAGE_SIZE)) + 1):
            batch = rows[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
            subdir = f"page/{page}" if page > 1 else ""

            pc_title = f"Planning Applications in {code.upper()} — Leeds"
            pc_desc = f"{len(rows)} planning applications in {code.upper()} Leeds. Extensions, new builds, tree works and more."
            pc_path = f"/postcode/{code.lower()}/" + (f"page/{page}/" if page > 1 else "")
            html = tpl.render(
                title=pc_title,
                meta_description=pc_desc,
                canonical_url=pc_path,
                heading=f"Planning in {code.upper()}",
                total=len(rows), current_page=page,
                total_pages=max(1, math.ceil(len(rows)/PAGE_SIZE)),
                base_url=f"/postcode/{code.lower()}/",
                applications=[dict(zip(["uid","address","description","app_type","app_size","app_state","start_date","decided_date","postcode"], b)) for b in batch],
                breadcrumbs=[{"url":"/postcode/","label":"Postcodes"}], current_crumb=code.upper(),
                sorted_by="date (newest first)",
                related_links=[],
                jsonld=page_jsonld(
                    pc_title, pc_desc, pc_path,
                    crumbs=[("Home", "/"), ("Postcodes", "/postcode/"),
                            (code.upper(), f"/postcode/{code.lower()}/")]
                           + ([(f"Page {page}", pc_path)] if page > 1 else [])),
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
    about_ld = page_jsonld(
        "About — Leeds Planning",
        "About Leeds Planning — tracking planning applications across Leeds from public council data.",
        "/about/", crumbs=[("Home", "/"), ("About", "/about/")])
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>About — Leeds Planning</title>
    <meta name="description" content="About Leeds Planning — tracking planning applications across Leeds from public council data.">
    <link rel="canonical" href="{SITE_URL}/about/">
    <script type="application/ld+json">{about_ld}</script>
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
</html>""".format(SITE_URL=SITE_URL, ts=ts, nav_links=nav_links, about_ld=about_ld)
    write_html(OUTPUT_DIR / "about" / "index.html", html)


async def build_sitemap(db: aiosqlite.Connection):
    print("Building sitemap...")
    today = time.strftime("%Y-%m-%d", time.gmtime())

    # url -> lastmod.  Google weighs lastmod when deciding what to recrawl;
    # this site rebuilds daily, so without it every one of the ~7k URLs looks
    # equally stale and the crawl is spread blindly across all of them.
    # Application pages use the council record's own last-changed date (the
    # page content only changes when the record does); hubs change daily.
    urls = {f"{SITE_URL}/": today, f"{SITE_URL}/about/": today, f"{SITE_URL}/postcode/": today}

    rows = await db.execute_fetchall(
        "SELECT uid, last_changed, start_date, decided_date FROM applications")
    for uid, last_changed, start_date, decided_date in rows:
        stamp = str(last_changed or start_date or decided_date or "")
        urls[f"{SITE_URL}/application/{uid.replace('/','_')}/"] = stamp[:10] or today

    types = await db.execute_fetchall("SELECT DISTINCT app_type FROM applications")
    for (t,) in types:
        urls[f"{SITE_URL}/{slugify(t)}/"] = today

    areas = await db.execute_fetchall(f"SELECT DISTINCT {OUTCODE_SQL} FROM applications WHERE postcode != ''")
    for (code,) in areas:
        urls[f"{SITE_URL}/postcode/{code.lower()}/"] = today

    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url in sorted(urls):
        xml.append(f"  <url><loc>{url}</loc><lastmod>{urls[url]}</lastmod></url>")
    xml.append("</urlset>")
    write_html(OUTPUT_DIR / "sitemap.xml", "\n".join(xml) + "\n")
    print(f"  {len(urls)} URLs")


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