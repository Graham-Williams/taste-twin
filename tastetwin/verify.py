"""Live verification of dataset-ranked matches.

The Kaggle pool is a ~Oct 2023 snapshot, so the ranking it produces is
stale. This stage re-scrapes the CURRENT public ratings of the top-ranked
candidates (politely: 1 req/s, cached, capped pages), recomputes the
similarity on fresh data, and drops accounts that are gone or private.
"""

from __future__ import annotations

import logging

from .scraper import PoliteSession, fetch_user_ratings
from .similarity import Match, compare, zscores

log = logging.getLogger("tastetwin")


def verify_matches(session: PoliteSession, target: dict[str, float],
                   matches: list[Match], top_n: int = 50,
                   max_pages: int = 10,
                   min_overlap: int = 15) -> list[Match]:
    """Re-score the top_n matches against their live ratings.

    Returns fresh Match objects (source='live', dataset_score preserved),
    sorted by fresh score. Dead/private/renamed accounts are dropped, as
    are candidates whose fresh overlap falls below min_overlap.
    """
    shortlist = matches[:top_n]
    est = len(shortlist) * max_pages
    log.info("Verifying top %d matches live (worst case ~%d requests "
             "≈ %.0f min at 1 req/s)...", len(shortlist), est, est / 60)

    tz = zscores(target)
    verified: list[Match] = []
    dropped = 0
    for i, m in enumerate(shortlist, 1):
        ratings = fetch_user_ratings(session, m.username, max_pages=max_pages)
        if not ratings:
            log.info("  [%d/%d] %s: gone or private — dropped",
                     i, len(shortlist), m.username)
            dropped += 1
            continue
        fresh = {slug: float(info["rating"]) for slug, info in ratings.items()}
        fm = compare(target, fresh, min_overlap=min_overlap, target_z=tz)
        if fm is None:
            log.info("  [%d/%d] %s: fresh overlap below threshold — dropped",
                     i, len(shortlist), m.username)
            dropped += 1
            continue
        fm.username = m.username
        fm.source = "live"
        fm.dataset_score = m.score
        verified.append(fm)
        log.info("  [%d/%d] %s: dataset %.3f -> fresh %.3f (r %.3f, "
                 "overlap %d)", i, len(shortlist), m.username,
                 m.score, fm.score, fm.pearson, fm.overlap)

    verified.sort(key=lambda m: -m.score)
    log.info("Verification done: %d confirmed, %d dropped.",
             len(verified), dropped)
    return verified
