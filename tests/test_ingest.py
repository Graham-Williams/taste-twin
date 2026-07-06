"""Ingest tests: synthetic CSVs -> SQLite pool -> overlap queries."""

import csv

import pytest

from tastetwin.ingest import build_pool_db, pool_overlap_ratings

RATINGS = [
    # alice: mean 3.0 stars over 4 ratings
    ("alice", "film-a", "4.0"), ("alice", "film-b", "2.0"),
    ("alice", "film-c", "3.5"), ("alice", "film-d", "2.5"),
    # bob rates only one target film
    ("bob", "film-a", "5.0"), ("bob", "film-x", "1.0"),
    # carol rates nothing the target rated
    ("carol", "film-x", "3.0"), ("carol", "film-y", "4.0"),
]

FILMS = [
    ("film-a", "Film A", "2001"), ("film-b", "Film B", "2002"),
    ("film-c", "Film C", "2003"), ("film-d", "Film D", "2004"),
    ("film-x", "Film X", "2005"), ("film-y", "Film Y", "2006"),
]


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    d = tmp_path_factory.mktemp("pool")
    with open(d / "ratings.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["user_name", "film_id", "rating"])
        w.writerows(RATINGS)
    with open(d / "films.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["film_id", "film_name", "year", "poster_url"])
        w.writerows([(a, b, c) for a, b, c in FILMS])
    db = d / "pool.db"
    build_pool_db(d, db)
    return db


def test_ratings_stored_as_halfstars(db_path):
    overlaps, _, _ = pool_overlap_ratings(db_path, ["film-a"])
    assert overlaps["alice"]["film-a"] == 8.0   # 4.0 stars -> 8 half-stars
    assert overlaps["bob"]["film-a"] == 10.0


def test_overlap_restricted_to_target_films(db_path):
    target = ["film-a", "film-b", "film-c"]
    overlaps, _, _ = pool_overlap_ratings(db_path, target)
    assert set(overlaps["alice"]) == {"film-a", "film-b", "film-c"}
    assert set(overlaps["bob"]) == {"film-a"}
    assert "carol" not in overlaps  # zero overlap


def test_user_stats_cover_full_history(db_path):
    _, stats, _ = pool_overlap_ratings(db_path, ["film-a"])
    mean, std, n = stats["alice"]
    assert n == 4
    assert mean == pytest.approx(6.0)  # (8+4+7+5)/4 half-stars
    assert std == pytest.approx(1.5811, abs=1e-3)
    # bob's stats span film-x too, not just the overlap
    assert stats["bob"][2] == 2
    assert stats["bob"][0] == pytest.approx(6.0)  # (10+2)/2


def test_film_titles_joined(db_path):
    _, _, titles = pool_overlap_ratings(db_path, ["film-a", "film-y", "nope"])
    assert titles == {"film-a": "Film A", "film-y": "Film Y"}
