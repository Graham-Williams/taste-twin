"""taste-twin CLI.

    python -m tastetwin run <username> [--pool N] [--min-overlap N] [--max-pages N]

or stage by stage:

    python -m tastetwin fetch <username>
    python -m tastetwin discover <username> [--pool N]
    python -m tastetwin collect <username> [--max-pages N]
    python -m tastetwin analyze <username> [--min-overlap N]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .collect import collect_pool_ratings
from .discover import build_candidate_pool, choose_seed_films
from .report import write_reports
from .scraper import (MEMBERS_PER_PAGE, PoliteSession, fetch_user_ratings)
from .similarity import rank_candidates

log = logging.getLogger("tastetwin")


def _run_dir(data_dir: Path, username: str) -> Path:
    d = data_dir / "runs" / username.lower()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load(path: Path, what: str) -> dict | list:
    if not path.exists():
        sys.exit(f"error: {what} not found at {path} — run the earlier "
                 f"stage first (see python -m tastetwin --help)")
    return json.loads(path.read_text())


def stage_fetch(session: PoliteSession, run_dir: Path, username: str) -> dict:
    log.info("Fetching ratings for target user %r ...", username)
    ratings = fetch_user_ratings(session, username)
    if ratings is None:
        sys.exit(f"error: Letterboxd user {username!r} not found (404)")
    if not ratings:
        sys.exit(f"error: {username!r} has no public ratings")
    (run_dir / "target.json").write_text(json.dumps(ratings))
    log.info("Target has %d rated films.", len(ratings))
    return ratings


def stage_discover(session: PoliteSession, run_dir: Path, username: str,
                   pool_size: int) -> list[str]:
    target = _load(run_dir / "target.json", "target ratings")
    seeds = choose_seed_films(session, target)
    (run_dir / "seeds.json").write_text(json.dumps(seeds))
    est_pages = -(-pool_size // (len(seeds) * (MEMBERS_PER_PAGE - 5) or 1))
    log.info("Building candidate pool of %d (~%d member-page requests)...",
             pool_size, est_pages * len(seeds))
    pool = build_candidate_pool(session, seeds, username, pool_size)
    (run_dir / "pool.json").write_text(json.dumps(pool))
    log.info("Pool built: %d unique candidates.", len(pool))
    return pool


def stage_collect(session: PoliteSession, run_dir: Path,
                  max_pages: int) -> dict:
    pool = _load(run_dir / "pool.json", "candidate pool")
    worst_case_req = len(pool) * max_pages
    log.info("Collecting ratings for %d candidates, up to %d pages each.",
             len(pool), max_pages)
    log.info("Heads up: worst case ~%d requests at 1 req/s ≈ %.1f hours. "
             "Progress is saved — you can kill and resume anytime.",
             worst_case_req, worst_case_req / 3600)
    return collect_pool_ratings(session, pool, run_dir, max_pages=max_pages)


def stage_analyze(run_dir: Path, username: str, min_overlap: int) -> None:
    target_raw = _load(run_dir / "target.json", "target ratings")
    ratings_dir = run_dir / "ratings"
    if not ratings_dir.exists():
        sys.exit("error: no collected ratings — run `collect` first")

    target = {slug: float(info["rating"]) for slug, info in target_raw.items()}
    titles = {slug: info["title"] for slug, info in target_raw.items()}

    candidates: dict[str, dict[str, float]] = {}
    for path in sorted(ratings_dir.glob("*.json")):
        data = json.loads(path.read_text())
        r = data.get("ratings") or {}
        if len(r) >= 20:
            candidates[data["username"]] = {
                slug: float(info["rating"]) for slug, info in r.items()}

    popularity: dict[str, int] = {}
    seeds_path = run_dir / "seeds.json"
    if seeds_path.exists():
        for s in json.loads(seeds_path.read_text()):
            popularity[s["slug"]] = s["popularity"]

    log.info("Scoring %d candidates against %d target ratings "
             "(min overlap %d)...", len(candidates), len(target), min_overlap)
    matches = rank_candidates(target, candidates, min_overlap=min_overlap)
    (run_dir / "results.json").write_text(json.dumps([
        {"username": m.username, "score": m.score, "pearson": m.pearson,
         "overlap": m.overlap} for m in matches]))

    if not matches:
        log.warning("No candidates met the overlap threshold — try a bigger "
                    "--pool or a lower --min-overlap.")
        return
    md, html = write_reports(run_dir, username, matches, titles, popularity)
    top = matches[0]
    log.info("Done. %d matches. Top: %s (score %.3f, r %.3f, overlap %d)",
             len(matches), top.username, top.score, top.pearson, top.overlap)
    log.info("Reports: %s  |  %s", md, html)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m tastetwin",
        description="Find Letterboxd users with taste similar to yours.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"),
                        help="where cache/runs live (default: ./data)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str, **flags) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.add_argument("username", help="Letterboxd username")
        if flags.get("pool"):
            p.add_argument("--pool", type=int, default=1000,
                           help="candidate pool size (default 1000)")
        if flags.get("pages"):
            p.add_argument("--max-pages", type=int, default=10,
                           help="max ratings pages per candidate "
                                "(default 10 ≈ 720 films)")
        if flags.get("overlap"):
            p.add_argument("--min-overlap", type=int, default=15,
                           help="min co-rated films to count a match "
                                "(default 15)")
        return p

    add("run", "full pipeline end-to-end", pool=True, pages=True, overlap=True)
    add("fetch", "scrape the target user's ratings")
    add("discover", "choose seed films + build candidate pool", pool=True)
    add("collect", "fetch candidates' ratings (resumable)", pages=True)
    add("analyze", "score candidates + write reports", overlap=True)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    session = PoliteSession(cache_dir=args.data_dir / "cache")
    run_dir = _run_dir(args.data_dir, args.username)

    if args.command == "run":
        est = args.pool * (1 + args.max_pages // 2)  # rough midpoint estimate
        log.info("Full run for %r: pool=%d, max_pages=%d. Expect roughly "
                 "%d-%d requests at 1 req/s — i.e. ~%.1f-%.1f hours. "
                 "Safe to interrupt; reruns resume from cache.",
                 args.username, args.pool, args.max_pages,
                 est, args.pool * args.max_pages,
                 est / 3600, args.pool * args.max_pages / 3600)
        stage_fetch(session, run_dir, args.username)
        stage_discover(session, run_dir, args.username, args.pool)
        stage_collect(session, run_dir, args.max_pages)
        stage_analyze(run_dir, args.username, args.min_overlap)
    elif args.command == "fetch":
        stage_fetch(session, run_dir, args.username)
    elif args.command == "discover":
        stage_discover(session, run_dir, args.username, args.pool)
    elif args.command == "collect":
        stage_collect(session, run_dir, args.max_pages)
    elif args.command == "analyze":
        stage_analyze(run_dir, args.username, args.min_overlap)

    log.info("HTTP: %d live requests, %d cache hits.",
             session.requests_made, session.cache_hits)


if __name__ == "__main__":
    main()
