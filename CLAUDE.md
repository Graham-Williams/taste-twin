# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

taste-twin: a Python pipeline that finds Letterboxd users with movie taste
similar to a given user, from public data only. Dataset-first design: the
candidate pool is the CC0 Kaggle dataset `freeth/letterboxd-film-ratings`
(~11k users / ~18.2M ratings, Oct 2023 snapshot, scraped from public pages
by github.com/adamjhf/letterboxd-scraper); only the target's ratings and the
top matches' verification are scraped live. See `README.md` for the
user-facing overview.

## Stack

- Python 3.11+ (no 3.11+ interpreter on the system? use `uv venv --python 3.12`)
- Plain `venv`; runtime deps in `requirements.txt` (kept minimal:
  `requests>=2.32.4`, `beautifulsoup4`, `kagglehub` — dataset download works
  for this public dataset WITHOUT Kaggle credentials); dev deps in
  `requirements-dev.txt` (`pytest`). Similarity math is stdlib — no
  numpy/pandas.
- Web deps: `flask` (app), `gunicorn` (serving), `PyJWT[crypto]`
  (Cloudflare Access JWT verification).
- Package `tastetwin/`:
  - `scraper.py` — polite HTTP (PoliteSession: 1 req/s, UA, backoff,
    disk cache, robots.txt) + HTML parsing. Selector notes live in its
    docstring; fixtures in `tests/fixtures/` are saved real pages.
  - `ingest.py` — kagglehub download (cached under `~/.cache/kagglehub/`,
    NOT `data/`; manual fallback: unzip CSVs into `data/dataset/`) →
    streamed load into SQLite `data/pool.db` (~1.6 GB, ~45 s) with
    film/user indexes + a precomputed `user_stats` table (per-user
    mean/var for z-scoring overlap slices).
  - `similarity.py` — per-user z-scores, Pearson over co-rated films,
    significance weighting `r * min(overlap,50)/50`. Dataset ratings are
    stored as half-stars (1–10) to match the scraper.
  - `verify.py` — re-scores top dataset matches against their live ratings;
    drops dead/private accounts.
  - `discover.py` / `collect.py` — OPTIONAL scrape-based candidate
    discovery (obscure-favorite seeds → film members pages); no longer the
    default path; analyze merges any collected scraped users automatically.
  - `report.py` — report.md + standalone report.html (inline CSS).
  - `pipeline.py` — the stage implementations (fetch/analyze/verify/
    discover/collect + `run_full`), shared by the CLI and the web app.
    Stages raise `PipelineError` (never `sys.exit`) on user-visible
    failures.
  - `__main__.py` — CLI (argparse; catches `PipelineError`).
  - `web/` — Flask app (see "Web app" below): `app.py` (factory
    `create_app`, routes, security middleware), `auth.py` (Cloudflare
    Access JWT verification, JWKS cached with TTL), `jobs.py`
    (`JobManager`: FIFO queue + single worker thread), `templates/`.
- All bulky/local state under `data/` (gitignored): `cache/` (HTTP),
  `pool.db`, `dataset/` (manual-fallback CSVs only), `runs/<user>/`
  (target.json, matches_*.json, report.md/html). Exception: the raw
  kagglehub download itself is cached under `~/.cache/kagglehub/`, not
  `data/`.

## Security hardening (keep these invariants)

- Scraped/CSV usernames and film slugs are validated against
  `^[A-Za-z0-9_-]+$` (`scraper.is_valid_name`) and silently dropped (debug
  log) before ANY URL is built — in the parsers, the high-level fetchers,
  and `discover._raters_page`. Never interpolate an unvalidated remote
  string into a request URL.
- `PoliteSession._fetch_raw` rejects responses whose final URL (after
  redirects) is not letterboxd.com or a subdomain, and caps response
  bodies at 5 MB (streamed read).
- `Retry-After` sleeps are clamped to 300 s, and the header is parsed
  defensively (`.isdecimal()` + try/except — `"²".isdigit()` is True but
  `float()` raises); any unparseable value falls back to normal backoff.
- A per-candidate `ScrapeError` (off-site redirect, oversized body, retry
  exhaustion) is caught inside the `verify.py` / `collect.py` loops: the
  candidate is dropped with a warning, the run continues. Only
  `MAX_CONSECUTIVE_FAILURES` (5) failures in a row — a site-wide problem —
  abort the stage. In collect, a failed candidate writes no ratings file,
  so a resumed run retries it. Regression tests in
  `tests/test_resilience.py`.
