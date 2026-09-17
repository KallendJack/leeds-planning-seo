# Data Ingestion & Static Generation Pipeline

An end-to-end pipeline that ingests a rate-limited public planning API into SQLite
and generates a ~7,000-page static site from it. It runs unattended on a home
server, with staged deploys and overlap-guarded cron.

**Live:** [leedsplanning.org.uk](https://leedsplanning.org.uk)

**Stack:** Python 3.13, aiohttp, aiosqlite, SQLite, Jinja2, Caddy

---

## Why it exists

Leeds City Council publishes planning applications through a third-party portal
(PlanIt) that offers no bulk export and no usable search. Finding out what is being
built on a given street means clicking through it by hand.

This project turns that into a queryable dataset and a set of indexable pages: one
per application, plus hubs per postcode outcode and per application type.

## Architecture

```
 PlanIt API --> ingest_careful.py --> data/leeds_planning.db (SQLite)
                   adaptive pacer          |
                   truncation guard        v
                                   generate.py (Jinja2)
                                          |
                                          v
                              output_staging/ --> output/   <-- Caddy
                                 (build)         (overlay + prune)
```

- **Ingest** is async (`aiohttp` + `aiosqlite`) and self-pacing. The API replies
  `Rate limits exceeded / try again in Ns`, and the pacer turns that into a
  sustainable interval instead of blocking for the whole advertised window.
- **Generate** renders from the DB with Jinja2. Templates never touch the DB, and
  the sitemap and JSON-LD come out of the same pass over the data.
- **Deploy** never swaps the served directory. A bind-mounted `output/` has to keep
  its inode, so a new build is overlaid onto the existing tree and stale files are
  pruned. A crash mid-build leaves the live site intact.

## Pipeline

| Script | Purpose |
|---|---|
| `ingest_careful.py` | Fetches PlanIt application listings into `data/leeds_planning.db` (SQLite). Rate-limit aware. |
| `generate.py` | Renders the static site from the DB into `output/` using `templates/` + `static/`. Emits `sitemap.xml` with per-URL `<lastmod>` and schema.org JSON-LD. |
| `build_staging.py` + `deploy_staging.py` | Safe rebuild for the live site: builds into `output_staging/`, then overlays it onto `output/` without replacing the directory (a bind-mounted dir must keep its inode). Use these instead of `generate.py` when a mid-build crash must not leave the served site empty. |
| `cron.sh` | Daily entrypoint. Guards against stale or hung ingests and overlapping runs, then runs **incremental** ingest + generate. |
| `cron-full.sh` | Weekly entrypoint. Same guards, runs a **full** ingest + generate. |
| `pipeline.py` | Legacy combined script, superseded by the two above. |

### Ingest modes

- **Incremental (default):** `recent=365&changed=3`, upserts only applications changed in the last 3 days. A handful of requests, 1-2 min. This is the daily path.
- **`--full`:** re-fetches the whole 365-day window and replaces the table, dropping anything that has aged out. ~24 requests, slow because of PlanIt's rate and volume caps.
- **`--apply-cache`:** re-apply the last completed fetch (`data/last_fetch.json.gz`) without hitting the API. Use after a failed write.

### Correctness notes

- PlanIt allows a short burst, then replies `Rate limits exceeded / Volume limits exceeded, try again in Ns`. The adaptive pacer converts that into a sustainable interval (`wait ÷ burst`, floor 8 / cap 300s) rather than waiting the whole window per request.
- Page size is 300 (the API default). Avoid larger pages, the docs ask for many small requests.
- The truncation guard compares **records received** against the API total and exits non-zero if short, so a partial fetch never overwrites the live table. Duplicate UIDs are normal and collapse under the primary key.

## SEO output

- `sitemap.xml` carries a `<lastmod>` on every URL. Application pages use the
  council record's own `last_changed` date, since the page only changes when the
  record does; hubs and the postcode index use the build date. Without this
  crawlers have no signal about which of 7k URLs are worth recrawling.
- Every page emits schema.org JSON-LD: `WebSite` + `WebPage`, plus
  `BreadcrumbList` (matching the visible breadcrumb) and a `Place` node on
  application pages. Breadcrumbs on application pages are also ~7k internal
  links into the postcode and type hubs.
- Application `<title>`s are built by `build_title()`, which truncates on a word
  boundary and keeps the postcode tail. Do **not** go back to `address[:40]`,
  which chopped mid-word and shipped titles cut off like `...Scholes Leeds L`
  on every page.

## Data notes

A UK postcode's **outcode** is the part before the space (`LS20 8JB` -> `LS20`).
Never derive postcode areas with a fixed-width slice: a 3-char prefix folds
`LS20`-`LS29` into `LS2`. Grouping also includes `WF`/`BD` outcodes, because parts
of the Leeds district (Tingley, Drighlington, Rawdon) carry those postcodes.

## Schedule

| Job | When | Script |
|---|---|---|
| `leeds-planning-pipeline-daily` | daily 06:00 UTC | `cron.sh` (incremental) |
| `leeds-planning-full-reconcile` | Sunday 04:00 UTC | `cron-full.sh` (full) |

Both run as **no_agent** cron jobs (no LLM in the loop, no token cost) via
`flock` on `/tmp/leeds-planning-pipeline.lock`, and kill a stale ingest before
starting a new one, so a hung run cannot stack up behind the next schedule.

## Paths

- Project root: `/workspace/dev/leeds-planning-seo` (the NAS mount inside the container)
- DB: `data/leeds_planning.db` (gitignored, rebuildable via ingest)
- Cached fetch: `data/last_fetch.json.gz` (gitignored)
- Site output: `output/` (gitignored, regenerated by `generate.py`; wiped at the start of each build)
- Source assets: `static/` (tracked, copied into `output/static/` on build)

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# initial population (slow: full 365-day window, pacing against the API)
.venv/bin/python ingest_careful.py --full

# build the site into output/
.venv/bin/python generate.py
```

For a rebuild of the live site, use `build_staging.py` then `deploy_staging.py`
rather than `generate.py`.