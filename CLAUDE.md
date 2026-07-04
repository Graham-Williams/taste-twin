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
- Plain `venv` + `requirements.txt`; deps kept minimal: `requests`,
  `beautifulsoup4`, `kagglehub` (dataset download — works for this public
  dataset WITHOUT Kaggle credentials), `pytest` (dev). Similarity math is
  stdlib — no numpy/pandas.
- Package `tastetwin/`:
  - `scraper.py` — polite HTTP (PoliteSession: 1 req/s, UA, backoff,
    disk cache, robots.txt) + HTML parsing. Selector notes live in its
    docstring; fixtures in `tests/fixtures/` are saved real pages.
  - `ingest.py` — kagglehub download (manual fallback: unzip CSVs into
    `data/dataset/`) → streamed load into SQLite `data/pool.db` (~1.6 GB,
    ~45 s) with film/user indexes + a precomputed `user_stats` table
    (per-user mean/var for z-scoring overlap slices).
  - `similarity.py` — per-user z-scores, Pearson over co-rated films,
    significance weighting `r * min(overlap,50)/50`. Dataset ratings are
    stored as half-stars (1–10) to match the scraper.
  - `verify.py` — re-scores top dataset matches against their live ratings;
    drops dead/private accounts.
  - `discover.py` / `collect.py` — OPTIONAL scrape-based candidate
    discovery (obscure-favorite seeds → film members pages); no longer the
    default path; analyze merges any collected scraped users automatically.
  - `report.py` — report.md + standalone report.html (inline CSS).
  - `__main__.py` — CLI.
- All bulky/local state under `data/` (gitignored): `cache/` (HTTP),
  `pool.db`, `dataset/` (manual CSVs), `runs/<user>/` (target.json,
  matches_*.json, report.md/html).

## Run / test

```bash
# setup
python3 -m venv .venv && source .venv/bin/activate   # or: uv venv --python 3.12
pip install -r requirements.txt

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

# tests — fixtures + hand-computed math + synthetic-CSV ingest; no network
python -m pytest
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