- Report output escapes everything remote-derived: HTML hrefs are
  URL-encoded then HTML-escaped; Markdown link text escapes
  ``\[]()<>` `` and URLs are percent-encoded (`report._md_text`,
  `_film_url`, `_profile_url`). All whitespace runs in report text —
  including `\r`, `\n`, `\v`, `\f`, U+0085/U+2028/U+2029 — are flattened
  to a single space (`report._flatten_ws`, applied in both Markdown and
  HTML renderers) so a newline in a title can never hoist block-level
  Markdown (headings, bare URLs) out of its link text. Regression tests
  in `tests/test_report.py` and `tests/test_security.py`.
- Untrusted names used as path components go through `util.safe_filename`
  (shared by `collect.py`, `pipeline.py`, and the web app).

## Web app (keep these invariants too)

`tastetwin/web/` wraps the pipeline in a small Flask app (server-rendered,
meta-refresh polling, no JS). Routes: `/` (runs list + start form),
`POST /run` (enqueue), `/run/<username>` (live status: queue position /
stage / log tail / re-run on failure), `/report/<username>` (serves the
generated report.html inline), `/about` (methodology), `/healthz`
(unauthenticated healthcheck, leaks nothing).

- **Queue:** ONE worker thread, strict FIFO, one job at a time, pending
  cap 5 (`jobs.MAX_PENDING`) — the 1 req/s politeness budget is global to
  the process, so jobs must never scrape concurrently. Consequently
  gunicorn runs with `--workers 1` (threads for request concurrency) and
  no `--max-requests`. Never scale to multiple processes/replicas without
  moving the queue out of process.
- **Job state** persists to `data/runs/<user>/job.json` + `job.log`; on
  boot `JobManager.recover()` marks jobs left queued/running by a dead
  process as failed (UI offers Re-run; the HTTP cache makes that cheap).
  First boot: the worker ingests the Kaggle dataset automatically if
  `data/pool.db` is missing (in Docker, `KAGGLEHUB_CACHE` points inside
  the `data/` volume).
- **Auth:** no built-in auth. When `CF_ACCESS_AUD` + `CF_ACCESS_TEAM_DOMAIN`
  are set, every route except `/healthz` requires a valid Cloudflare
  Access JWT (`Cf-Access-Jwt-Assertion` header or `CF_Authorization`
  cookie): signature vs the team JWKS (cached 1 h, stale-on-refresh-failure,
  otherwise fail CLOSED), plus `aud`/`iss`/`exp` checks. Both vars unset =
  dev mode with a loud log warning. `APP_HOST`, when set, pins the Host
  header on all routes and the Origin header on POSTs (CSRF defense).
- **Input:** the only user input is the username — validated with
  `scraper.is_valid_name` (+ a 64-char bound) before ANY use, and passed
  through `util.safe_filename` before touching paths. `/report/` serves
  only the fixed filename `report.html` under the sanitized run dir, with
  a resolved-path containment check. Jinja autoescape stays on; nothing
  remote-derived is ever `|safe`. Security headers (CSP, nosniff,
  X-Frame-Options) are set on every response.
- Web tests: `tests/test_web_auth.py` (JWT/JWKS), `tests/test_web_routes.py`
  (validation, pinning, report serving), `tests/test_web_jobs.py` (queue
  semantics with a mocked runner).

Deployment (Docker, Cloudflare tunnel + Access, first-boot ingest) is
documented in `DEPLOY.md`.

## Run / test

```bash
# setup
python3 -m venv .venv && source .venv/bin/activate   # or: uv venv --python 3.12
pip install -r requirements-dev.txt   # dev deps include -r requirements.txt

# end-to-end (default: fetch → ingest → analyze → verify → report)
python -m tastetwin run <user> [--verify-top 50] [--min-overlap 15] [--max-pages 10]

# stages
python -m tastetwin ingest           # one-time; ~3 min download + ~1 min build
python -m tastetwin fetch <user>
python -m tastetwin analyze <user>   # full 11k pool in ~6 s
python -m tastetwin verify <user> [--verify-top N] [--max-pages N]

# optional scraped supplement (slow: hours for --pool 1000 at 1 req/s)
python -m tastetwin discover <user> [--pool N]
python -m tastetwin collect <user>

# tests — fixtures + hand-computed math + synthetic-CSV ingest + web
# (fake JWKS, mocked pipeline runner); no network
python -m pytest

# web app, local dev (unauthenticated — logs a warning)
flask --app tastetwin.web run --port 8080

# web app, production-style
gunicorn --workers 1 --threads 8 -b 0.0.0.0:8080 "tastetwin.web:create_app()"
```

Smoke-test recipe (small + polite): `fetch <user>` → `analyze <user>` →
`verify <user> --verify-top 3 --max-pages 2`.

## Polite-scraping policy (hard requirement)

Never weaken these without explicit approval:

- ≥1 second minimum delay between HTTP requests; single-threaded fetching only
- Custom User-Agent identifying the project (`taste-twin/x.y personal project`)
- Retry with exponential backoff on 429/5xx; honor `Retry-After`
- On-disk cache (`data/cache/`, TTL-based) so reruns never re-fetch fresh pages
- Respect robots.txt disallows for any path we fetch (note: `/*/by/*` sort
  URLs are disallowed — never fetch those)
- Prefer the dataset over bulk scraping; live scraping is for the target +
  top-match verification.
- Letterboxd serves an HTTP/2-fingerprint Cloudflare challenge to some
  clients; plain HTTP/1.1 (python-requests' default) is served normally.
  Don't "fix" a 403 by faking browser fingerprints.
- Letterboxd's HTML is not an API — selectors live in `scraper.py` with test
  fixtures in `tests/fixtures/`; if Letterboxd changes markup, update the
  fixtures from a freshly saved real page, never by hand-editing.

## Git workflow

- All work happens on **feature branches** (`feature/<name>`); commit freely
  there.
- `main` is **protected**: no direct pushes, no force-push. Changes reach
  `main` only via a Pull Request that Graham reviews, approves, and merges
  himself. Never merge a PR to `main` on his behalf.

## Self-maintenance

When you add or change a capability, dependency, command, or architectural
decision, update this CLAUDE.md before the task is done. This file is how
context persists for the next agent/session that enters the repo.
