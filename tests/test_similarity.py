"""Hand-computed cases for the similarity math."""

import math

import pytest

from tastetwin.similarity import (compare, pearson, rank_candidates,
                                  significance_weight, zscores)


def _films(ratings):
    return {f"film-{i}": float(r) for i, r in enumerate(ratings)}


class TestZScores:
    def test_hand_computed(self):
        z = zscores({"a": 2.0, "b": 4.0, "c": 6.0})
        # mean 4, population std = sqrt(8/3)
        std = math.sqrt(8 / 3)
        assert z["a"] == pytest.approx(-2 / std)
        assert z["b"] == pytest.approx(0.0)
        assert z["c"] == pytest.approx(2 / std)

    def test_constant_rater_has_no_signal(self):
        z = zscores({"a": 7.0, "b": 7.0})
        assert z == {"a": 0.0, "b": 0.0}

    def test_empty(self):
        assert zscores({}) == {}


class TestPearson:
    def test_perfect_match(self):
        a = _films([1, 3, 5, 7, 9, 10, 2, 4])
        r = pearson(a, dict(a), list(a))
        assert r == pytest.approx(1.0)

    def test_inverted_taste(self):
        a = _films([1, 2, 3, 4, 5])
        b = {k: 11 - v for k, v in a.items()}  # mirror image
        r = pearson(a, b, list(a))
        assert r == pytest.approx(-1.0)

    def test_scale_shifted_identical_taste(self):
        # Candidate rates exactly like the target but compressed and shifted:
        # a harsh rater vs a generous one. Must be ~1.0 after normalization.
        a = _films([2, 4, 6, 8, 10, 5, 7, 3])
        b = {k: 0.4 * v + 1.5 for k, v in a.items()}
        r = pearson(a, b, list(a))
        assert r == pytest.approx(1.0)

    def test_constant_candidate_undefined(self):
        a = _films([1, 2, 3])
        b = {k: 5.0 for k in a}
        assert pearson(a, b, list(a)) is None

    def test_hand_computed_value(self):
        # x = [1,2,3], y = [1,3,2]: r = 0.5 by hand
        a = {"p": 1.0, "q": 2.0, "r": 3.0}
        b = {"p": 1.0, "q": 3.0, "r": 2.0}
        assert pearson(a, b, ["p", "q", "r"]) == pytest.approx(0.5)


class TestSignificanceWeighting:
    def test_tiny_overlap_penalized(self):
        assert significance_weight(15) == pytest.approx(15 / 50)
        assert significance_weight(50) == 1.0
        assert significance_weight(400) == 1.0  # capped, never > 1

    def test_perfect_r_with_tiny_overlap_cannot_beat_good_r_with_big_overlap(self):
        target = _films(range(1, 61))  # 60 films, ratings 1..10 cycling
        target = {k: (i % 10) + 1.0 for i, k in enumerate(target)}

        # candidate A: perfect agreement, but only on 16 films
        cand_a = {k: v for k, v in list(target.items())[:16]}
        # candidate B: strong-but-imperfect agreement on all 60
        cand_b = {k: v + (0.7 if i % 3 == 0 else -0.4)
                  for i, (k, v) in enumerate(target.items())}

        ranked = rank_candidates(target, {"a": cand_a, "b": cand_b},
                                 min_overlap=15)
        assert [m.username for m in ranked] == ["b", "a"]
        a = next(m for m in ranked if m.username == "a")
        assert a.pearson == pytest.approx(1.0)
        assert a.score == pytest.approx(16 / 50)  # shrunk hard


class TestCompare:
    def test_below_min_overlap_returns_none(self):
        a = _films([1, 2, 3, 4, 5])
        assert compare(a, dict(a), min_overlap=6) is None

    def test_scale_shifted_scores_one_with_full_overlap(self):
        vals = [(i % 10) + 1.0 for i in range(60)]
        a = {f"f{i}": v for i, v in enumerate(vals)}
        b = {k: 0.3 * v + 2.0 for k, v in a.items()}  # same taste, other scale
        m = compare(a, b, min_overlap=15)
        assert m is not None
        assert m.pearson == pytest.approx(1.0)
        assert m.score == pytest.approx(1.0)  # overlap 60 >= cap 50
        assert m.overlap == 60

    def test_shared_loves_require_both_z_high(self):
        # both love f-high, disagree on f-split
        a = {"f-high": 10.0, "f-split": 9.0, "f-low": 1.0, "f-mid": 5.0,
             "f-mid2": 5.0}
        b = {"f-high": 9.0, "f-split": 1.0, "f-low": 2.0, "f-mid": 5.0,
             "f-mid2": 6.0}
        m = compare(a, b, min_overlap=2)
        assert m is not None
        assert "f-high" in m.shared_loves
        assert "f-split" not in m.shared_loves
        assert "f-low" not in m.shared_loves
        # the split film is the biggest disagreement
        assert m.disagreements[0][0] == "f-split"

    def test_disagreements_are_large_z_gaps_only(self):
        a = _films([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        m = compare(a, dict(a), min_overlap=5)
        assert m is not None
        assert m.disagreements == []  # identical taste: no gaps >= 1 sigma


class TestOverlapSliceWithGlobalStats:
    def test_cand_stats_used_for_love_zscores(self):
        # Candidate slice only holds overlap films, all rated 8 — with no
        # global stats their z-scores would be 0 (no signal). With global
        # stats (mean 5, std 2 over full history) an 8 is +1.5 sigma: love.
        target = {f"f{i}": float((i % 10) + 1) for i in range(30)}
        target["f-gem"] = 10.0
        cand_slice = {"f-gem": 8.0}
        cand_slice.update({f"f{i}": target[f"f{i}"] for i in range(20)})

        from tastetwin.similarity import compare
        m = compare(target, cand_slice, min_overlap=10,
                    cand_stats=(5.0, 2.0))
        assert m is not None
        assert "f-gem" in m.shared_loves

    def test_rank_candidates_passes_stats_and_source(self):
        target = {f"f{i}": float((i % 10) + 1) for i in range(30)}
        cands = {"u1": dict(target)}
        ranked = rank_candidates(target, cands, min_overlap=10,
                                 cand_stats={"u1": (5.5, 2.87)},
                                 source="dataset")
        assert ranked[0].username == "u1"
        assert ranked[0].source == "dataset"
        assert ranked[0].pearson == pytest.approx(1.0)
