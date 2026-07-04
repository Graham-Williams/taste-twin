"""Ratings collection for the candidate pool.

Resumable by construction: each candidate's parsed ratings are written to
data/runs/<target>/ratings/<candidate>.json as soon as they're collected,
and candidates with an existing file are skipped on rerun. (The HTTP cache
additionally makes any re-fetch free.)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .scraper import PoliteSession, ScrapeError, fetch_user_ratings
from .util import safe_filename

log = logging.getLogger("tastetwin")

MIN_RATINGS = 20  # candidates with fewer total ratings are skipped

# One hostile/anomalous candidate (off-site redirect, oversized body, ...)
# must not abort a long collection run — it gets dropped with a warning.
# But this many failures IN A ROW means the problem is site-wide (or we're
# blocked), so aborting beats burning the remaining request budget.
MAX_CONSECUTIVE_FAILURES = 5


def _candidate_path(ratings_dir: Path, username: str) -> Path:
    return ratings_dir / f"{safe_filename(username)}.json"


def collect_pool_ratings(session: PoliteSession, pool: list[str],
                         run_dir: Path, max_pages: int = 10) -> dict[str, dict]:
    """Fetch ratings for every candidate in the pool. Returns
    {username: {slug: {"title", "rating"}}} for candidates that pass the
    MIN_RATINGS bar."""
    ratings_dir = run_dir / "ratings"
    ratings_dir.mkdir(parents=True, exist_ok=True)

    collected: dict[str, dict] = {}
    skipped = 0
    started = time.monotonic()
    done_before = sum(
        1 for u in pool if _candidate_path(ratings_dir, u).exists())
    if done_before:
        log.info("Resuming: %d/%d candidates already collected.",
                 done_before, len(pool))

    fresh_done = 0
    consecutive_failures = 0
    for i, username in enumerate(pool, 1):
        path = _candidate_path(ratings_dir, username)
        if path.exists():
            data = json.loads(path.read_text())
        else:
            try:
                ratings = fetch_user_ratings(session, username,
                                             max_pages=max_pages)
            except ScrapeError as exc:
                consecutive_failures += 1
                skipped += 1
                # No file is written, so a rerun will retry this candidate.
                log.warning("Candidate %s: fetch failed (%s) — dropped",
                            username, exc)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise ScrapeError(
                        f"{consecutive_failures} consecutive candidate "
                        f"fetch failures — aborting collection (likely "
                        f"site-wide, last error: {exc})") from exc
                continue
            consecutive_failures = 0
            data = {"username": username, "ratings": ratings or {}}
            path.write_text(json.dumps(data))
            fresh_done += 1
            if fresh_done % 10 == 0:
                elapsed = time.monotonic() - started
                remaining = len(pool) - i
                rate = fresh_done / elapsed if elapsed > 0 else 0
                eta_min = remaining / rate / 60 if rate > 0 else float("inf")
                log.info("Collected %d/%d candidates (~%.0f min remaining)",
                         i, len(pool), eta_min)

        if len(data["ratings"]) < MIN_RATINGS:
            skipped += 1
            continue
        collected[username] = data["ratings"]

    log.info("Collection done: %d usable candidates, %d skipped "
             "(<%d ratings or private/missing).",
             len(collected), skipped, MIN_RATINGS)
    return collected
