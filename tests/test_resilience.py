"""Regression tests: one hostile/anomalous candidate must not abort a stage.

The per-response failure modes (off-site redirect rejection, oversized-body
cap) raise ScrapeError. verify/collect runs take ~10 min at 1 req/s — a
single bad candidate is dropped with a warning; only sustained consecutive
failures (a site-wide problem) abort the run.
"""

import json
import logging

import pytest

from tastetwin import collect as collect_mod
from tastetwin import verify as verify_mod
from tastetwin.collect import collect_pool_ratings
from tastetwin.scraper import ScrapeError
from tastetwin.similarity import Match
from tastetwin.verify import verify_matches

# 30 co-rated films with enough spread for a defined Pearson r.
TARGET = {f"film-{i}": float(i % 10 + 1) for i in range(30)}
GOOD_RATINGS = {f"film-{i}": {"title": f"T{i}", "rating": i % 10 + 1}
                for i in range(30)}


def _match(username: str) -> Match:
    return Match(username=username, score=0.5, pearson=0.6, overlap=30)


def _fetch_failing_for(bad_users):
    def fake_fetch(session, username, max_pages=None):
        if username in bad_users:
            raise ScrapeError(f"{username}: response exceeds cap — aborting")
        return dict(GOOD_RATINGS)
    return fake_fetch


class TestVerifyResilience:
    def test_one_failing_candidate_does_not_abort(self, monkeypatch, caplog):
        monkeypatch.setattr(verify_mod, "fetch_user_ratings",
                            _fetch_failing_for({"hostile"}))
        matches = [_match(u) for u in ["alice", "hostile", "bob"]]
        with caplog.at_level(logging.WARNING, logger="tastetwin"):
            out = verify_matches(None, TARGET, matches, top_n=3,
                                 min_overlap=15)
        assert sorted(m.username for m in out) == ["alice", "bob"]
        # the warning names the candidate and the reason
        warned = [r.message for r in caplog.records
                  if r.levelno == logging.WARNING]
        assert any("hostile" in msg and "exceeds cap" in msg
                   for msg in warned)

    def test_consecutive_failures_abort(self, monkeypatch):
        n = verify_mod.MAX_CONSECUTIVE_FAILURES
        bad = {f"bad{i}" for i in range(n)}
        monkeypatch.setattr(verify_mod, "fetch_user_ratings",
                            _fetch_failing_for(bad))
        matches = [_match(u) for u in sorted(bad)] + [_match("alice")]
        with pytest.raises(ScrapeError, match="consecutive"):
            verify_matches(None, TARGET, matches, top_n=len(matches),
                           min_overlap=15)

    def test_success_resets_failure_counter(self, monkeypatch):
        n = verify_mod.MAX_CONSECUTIVE_FAILURES
        bad = {f"bad{i}" for i in range(2 * (n - 1))}
        monkeypatch.setattr(verify_mod, "fetch_user_ratings",
                            _fetch_failing_for(bad))
        users = ([f"bad{i}" for i in range(n - 1)] + ["alice"]
                 + [f"bad{i}" for i in range(n - 1, 2 * (n - 1))] + ["bob"])
        out = verify_matches(None, TARGET, [_match(u) for u in users],
                             top_n=len(users), min_overlap=15)
        assert sorted(m.username for m in out) == ["alice", "bob"]

    def test_invalid_username_dropped_before_fetch(self, monkeypatch, caplog):
        fetched = []

        def fake_fetch(session, username, max_pages=None):
            fetched.append(username)
            return dict(GOOD_RATINGS)

        monkeypatch.setattr(verify_mod, "fetch_user_ratings", fake_fetch)
        matches = [_match(u) for u in ["alice", "e/vil", "bob"]]
        with caplog.at_level(logging.INFO, logger="tastetwin"):
            out = verify_matches(None, TARGET, matches, top_n=3,
                                 min_overlap=15)
        assert sorted(m.username for m in out) == ["alice", "bob"]
        assert "e/vil" not in fetched
        # the drop log says WHY (charset validation), not "gone or private"
        infos = [r.message for r in caplog.records]
        assert any("invalid username" in msg for msg in infos)
        assert not any("e/vil" in msg and "gone or private" in msg
                       for msg in infos)


class TestCollectResilience:
    def test_one_failing_candidate_does_not_abort(self, tmp_path,
                                                  monkeypatch, caplog):
        monkeypatch.setattr(collect_mod, "fetch_user_ratings",
                            _fetch_failing_for({"hostile"}))
        with caplog.at_level(logging.WARNING, logger="tastetwin"):
            out = collect_pool_ratings(None, ["alice", "hostile", "bob"],
                                       tmp_path)
        assert sorted(out) == ["alice", "bob"]
        assert any("hostile" in r.message for r in caplog.records
                   if r.levelno == logging.WARNING)
        # no file was written for the failure, so a rerun retries it
        assert not (tmp_path / "ratings" / "hostile.json").exists()
        assert (tmp_path / "ratings" / "alice.json").exists()

    def test_failed_candidate_retried_on_resume(self, tmp_path, monkeypatch):
        monkeypatch.setattr(collect_mod, "fetch_user_ratings",
                            _fetch_failing_for({"hostile"}))
        collect_pool_ratings(None, ["alice", "hostile"], tmp_path)
        # second run: the fetch now succeeds and the candidate is collected
        monkeypatch.setattr(collect_mod, "fetch_user_ratings",
                            _fetch_failing_for(set()))
        out = collect_pool_ratings(None, ["alice", "hostile"], tmp_path)
        assert sorted(out) == ["alice", "hostile"]
        data = json.loads((tmp_path / "ratings" / "hostile.json").read_text())
        assert data["username"] == "hostile"

    def test_consecutive_failures_abort(self, tmp_path, monkeypatch):
        n = collect_mod.MAX_CONSECUTIVE_FAILURES
        bad = {f"bad{i}" for i in range(n)}
        monkeypatch.setattr(collect_mod, "fetch_user_ratings",
                            _fetch_failing_for(bad))
        with pytest.raises(ScrapeError, match="consecutive"):
            collect_pool_ratings(None, sorted(bad) + ["alice"], tmp_path)
