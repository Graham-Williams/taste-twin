# taste-twin

Find Letterboxd users whose movie taste is most similar to yours, using only
public data.

Given a Letterboxd username, taste-twin:

1. **Fetches** the user's current public ratings from their profile
   (politely: 1 request/second, identified User-Agent, on-disk cache).
2. **Ranks** them against a candidate pool of ~11,000 of Letterboxd's most
   active members — the CC0 Kaggle dataset
   [freeth/letterboxd-film-ratings](https://www.kaggle.com/datasets/freeth/letterboxd-film-ratings)
   (~18M ratings, collected from public pages by
   [adamjhf/letterboxd-scraper](https://github.com/adamjhf/letterboxd-scraper)),
   loaded once into a local SQLite database. Scoring all 11k users takes
   seconds.
3. **Verifies live**: the dataset is a snapshot (Oct 2023), so the top
   matches are re-scored against their *current* public ratings; dead and
   private accounts are dropped.
4. **Reports** the final matches as `report.md` and a standalone
   `report.html`: score, Pearson r, overlap, data freshness, films you both
   love, biggest disagreements.

The similarity metric is a Pearson correlation over co-rated films after
per-user z-score normalization (so a harsh 3-star-max rater can still match a
generous 5-star rater), with significance weighting
(`score = r * min(overlap, 50) / 50`) so tiny overlaps can't win.

taste-twin can be used two ways: a **CLI** (below) or a small self-hostable
**web app** (see [Web app](#web-app)).

## Requirements

- Python 3.11+
- `requests`, `beautifulsoup4`, `kagglehub`, plus `flask` / `gunicorn` /
  `PyJWT` for the web app (see `requirements.txt`); dev/test tooling
  (`pytest`) lives in `requirements-dev.txt`
- ~2.3 GB disk for the dataset + SQLite pool. The `kagglehub` download is
  cached under `~/.cache/kagglehub/`; `data/` (gitignored) holds the built
  `data/pool.db` and, only if you use the manual fallback, the CSVs in
  `data/dataset/`

## Setup

```bash
python3 -m venv .venv           # or: uv venv --python 3.12
source .venv/bin/activate
pip install -r requirements.txt      # runtime deps
pip install -r requirements-dev.txt  # optional: adds pytest for the test suite
```

## Usage

End-to-end:

```bash
python -m tastetwin run <letterboxd-username>
```

The first run downloads the Kaggle dataset via `kagglehub` (no Kaggle
account needed; the download is cached under `~/.cache/kagglehub/`) and
builds `data/pool.db` (~1 minute). If the automatic download ever fails,
download the zip from the dataset page and unzip `ratings.csv` +
`films.csv` into `data/dataset/`.

Useful options:

```bash
python -m tastetwin run <user> --verify-top 50 --min-overlap 15 --max-pages 10
```

- `--verify-top N` — how many top matches to re-verify live (default 50)
- `--min-overlap N` — minimum co-rated films to consider a match (default 15)
- `--max-pages N` — max ratings pages fetched per verified user (default 10,
  ≈720 films)

Stages can also be run individually:

```bash
python -m tastetwin ingest            # download dataset, build data/pool.db
python -m tastetwin fetch <user>      # scrape the target's current ratings
python -m tastetwin analyze <user>    # rank all ~11k pool users (offline, fast)
python -m tastetwin verify <user>     # re-score top matches live + report
```

Output lands in `data/runs/<user>/` (`report.md`, `report.html`).

**Runtime:** analyze is seconds; live verification of the top 50 at
1 request/second is roughly 10–30 minutes. Interrupting is safe — all pages
are cached, so reruns resume where they left off.

### Optional: scrape-based discovery

The dataset pool skews toward very active accounts. To hunt for candidates
outside it, an optional discovery mode finds users through the target's
*obscure favorites* (high rating × low global popularity — the strongest
taste signal), then collects their ratings:

```bash
python -m tastetwin discover <user> --pool 1000   # find candidate usernames
python -m tastetwin collect <user>                # fetch their ratings (slow!)
python -m tastetwin analyze <user>                # merges them into the ranking
```

Fair warning: collecting a 1000-user scraped pool at 1 request/second takes
**hours**. The collector is resumable (progress is saved per candidate).

## Web app

A minimal Flask app wraps the same pipeline — no JavaScript frameworks, just
server-rendered pages with meta-refresh polling:

- `/` — completed runs + a form to start a new run for any username
- `POST /run` — enqueue an analysis (FIFO queue, one job at a time — the
  1 req/s politeness budget is global, so jobs never scrape concurrently)
- `/run/<username>` — live status: queue position, current stage, log tail
- `/report/<username>` — the generated `report.html`
- `/about` — plain-English methodology

Local dev (unauthenticated; the app logs a warning):

```bash
flask --app tastetwin.web run --port 8080
```

For self-hosting there's a `Dockerfile` + `docker-compose.yml` (gunicorn,
non-root, single worker process, all state in a `data/` volume; the first
boot ingests the Kaggle dataset automatically). The app is designed to sit
behind [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/)
and verifies the Access JWT in-app when `CF_ACCESS_AUD` +
`CF_ACCESS_TEAM_DOMAIN` are set (plus a Host/Origin pin via `APP_HOST`).
See `DEPLOY.md` for the runbook.

### View-only mode

Set **`TASTE_TWIN_VIEWER_MODE=1`** to run the app as a read-only report
gallery: the homepage hides the username form, `POST /run` is refused, and no
background worker or dataset ingest runs (so `pool.db` isn't needed). This is
how the hosted instance runs, because Letterboxd's Cloudflare bot management
challenges a server IP and blocks live scraping from the box. New reports are
generated on a residential-IP machine and pushed to the box with
`scripts/publish.py <username>` (which runs the pipeline locally, then copies
`report.html` + `matches_verified.json` into the container). The flag defaults
off, so a normal full-mode instance is unchanged. See `DEPLOY.md`.

## Politeness policy

This project only reads public pages, and does so gently:

- ≥1 second between requests, single-threaded, no parallel fetching
- Custom User-Agent identifying the project
- Retry with exponential backoff on 429/5xx, honoring `Retry-After`
- On-disk cache with a freshness TTL — reruns don't re-fetch
- robots.txt disallows are respected
- The bulk candidate pool comes from an existing public CC0 dataset instead
  of mass scraping

## Tests

```bash
pip install -r requirements-dev.txt   # once, for pytest
python -m pytest
```

Parsing tests run against saved HTML fixtures (no network); similarity tests
use hand-computed cases; ingest tests use synthetic CSVs; web tests cover the
Access-JWT middleware (against a fake JWKS), host pinning, input validation,
report serving, and queue semantics with a mocked pipeline.

## Credits

- Candidate pool: [Letterboxd film ratings](https://www.kaggle.com/datasets/freeth/letterboxd-film-ratings)
  by freeth on Kaggle (CC0), built with
  [adamjhf/letterboxd-scraper](https://github.com/adamjhf/letterboxd-scraper).
- Not affiliated with Letterboxd. Be kind to their servers.

## License

MIT
