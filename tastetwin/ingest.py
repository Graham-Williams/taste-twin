"""Dataset ingest: Kaggle `freeth/letterboxd-film-ratings` -> SQLite pool.

The dataset (CC0, snapshot ~Oct 2023) holds ~18.2M ratings by ~11k of
Letterboxd's most active members, scraped from public pages by
https://github.com/adamjhf/letterboxd-scraper. Both `user_name` and
`film_id` are real Letterboxd URL slugs (spot-checked live), so they join
directly against what our scraper collects.

Download path: `kagglehub` works for this public dataset WITHOUT Kaggle
credentials. If that ever fails, manual fallback: download the zip from
https://www.kaggle.com/datasets/freeth/letterboxd-film-ratings and unzip
`ratings.csv` + `films.csv` into `data/dataset/`.

Ratings in the CSV are 0.5-5.0 stars; we store half-stars (1-10) to match
the scraper's representation.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("tastetwin")

KAGGLE_DATASET = "freeth/letterboxd-film-ratings"
BATCH = 50_000


def locate_dataset(data_dir: Path) -> Path:
    """Return a directory containing ratings.csv + films.csv."""
    manual = data_dir / "dataset"
    if (manual / "ratings.csv").exists():
        log.info("Using manually downloaded dataset at %s", manual)
        return manual
    try:
        import kagglehub
        path = Path(kagglehub.dataset_download(KAGGLE_DATASET))
        if (path / "ratings.csv").exists():
            return path
        raise FileNotFoundError(f"ratings.csv missing under {path}")
    except Exception as exc:  # noqa: BLE001 - present the manual fallback
        raise SystemExit(
            f"Could not fetch the Kaggle dataset automatically ({exc}).\n"
            f"Manual fallback: download the zip from\n"
            f"  https://www.kaggle.com/datasets/{KAGGLE_DATASET}\n"
            f"and unzip ratings.csv + films.csv into {manual}/ then rerun."
        ) from exc


def build_pool_db(csv_dir: Path, db_path: Path) -> None:
    """Stream the CSVs into SQLite with indexes + per-user stats."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript("""
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE ratings (
            user_name TEXT NOT NULL,
            film_id   TEXT NOT NULL,
            rating    REAL NOT NULL          -- half-stars, 1..10
        );
        CREATE TABLE films (
            film_id   TEXT PRIMARY KEY,
            film_name TEXT,
            year      TEXT
        );
    """)

    started = time.monotonic()
    n = 0
    with open(csv_dir / "ratings.csv", newline="") as f:
        reader = csv.reader(f)
        next(reader)  # header
        batch = []
        for user, film, rating in reader:
            batch.append((user, film, float(rating) * 2))  # -> half-stars
            if len(batch) >= BATCH:
                con.executemany("INSERT INTO ratings VALUES (?,?,?)", batch)
                n += len(batch)
                batch.clear()
                if n % 2_000_000 == 0:
                    log.info("  ...%dM ratings loaded", n // 1_000_000)
        if batch:
            con.executemany("INSERT INTO ratings VALUES (?,?,?)", batch)
            n += len(batch)

    with open(csv_dir / "films.csv", newline="") as f:
        reader = csv.reader(f)
        next(reader)
        con.executemany(
            "INSERT OR IGNORE INTO films VALUES (?,?,?)",
            ((row[0], row[1], row[2]) for row in reader if len(row) >= 3))

    log.info("Loaded %d ratings; building indexes...", n)
    con.executescript("""
        CREATE INDEX idx_ratings_film ON ratings(film_id);
        CREATE INDEX idx_ratings_user ON ratings(user_name);
        CREATE TABLE user_stats AS
            SELECT user_name,
                   COUNT(*)    AS n,
                   AVG(rating) AS mean,
                   AVG(rating * rating) - AVG(rating) * AVG(rating) AS var
            FROM ratings GROUP BY user_name;
        CREATE UNIQUE INDEX idx_user_stats ON user_stats(user_name);
    """)
    con.commit()
    con.close()
    tmp.replace(db_path)
    log.info("Pool DB ready at %s (%.0fs, %d ratings).",
             db_path, time.monotonic() - started, n)


def ensure_pool_db(data_dir: Path) -> Path:
    """Idempotent: return the pool DB path, building it if absent."""
    db_path = data_dir / "pool.db"
    if db_path.exists():
        return db_path
    log.info("Pool DB not found — ingesting the Kaggle dataset "
             "(~600 MB CSV; a few minutes one-time).")
    csv_dir = locate_dataset(data_dir)
    build_pool_db(csv_dir, db_path)
    return db_path


def pool_overlap_ratings(db_path: Path, film_slugs: list[str],
                         ) -> tuple[dict[str, dict[str, float]],
                                    dict[str, tuple[float, float, int]],
                                    dict[str, str]]:
    """For every pool user, their ratings restricted to the given films.

    Returns (overlaps, user_stats, film_titles):
      overlaps:  {user: {slug: rating_halfstars}}
      user_stats: {user: (mean, std, total_rating_count)} over their FULL
                  dataset ratings (for z-scoring on their whole scale)
      film_titles: {slug: title} for the requested slugs found in the pool
    """
    con = sqlite3.connect(db_path)
    con.execute("CREATE TEMP TABLE target_films (film_id TEXT PRIMARY KEY)")
    con.executemany("INSERT OR IGNORE INTO target_films VALUES (?)",
                    ((s,) for s in film_slugs))

    overlaps: dict[str, dict[str, float]] = {}
    for user, film, rating in con.execute(
            "SELECT r.user_name, r.film_id, r.rating FROM ratings r "
            "JOIN target_films t ON t.film_id = r.film_id"):
        overlaps.setdefault(user, {})[film] = rating

    stats = {
        user: (mean, max(var, 0.0) ** 0.5, n)
        for user, n, mean, var in con.execute(
            "SELECT user_name, n, mean, var FROM user_stats")}

    titles = {
        film: name for film, name in con.execute(
            "SELECT f.film_id, f.film_name FROM films f "
            "JOIN target_films t ON t.film_id = f.film_id")}
    con.close()
    return overlaps, stats, titles
