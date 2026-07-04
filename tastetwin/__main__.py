"""taste-twin CLI.

Default pipeline (dataset-first):

    python -m tastetwin run <username> [--verify-top N] [--min-overlap N] [--max-pages N]

    fetch  target's live ratings  ->  ingest  Kaggle pool (one-time)
    ->  analyze  vs ~11k dataset users (seconds)  ->  verify  top matches
    against their CURRENT public ratings (1 req/s)  ->  report

Stages individually:

    python -m tastetwin ingest
    python -m tastetwin fetch <username>
    python -m tastetwin analyze <username> [--min-overlap N]
    python -m tastetwin verify <username> [--verify-top N] [--max-pages N]

Optional scrape-based discovery (supplements the dataset pool with users
found via the target's obscure favorites — slow, hours at 1 req/s):

    python -m tastetwin discover <username> [--pool N]
    python -m tastetwin collect <username> [--max-pages N]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .collect import collect_pool_ratings
from .discover import build_candidate_pool, choose_seed_films
from .ingest import ensure_pool_db, pool_overlap_ratings
from .report import write_reports
from .scraper import PoliteSession, fetch_user_ratings
from .similarity import Match, rank_candidates
from .verify import verify_matches

log = logging.getLogger("tastetwin")


def _run_dir(data_dir: Path, username: str) -> Path:
    d = data_dir / "runs" / username.lower()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load(path: Path, what: str):
    if not path.exists():
        sys.exit(f"error: {what} not found at {path} — run the earlier "
                 f"stage first (see python -m tastetwin --help)")
    return json.loads(path.read_text())


def _save_matches(path: Path, matches: list[Match]) -> None:
    path.write_text(json.dumps([asdict(m) for m in matches]))


def _load_matches(path: Path, what: str) -> list[Match]:
    return [Match(**d) for d in _load(path, what)]


def _target_vectors(run_dir: Path) -> tuple[dict[str, float], dict[str, str]]:
    raw = _load(run_dir / "target.json", "target ratings")
    return ({slug: float(i["rating"]) for slug, i in raw.items()},
            {slug: i["title"] for slug, i in raw.items()})


# -- stages -----------------------------------------------------------------

def stage_fetch(session: PoliteSession, run_dir: Path, username: str) -> dict:
    log.info("Fetching live ratings for target user %r ...", username)
    ratings = fetch_user_ratings(session, username)
    if ratings is None:
        sys.exit(f"error: Letterboxd user {username!r} not found (404)")
    if not ratings:
        sys.exit(f"error: {username!r} has no public ratings")
    (run_dir / "target.json").write_text(json.dumps(ratings))
    log.info("Target has %d rated films.", len(ratings))
    return ratings


def stage_analyze(data_dir: Path, run_dir: Path, min_overlap: int,
                  ) -> list[Match]:
    target, _titles = _target_vectors(run_dir)
    db_path = ensure_pool_db(data_dir)

    started = time.monotonic()
    overlaps, stats, pool_titles = pool_overlap_ratings(db_path, list(target))
    joined = {slug for user_ratings in overlaps.values()
              for slug in user_ratings}
    join_rate = len(joined) / len(target) if target else 0
    log.info("Pool join: %d of the target's %d films exist in the dataset "
             "(%.0f%%); %d pool users share at least one film.",
             len(joined), len(target), join_rate * 100, len(overlaps))

    cand_stats = {u: (mean, std) for u, (mean, std, _n) in stats.items()}
    matches = rank_candidates(target, overlaps, min_overlap=min_overlap,
                              cand_stats=cand_stats, source="dataset")

    # optional supplement: any scraped candidates collected via discover/collect
    ratings_dir = run_dir / "ratings"
    if ratings_dir.exists():
        scraped: dict[str, dict[str, float]] = {}
        for path in sorted(ratings_dir.glob("*.json")):
            data = json.loads(path.read_text())
            r = data.get("ratings") or {}
            if len(r) >= 20:
                scraped[data["username"]] = {
                    s: float(i["rating"]) for s, i in r.items()}
        if scraped:
            log.info("Merging %d scraped candidates into the ranking.",
                     len(scraped))
            existing = {m.username.lower() for m in matches}
            extra = rank_candidates(target, scraped,
                                    min_overlap=min_overlap, source="scraped")
            matches.extend(m for m in extra
                           if m.username.lower() not in existing)
            matches.sort(key=lambda m: -m.score)

    elapsed = time.monotonic() - started
    log.info("Analyze done in %.1fs: %d candidates met the %d-film overlap "
             "threshold.", elapsed, len(matches), min_overlap)
    if matches:
        top = matches[0]
        log.info("Top (unverified): %s score %.3f (r %.3f, overlap %d)",
                 top.username, top.score, top.pearson, top.overlap)
    _save_matches(run_dir / "matches_dataset.json", matches)
    return matches


def stage_verify(session: PoliteSession, run_dir: Path, username: str,
                 verify_top: int, max_pages: int,
                 min_overlap: int) -> list[Match]:
    target, titles = _target_vectors(run_dir)
    matches = _load_matches(run_dir / "matches_dataset.json",
                            "dataset match ranking")
    verified = verify_matches(session, target, matches, top_n=verify_top,
                              max_pages=max_pages, min_overlap=min_overlap)
    _save_matches(run_dir / "matches_verified.json", verified)
    _write_report(run_dir, username, verified, titles)
    return verified


def _write_report(run_dir: Path, username: str, matches: list[Match],
                  titles: dict[str, str]) -> None:
    if not matches:
        log.warning("No matches to report — try a lower --min-overlap.")
        return
    seeds_path = run_dir / "seeds.json"
    popularity = {}
    if seeds_path.exists():
        popularity = {s["slug"]: s["popularity"]
                      for s in json.loads(seeds_path.read_text())}
    md, html = write_reports(run_dir, username, matches, titles, popularity)
    top = matches[0]
    log.info("Report ready: top match %s (score %.3f, r %.3f, overlap %d, "
             "%s).", top.username, top.score, top.pearson, top.overlap,
             top.source)
    log.info("  %s\n  %s", md, html)


def stage_discover(session: PoliteSession, run_dir: Path, username: str,
                   pool_size: int) -> list[str]:
    target = _load(run_dir / "target.json", "target ratings")
    seeds = choose_seed_films(session, target)
    (run_dir / "seeds.json").write_text(json.dumps(seeds))
    pool = build_candidate_pool(session, seeds, username, pool_size)
    (run_dir / "pool.json").write_text(json.dumps(pool))
    log.info("Scraped candidate pool: %d unique users.", len(pool))
    return pool


def stage_collect(session: PoliteSession, run_dir: Path,
                  max_pages: int) -> None:
    pool = _load(run_dir / "pool.json", "candidate pool")
    worst = len(pool) * max_pages
    log.info("Collecting ratings for %d scraped candidates (worst case ~%d "
             "requests at 1 req/s ≈ %.1f h; resumable).",
             len(pool), worst, worst / 3600)
    collect_pool_ratings(session, pool, run_dir, max_pages=max_pages)


# -- entrypoint ---------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m tastetwin",
        description="Find Letterboxd users with taste similar to yours.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"),
                        help="where cache/pool/runs live (default: ./data)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str, user=True, **flags) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        if user:
            p.add_argument("username", help="Letterboxd username")
        if flags.get("verify"):
            p.add_argument("--verify-top", type=int, default=50,
                           help="matches to re-verify live (default 50)")
        if flags.get("pool"):
            p.add_argument("--pool", type=int, default=1000,
                           help="scraped candidate pool size (default 1000)")
        if flags.get("pages"):
            p.add_argument("--max-pages", type=int, default=10,
                           help="max ratings pages per user "
                                "(default 10 ≈ 720 films)")
        if flags.get("overlap"):
            p.add_argument("--min-overlap", type=int, default=15,
                           help="min co-rated films to count a match "
                                "(default 15)")
        return p

    add("run", "full pipeline: fetch → ingest → analyze → verify → report",
        verify=True, pages=True, overlap=True)
    add("ingest", "download the Kaggle pool dataset and build data/pool.db",
        user=False)
    add("fetch", "scrape the target user's current ratings")
    add("analyze", "rank all pool users against the target (fast, offline)",
        overlap=True)
    add("verify", "re-score the top matches against live ratings + report",
        verify=True, pages=True, overlap=True)
    add("discover", "optional: scrape-discover extra candidates via the "
        "target's obscure favorites", pool=True)
    add("collect", "optional: fetch scraped candidates' ratings (slow, "
        "resumable)", pages=True)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    session = PoliteSession(cache_dir=args.data_dir / "cache")

    if args.command == "ingest":
        ensure_pool_db(args.data_dir)
        return

    run_dir = _run_dir(args.data_dir, args.username)

    if args.command == "run":
        log.info("Full run for %r: analyze vs the ~11k-user dataset pool "
                 "(fast), then live-verify top %d (~%d requests at 1 req/s "
                 "≈ %.0f-%.0f min). Interruptible; reruns resume from "
                 "cache.", args.username, args.verify_top,
                 args.verify_top * args.max_pages,
                 args.verify_top * (1 + args.max_pages // 2) / 60,
                 args.verify_top * args.max_pages / 60)
        stage_fetch(session, run_dir, args.username)
        stage_analyze(args.data_dir, run_dir, args.min_overlap)
        stage_verify(session, run_dir, args.username, args.verify_top,
                     args.max_pages, args.min_overlap)
    elif args.command == "fetch":
        stage_fetch(session, run_dir, args.username)
    elif args.command == "analyze":
        stage_analyze(args.data_dir, run_dir, args.min_overlap)
    elif args.command == "verify":
        stage_verify(session, run_dir, args.username, args.verify_top,
                     args.max_pages, args.min_overlap)
    elif args.command == "discover":
        stage_discover(session, run_dir, args.username, args.pool)
    elif args.command == "collect":
        stage_collect(session, run_dir, args.max_pages)

    log.info("HTTP this run: %d live requests, %d cache hits.",
             session.requests_made, session.cache_hits)


if __name__ == "__main__":
    main()
