"""Candidate discovery: pick seed films from the target's favorites, then
harvest users who rated those films.

Seed strategy: (high rating by target) x (low global popularity). Obscure
favorites are the strongest signal — millions of people rated Parasite, so
sharing it says little; sharing a 4.5-star opinion of a 3,000-watch film
says a lot. A few mainstream favorites are kept in the mix so we don't
select *only* ultra-niche viewers.
"""

from __future__ import annotations

import logging
import math
import random

from .scraper import PoliteSession, fetch_film_popularity, fetch_film_raters

log = logging.getLogger("tastetwin")

SEED_CANDIDATES_TO_PROBE = 60   # films whose popularity we look up
NUM_OBSCURE_SEEDS = 24
NUM_MAINSTREAM_SEEDS = 6
LOVED_THRESHOLD_HALFSTARS = 8   # 4 stars


def choose_seed_films(session: PoliteSession, target_ratings: dict[str, dict],
                      rng: random.Random | None = None) -> list[dict]:
    """Pick ~30 seed films for candidate discovery.

    Returns a list of {"slug", "title", "rating", "popularity", "kind"}
    sorted most-obscure-first, kind in {"obscure", "mainstream"}.
    """
    rng = rng or random.Random(29)  # deterministic; any fixed seed works
    loved = [(slug, info) for slug, info in target_ratings.items()
             if info["rating"] >= LOVED_THRESHOLD_HALFSTARS]
    if len(loved) < 10:  # sparse profile: lower the bar to 3.5 stars
        loved = [(slug, info) for slug, info in target_ratings.items()
                 if info["rating"] >= 7]
    if not loved:
        raise ValueError("target has no sufficiently-rated films to seed from")

    probe = loved
    if len(probe) > SEED_CANDIDATES_TO_PROBE:
        probe = rng.sample(loved, SEED_CANDIDATES_TO_PROBE)

    log.info("Probing popularity of %d loved films (1 req/s)...", len(probe))
    scored = []
    for i, (slug, info) in enumerate(probe, 1):
        pop = fetch_film_popularity(session, slug)
        if pop is None or pop <= 0:
            continue
        # obscurity score: rating weighted against log-popularity
        obscurity = info["rating"] / math.log10(pop + 10)
        scored.append({"slug": slug, "title": info["title"],
                       "rating": info["rating"], "popularity": pop,
                       "obscurity": obscurity})
        if i % 20 == 0:
            log.info("  ...%d/%d films probed", i, len(probe))

    scored.sort(key=lambda s: -s["obscurity"])
    obscure = scored[:NUM_OBSCURE_SEEDS]
    rest = scored[NUM_OBSCURE_SEEDS:]
    # mainstream picks: the most-watched of the remaining loved films
    rest.sort(key=lambda s: -s["popularity"])
    mainstream = rest[:NUM_MAINSTREAM_SEEDS]

    for s in obscure:
        s["kind"] = "obscure"
    for s in mainstream:
        s["kind"] = "mainstream"
    seeds = obscure + mainstream
    log.info("Chose %d seeds (%d obscure, %d mainstream). Most obscure: %s "
             "(%s watches)", len(seeds), len(obscure), len(mainstream),
             seeds[0]["title"] if seeds else "-",
             f"{seeds[0]['popularity']:,}" if seeds else "-")
    return seeds


def build_candidate_pool(session: PoliteSession, seeds: list[dict],
                         target_username: str, pool_size: int) -> list[str]:
    """Round-robin the seed films' members pages until pool_size unique
    usernames are collected (excluding the target)."""
    pool: list[str] = []
    seen = {target_username.lower()}
    page = 1
    active = {s["slug"]: True for s in seeds}
    max_member_pages = max(2, math.ceil(
        pool_size / (len(seeds) * 20)) + 2)  # generous upper bound

    while len(pool) < pool_size and any(active.values()) and page <= max_member_pages:
        for seed in seeds:
            slug = seed["slug"]
            if not active[slug] or len(pool) >= pool_size:
                continue
            users = _raters_page(session, slug, page)
            if not users:
                active[slug] = False
                continue
            for u in users:
                lu = u.lower()
                if lu not in seen:
                    seen.add(lu)
                    pool.append(u)
                    if len(pool) >= pool_size:
                        break
        log.info("Candidate pool: %d/%d after members page %d",
                 len(pool), pool_size, page)
        page += 1
    return pool


def _raters_page(session: PoliteSession, slug: str, page: int) -> list[str]:
    from .scraper import BASE_URL, parse_film_members_page
    suffix = "" if page == 1 else f"page/{page}/"
    url = f"{BASE_URL}/film/{slug}/members/rated/.5-5/{suffix}"
    status, body = session.get(url)
    if status != 200:
        return []
    users, _ = parse_film_members_page(body)
    return users


# re-export for the CLI
__all__ = ["choose_seed_films", "build_candidate_pool", "fetch_film_raters"]
