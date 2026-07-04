"""Polite Letterboxd scraper: HTTP layer + HTML parsing.

Selectors were verified against live Letterboxd HTML (July 2026):

- A user's rated films live on ``/<user>/films/`` (the old
  ``/films/ratings/`` URL now 302s there). Each film is an
  ``li.griditem`` containing a ``div[data-item-slug]`` and, when the user
  rated it, a ``span.rating`` whose ``rated-N`` class encodes the rating in
  half-stars (N = 1..10). 72 films per page; pagination via
  ``div.paginate-pages`` / ``a.next``.
- Users who rated a film are listed at
  ``/film/<slug>/members/rated/.5-5/`` — table rows with
  ``td.col-member`` containing ``a.name`` whose href is ``/<username>/``.
  25 members per page.
- A film's popularity (watch count) comes from the small fragment
  ``/csi/film/<slug>/stats/`` ("Watched by N members" in an aria-label).

NOTE: Letterboxd serves an HTTP/2-fingerprint-based Cloudflare challenge to
some clients on some paths; plain HTTP/1.1 (what ``requests`` uses) is
served normally.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("tastetwin")

BASE_URL = "https://letterboxd.com"
USER_AGENT = (
    "taste-twin/0.1 (personal project; "
    "+https://github.com/Graham-Williams/taste-twin)"
)
MIN_DELAY_SECONDS = 1.0
CACHE_TTL_SECONDS = 7 * 24 * 3600  # a week; taste data moves slowly
MAX_RETRIES = 4
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

FILMS_PER_PAGE = 72
MEMBERS_PER_PAGE = 25


class ScrapeError(Exception):
    pass


class RobotsDisallowedError(ScrapeError):
    pass


class _Robots:
    """Minimal robots.txt matcher that supports '*' wildcards (Python's
    urllib.robotparser does not), applied to the '*' user-agent group —
    which is the group Letterboxd's robots.txt puts generic clients in."""

    def __init__(self, text: str):
        self._patterns: list[re.Pattern] = []
        agents: list[str] = []
        in_rules = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field, value = field.strip().lower(), value.strip()
            if field == "user-agent":
                if in_rules:  # a new agent group starts
                    agents, in_rules = [], False
                agents.append(value)
            elif field in ("disallow", "allow"):
                in_rules = True
                if "*" in agents and field == "disallow" and value:
                    self._patterns.append(self._compile(value))

    @staticmethod
    def _compile(pattern: str) -> re.Pattern:
        regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern)
        if pattern.endswith("$"):
            regex = regex[: -len(re.escape("$"))] + "$"
        return re.compile("^" + regex)

    def allowed(self, path: str) -> bool:
        return not any(p.search(path) for p in self._patterns)


class PoliteSession:
    """requests wrapper enforcing the politeness policy:

    - >= MIN_DELAY_SECONDS between requests, single-threaded
    - identifying User-Agent
    - retry with exponential backoff on 429/5xx (honors Retry-After)
    - on-disk cache keyed by URL, with a freshness TTL
    - robots.txt disallows respected
    """

    def __init__(self, cache_dir: Path, ttl: float = CACHE_TTL_SECONDS,
                 min_delay: float = MIN_DELAY_SECONDS):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.min_delay = min_delay
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._last_request_at = 0.0
        self._robots: _Robots | None = None
        self.requests_made = 0
        self.cache_hits = 0

    # -- cache ------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / f"{digest[:2]}" / f"{digest}.json"

    def _cache_read(self, url: str) -> dict | None:
        path = self._cache_path(url)
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - entry.get("fetched_at", 0) > self.ttl:
            return None
        return entry

    def _cache_write(self, url: str, status: int, body: str) -> None:
        path = self._cache_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"url": url, "fetched_at": time.time(), "status": status,
             "body": body}))

    # -- robots -----------------------------------------------------------

    def _check_robots(self, url: str) -> None:
        if self._robots is None:
            text = self._fetch_raw(f"{BASE_URL}/robots.txt").text
            self._robots = _Robots(text)
        path = urlparse(url).path
        if not self._robots.allowed(path):
            raise RobotsDisallowedError(f"robots.txt disallows {path}")

    # -- fetching ---------------------------------------------------------

    def _throttle(self) -> None:
        wait = self._last_request_at + self.min_delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _fetch_raw(self, url: str) -> requests.Response:
        backoff = 2.0
        for attempt in range(MAX_RETRIES + 1):
            self._throttle()
            self._last_request_at = time.monotonic()
            self.requests_made += 1
            resp = self._session.get(url, timeout=30)
            if resp.status_code not in RETRYABLE_STATUSES:
                return resp
            if attempt == MAX_RETRIES:
                raise ScrapeError(
                    f"{url} still failing ({resp.status_code}) after "
                    f"{MAX_RETRIES} retries")
            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if (retry_after or "").isdigit() else backoff
            log.warning("HTTP %s on %s — backing off %.0fs",
                        resp.status_code, url, delay)
            time.sleep(delay)
            backoff *= 2
        raise AssertionError("unreachable")

    def get(self, url: str) -> tuple[int, str]:
        """Return (status, body) for a URL, via cache when fresh."""
        cached = self._cache_read(url)
        if cached is not None:
            self.cache_hits += 1
            return cached["status"], cached["body"]
        self._check_robots(url)
        resp = self._fetch_raw(url)
        if resp.status_code in (200, 404):  # cache 404s too (deleted users)
            self._cache_write(url, resp.status_code, resp.text)
        return resp.status_code, resp.text


