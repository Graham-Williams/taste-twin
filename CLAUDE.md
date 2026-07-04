# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

taste-twin: a Python pipeline that finds Letterboxd users with movie taste
similar to a given user, from public profile data only. See `README.md` for
the user-facing overview.

## Stack

- Python 3.11+ (no 3.11+ interpreter on the system? use `uv venv --python 3.12`)
- Plain `venv` + `requirements.txt`; deps kept minimal: `requests`,
  `beautifulsoup4`, `pytest` (dev). Similarity math is stdlib
  (`statistics`/`math`) — no numpy/pandas.
- Package: `tastetwin/` — `scraper.py` (polite HTTP + HTML parsing),
  `discover.py` (candidate pool from seed films), `collect.py` (candidate
  ratings collection, resumable), `similarity.py` (z-score + Pearson +
  significance weighting), `report.py` (report.md / report.html),
  `__main__.py` (CLI).
- All scraped pages cached under `data/cache/` (gitignored); run artifacts
  under `data/runs/<user>/`; progress state under `data/` makes runs
  resumable.

## Run / test

```bash
# setup
python3 -m venv .venv && source .venv/bin/activate   # or: uv venv --python 3.12
pip install -r requirements.txt                      # or: uv pip install -r requirements.txt

# end-to-end
python -m tastetwin run <letterboxd-username> [--pool N] [--min-overlap N] [--max-pages N]

# individual stages
python -m tastetwin fetch <user>
python -m tastetwin discover <user>
python -m tastetwin collect <user>
python -m tastetwin analyze <user>

# tests (fixture-based parsing tests + similarity math; no network)
python -m pytest
```

A full `--pool 1000` run takes hours by design (1 req/s). For a quick live
check use `--pool 30 --max-pages 2`.

## Polite-scraping policy (hard requirement)

Never weaken these without explicit approval:

- ≥1 second minimum delay between HTTP requests; single-threaded fetching only
- Custom User-Agent identifying the project (`taste-twin/x.y personal project`)
- Retry with exponential backoff on 429/5xx; honor `Retry-After`
- On-disk cache (`data/cache/`, TTL-based) so reruns never re-fetch fresh pages
- Respect robots.txt disallows for any path we fetch
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
