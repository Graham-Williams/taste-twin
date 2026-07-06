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

Stage implementations live in ``tastetwin.pipeline`` (shared with the
web app in ``tastetwin.web``).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .ingest import ensure_pool_db
from .pipeline import (PipelineError, run_dir_for, stage_analyze,
                       stage_collect, stage_discover, stage_fetch,
                       stage_verify)
from .scraper import PoliteSession

log = logging.getLogger("tastetwin")


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

    try:
        if args.command == "ingest":
            ensure_pool_db(args.data_dir)
            return

        run_dir = run_dir_for(args.data_dir, args.username)

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
    except PipelineError as exc:
        sys.exit(f"error: {exc}")

    log.info("HTTP this run: %d live requests, %d cache hits.",
             session.requests_made, session.cache_hits)


if __name__ == "__main__":
    main()