# -- parsing ---------------------------------------------------------------

@dataclass
class FilmRating:
    slug: str
    title: str
    rating: int | None  # half-stars, 1..10; None if watched but unrated


_RATED_RE = re.compile(r"\brated-(\d+)\b")
_WATCHED_BY_RE = re.compile(r"Watched by ([\d,]+)(?:&nbsp;|\s)*members")


def parse_user_films_page(html: str) -> tuple[list[FilmRating], int | None]:
    """Parse one page of a user's films grid.

    Returns (films, last_page_number). last_page_number is None when the
    page carries no pagination block (single-page profiles).
    """
    soup = BeautifulSoup(html, "html.parser")
    films: list[FilmRating] = []
    for item in soup.select("li.griditem"):
        comp = item.select_one("div[data-item-slug]")
        if comp is None:
            continue
        slug = comp["data-item-slug"]
        title = comp.get("data-item-name") or slug
        rating = None
        rating_span = item.select_one("p.poster-viewingdata span.rating")
        if rating_span is not None:
            m = _RATED_RE.search(" ".join(rating_span.get("class", [])))
            if m:
                rating = int(m.group(1))
        films.append(FilmRating(slug=slug, title=title, rating=rating))

    last_page = None
    page_links = soup.select("div.paginate-pages li.paginate-page")
    if page_links:
        numbers = [int(li.get_text(strip=True))
                   for li in page_links
                   if li.get_text(strip=True).isdigit()]
        if numbers:
            last_page = max(numbers)
    return films, last_page


def parse_film_members_page(html: str) -> tuple[list[str], bool]:
    """Parse one page of a film's members table.

    Returns (usernames, has_next_page)."""
    soup = BeautifulSoup(html, "html.parser")
    users = []
    for cell in soup.select("td.col-member"):
        link = cell.select_one("a.name")
        if link and link.get("href"):
            users.append(link["href"].strip("/"))
    has_next = soup.select_one("a.next") is not None
    return users, has_next


def parse_film_stats(html: str) -> int | None:
    """Extract the watch count from the /csi/film/<slug>/stats/ fragment."""
    m = _WATCHED_BY_RE.search(html)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


# -- high-level fetchers -----------------------------------------------------

def fetch_user_ratings(session: PoliteSession, username: str,
                       max_pages: int | None = None,
                       ) -> dict[str, dict] | None:
    """Fetch a user's rated films.

    Returns {slug: {"title": ..., "rating": half_stars}} with only *rated*
    films, or None if the profile doesn't exist / isn't public.
    """
    ratings: dict[str, dict] = {}
    page = 1
    while True:
        suffix = "" if page == 1 else f"page/{page}/"
        status, body = session.get(f"{BASE_URL}/{username}/films/{suffix}")
        if status == 404:
            return None if page == 1 else ratings
        if status != 200:
            raise ScrapeError(f"unexpected HTTP {status} for {username} p{page}")
        films, last_page = parse_user_films_page(body)
        for f in films:
            if f.rating is not None:
                ratings[f.slug] = {"title": f.title, "rating": f.rating}
        if not films:
            break
        if last_page is not None and page >= last_page:
            break
        if last_page is None:  # no pagination block: single page
            break
        page += 1
        if max_pages is not None and page > max_pages:
            break
    return ratings


def fetch_film_raters(session: PoliteSession, slug: str,
                      max_pages: int = 4) -> list[str]:
    """Fetch usernames who rated a film (any rating), a few pages' worth."""
    users: list[str] = []
    for page in range(1, max_pages + 1):
        suffix = "" if page == 1 else f"page/{page}/"
        url = f"{BASE_URL}/film/{slug}/members/rated/.5-5/{suffix}"
        status, body = session.get(url)
        if status != 200:
            break
        page_users, has_next = parse_film_members_page(body)
        users.extend(page_users)
        if not has_next:
            break
    return users


def fetch_film_popularity(session: PoliteSession, slug: str) -> int | None:
    """Number of members who watched the film (its global popularity)."""
    status, body = session.get(f"{BASE_URL}/csi/film/{slug}/stats/")
    if status != 200:
        return None
    return parse_film_stats(body)
