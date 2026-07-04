# taste-twin

Find Letterboxd users whose movie taste is most similar to yours, using only
public profile data.

Given a Letterboxd username, taste-twin:

1. **Scrapes** the user's public ratings (politely: 1 request/second,
   identified User-Agent, on-disk cache).
2. **Discovers** a pool of candidate users by looking at who else rated the
   user's favorite films — weighted toward *obscure* favorites, which are the
   strongest taste signal.
3. **Collects** each candidate's ratings (bounded per candidate, resumable if
   interrupted).
4. **Scores** every candidate with a Pearson correlation over co-rated films,
   after per-user z-score normalization (so a harsh 3-star-max rater can still
   match a generous 5-star rater), with significance weighting so tiny overlaps
   can't win.
5. **Reports** the top matches as `report.md` and a standalone `report.html`:
   score, overlap, films you both love, biggest disagreements.

## Requirements

- Python 3.11+
- `requests`, `beautifulsoup4` (see `requirements.txt`)

## Setup

```bash
python3 -m venv .venv           # or: uv venv --python 3.12
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

End-to-end:

```bash
python -m tastetwin run <letterboxd-username>
```

Useful options:

```bash
python -m tastetwin run <user> --pool 1000 --min-overlap 15 --max-pages 10
```

- `--pool N` — target number of candidate users to score (default 1000)
- `--min-overlap N` — minimum co-rated films to consider a match (default 15)
- `--max-pages N` — max ratings pages fetched per candidate (default 10,
  ≈720 films)

Stages can also be run individually (each stage reuses cached/stored output
from the previous one):

```bash
python -m tastetwin fetch <user>      # scrape the target's ratings
python -m tastetwin discover <user>   # build the candidate pool
python -m tastetwin collect <user>    # fetch candidates' ratings (resumable)
python -m tastetwin analyze <user>    # score + write report.md / report.html
```

Output lands in `data/runs/<user>/` (`report.md`, `report.html`).

**Heads up on runtime:** the scraper is deliberately single-threaded at
~1 request/second. A full run with `--pool 1000` makes several thousand HTTP
requests and takes **hours**. The CLI prints an ETA up front, and a killed run
resumes where it left off (everything is cached under `data/cache/`).

## Politeness policy

This project only reads public pages, and does so gently:

- ≥1 second between requests, single-threaded, no parallel fetching
- Custom User-Agent identifying the project
- Retry with exponential backoff on 429/5xx, honoring `Retry-After`
- On-disk cache with a freshness TTL — reruns don't re-fetch
- robots.txt disallows are respected

## Tests

```bash
python -m pytest
```

Parsing tests run against saved HTML fixtures (no network); similarity tests
use hand-computed cases.

## License

MIT
