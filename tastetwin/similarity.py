"""Similarity math: per-user z-scores, Pearson over co-rated films,
significance weighting. Pure stdlib, no numpy."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MIN_OVERLAP_DEFAULT = 15
SIGNIFICANCE_CAP = 50


def mean_std(values: list[float]) -> tuple[float, float]:
    """Mean and population standard deviation."""
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return mean, math.sqrt(var)


def zscores(ratings: dict[str, float],
            stats: tuple[float, float] | None = None) -> dict[str, float]:
    """Normalize a user's ratings by their own mean and (population) std.

    Handles different personal scales: someone whose ratings live in 2–3.5
    stars and someone who uses the full 0.5–5 range produce comparable
    z-scores. `stats` may supply a precomputed (mean, std) over the user's
    FULL rating history when `ratings` is only a slice of it. If the user
    rates everything identically (std == 0) there is no signal; every
    z-score is 0.
    """
    if not ratings:
        return {}
    mean, std = stats if stats is not None else mean_std(list(ratings.values()))
    if std == 0:
        return {k: 0.0 for k in ratings}
    return {k: (v - mean) / std for k, v in ratings.items()}


def pearson(a: dict[str, float], b: dict[str, float],
            keys: list[str]) -> float | None:
    """Pearson correlation of two users' ratings over the given overlap keys.

    Pearson is invariant under each user's own linear rescaling, so this is
    mathematically the cosine of the two users' z-scored rating vectors
    restricted to the overlap (with means/stds computed *on the overlap*).
    Returns None when undefined (either user is constant on the overlap).
    """
    n = len(keys)
    if n < 2:
        return None
    xs = [a[k] for k in keys]
    ys = [b[k] for k in keys]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    r = sxy / math.sqrt(sxx * syy)
    return max(-1.0, min(1.0, r))  # clamp float noise


def significance_weight(overlap: int, cap: int = SIGNIFICANCE_CAP) -> float:
    """Shrink scores from small overlaps: full weight only at >= cap films."""
    return min(overlap, cap) / cap


@dataclass
class Match:
    username: str
    score: float           # r * significance weight — the ranking key
    pearson: float         # raw correlation over the overlap
    overlap: int           # number of co-rated films
    source: str = "dataset"            # "dataset" | "live" | "scraped"
    dataset_score: float | None = None  # pre-verification score, if any
    shared_loves: list[str] = field(default_factory=list)      # slugs
    disagreements: list[tuple[str, float, float]] = field(default_factory=list)
    # disagreements: (slug, target_z, candidate_z)


def compare(target: dict[str, float], candidate: dict[str, float],
            min_overlap: int = MIN_OVERLAP_DEFAULT,
            love_z: float = 0.5,
            target_z: dict[str, float] | None = None,
            cand_stats: tuple[float, float] | None = None) -> Match | None:
    """Score a candidate against the target user.

    Returns None when the overlap is below min_overlap or the correlation
    is undefined. shared_loves / disagreements use z-scores computed over
    each user's FULL ratings (their whole personal scale) — pass
    `cand_stats` = (mean, std) when `candidate` holds only the overlap
    slice of the candidate's history. The Pearson r itself is computed on
    the overlap set.
    """
    overlap_keys = sorted(set(target) & set(candidate))
    if len(overlap_keys) < min_overlap:
        return None
    r = pearson(target, candidate, overlap_keys)
    if r is None:
        return None
    score = r * significance_weight(len(overlap_keys))

    tz = target_z if target_z is not None else zscores(target)
    cz = zscores(candidate, stats=cand_stats)
    shared_loves = sorted(
        (k for k in overlap_keys if tz[k] >= love_z and cz[k] >= love_z),
        key=lambda k: -(tz[k] + cz[k]))
    disagreements = sorted(
        ((k, tz[k], cz[k]) for k in overlap_keys),
        key=lambda item: -abs(item[1] - item[2]))[:5]
    disagreements = [d for d in disagreements if abs(d[1] - d[2]) >= 1.0]

    return Match(username="", score=score, pearson=r,
                 overlap=len(overlap_keys), shared_loves=shared_loves,
                 disagreements=disagreements)


def rank_candidates(target: dict[str, float],
                    candidates: dict[str, dict[str, float]],
                    min_overlap: int = MIN_OVERLAP_DEFAULT,
                    cand_stats: dict[str, tuple[float, float]] | None = None,
                    source: str = "dataset") -> list[Match]:
    """Compare every candidate to the target; return matches sorted by score.

    `candidates` may hold full rating histories, or (for the dataset pool)
    only each user's overlap with the target — in that case pass
    `cand_stats` = {user: (mean, std)} computed over full histories.
    """
    tz = zscores(target)
    matches: list[Match] = []
    for username, ratings in candidates.items():
        m = compare(target, ratings, min_overlap=min_overlap,
                    target_z=tz,
                    cand_stats=(cand_stats or {}).get(username))
        if m is not None:
            m.username = username
            m.source = source
            matches.append(m)
    matches.sort(key=lambda m: -m.score)
    return matches
