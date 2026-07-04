"""Regression tests for scraper/CLI hardening.

- scraped/CSV usernames & slugs must be validated before reaching a URL
- responses must stay on letterboxd.com and under the size cap
- Retry-After sleeps are clamped
- untrusted names are sanitized before becoming path components
"""

import pytest

from tastetwin import scraper
from tastetwin.scraper import (PoliteSession, ScrapeError, fetch_film_popularity,
                               fetch_film_raters, fetch_user_ratings,
                               is_valid_name, parse_film_members_page,
                               parse_user_films_page)
from tastetwin.util import safe_filename


class _NoNetworkSession:
    """Stands in for PoliteSession; fails the test if a URL is ever built."""

    def get(self, url):  # pragma: no cover - reaching this IS the failure
        raise AssertionError(f"network layer reached with {url!r}")


HOSTILE_NAMES = ["../etc", "a/b", "a?b=1", "a#frag", "a b", "", ".",
                 "..", "a%2fb?", "x\">y"]


class TestNameValidation:
    def test_valid_names(self):
        for name in ["gooduser", "Good_User-123", "a", "0-_"]:
            assert is_valid_name(name)

    def test_hostile_names_rejected(self):
        for name in HOSTILE_NAMES:
            assert not is_valid_name(name), name

    def test_members_parser_drops_hostile_hrefs(self):
        rows = "".join(
            f'<tr><td class="col-member">'
            f'<a class="name" href="/{name}/">x</a></td></tr>'
            for name in ["gooduser", "../etc", "a/b", "a?b", "a#b"])
        users, _ = parse_film_members_page(f"<table>{rows}</table>")
        assert users == ["gooduser"]

    def test_films_parser_drops_hostile_slugs(self):
        items = "".join(
            f'<li class="griditem">'
            f'<div data-item-slug="{slug}" data-item-name="T"></div></li>'
            for slug in ["good-film", "../evil", "a?b", "a#b", "a/b"])
        films, _ = parse_user_films_page(f"<ul>{items}</ul>")
        assert [f.slug for f in films] == ["good-film"]

    def test_fetch_user_ratings_never_builds_hostile_url(self):
        for name in HOSTILE_NAMES:
            assert fetch_user_ratings(_NoNetworkSession(), name) is None

    def test_fetch_film_raters_never_builds_hostile_url(self):
        for slug in HOSTILE_NAMES:
            assert fetch_film_raters(_NoNetworkSession(), slug) == []

    def test_fetch_film_popularity_never_builds_hostile_url(self):
        for slug in HOSTILE_NAMES:
            assert fetch_film_popularity(_NoNetworkSession(), slug) is None


class _FakeResp:
    def __init__(self, status=200, body=b"ok", headers=None,
                 url="https://letterboxd.com/x/"):
        self.status_code = status
        self.url = url
        self.headers = headers or {}
        self.encoding = "utf-8"
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


def _session_returning(tmp_path, responses):
    s = PoliteSession(cache_dir=tmp_path / "cache", min_delay=0)
    queue = list(responses)
    s._session = type("S", (), {
        "get": lambda self_, url, **kw: queue.pop(0),
        "headers": {},
    })()
    return s


class TestFetchBounds:
    def test_offsite_redirect_rejected(self, tmp_path):
        s = _session_returning(
            tmp_path, [_FakeResp(url="https://evil.example.com/x/")])
        with pytest.raises(ScrapeError, match="off-site"):
            s._fetch_raw("https://letterboxd.com/x/")

    def test_letterboxd_subdomain_allowed(self, tmp_path):
        s = _session_returning(
            tmp_path, [_FakeResp(url="https://s.letterboxd.com/x/")])
        assert s._fetch_raw("https://letterboxd.com/x/") == (200, "ok")

    def test_lookalike_host_rejected(self, tmp_path):
        s = _session_returning(
            tmp_path, [_FakeResp(url="https://notletterboxd.com/x/")])
        with pytest.raises(ScrapeError, match="off-site"):
            s._fetch_raw("https://letterboxd.com/x/")

    def test_oversized_body_aborted(self, tmp_path):
        big = b"x" * (scraper.MAX_RESPONSE_BYTES + 1)
        s = _session_returning(tmp_path, [_FakeResp(body=big)])
        with pytest.raises(ScrapeError, match="exceeds"):
            s._fetch_raw("https://letterboxd.com/x/")

    def test_body_at_cap_ok(self, tmp_path):
        body = b"x" * scraper.MAX_RESPONSE_BYTES
        s = _session_returning(tmp_path, [_FakeResp(body=body)])
        status, text = s._fetch_raw("https://letterboxd.com/x/")
        assert status == 200 and len(text) == scraper.MAX_RESPONSE_BYTES


class TestRetryAfterClamp:
    def test_huge_retry_after_is_clamped(self, tmp_path, monkeypatch):
        sleeps = []
        monkeypatch.setattr(scraper.time, "sleep", sleeps.append)
        s = _session_returning(tmp_path, [
            _FakeResp(status=429, headers={"Retry-After": "86400"}),
            _FakeResp(body=b"recovered"),
        ])
        assert s._fetch_raw("https://letterboxd.com/x/") == (200, "recovered")
        assert sleeps and max(sleeps) <= scraper.MAX_RETRY_AFTER_SECONDS


class TestSafeFilename:
    def test_traversal_neutralized(self):
        assert "/" not in safe_filename("../../etc/passwd")
        assert safe_filename("..") != ".."
        assert safe_filename(".") != "."
        assert safe_filename("...") not in (".", "..", "...")

    def test_normal_names_unchanged(self):
        assert safe_filename("Good_User-1.2") == "Good_User-1.2"

    def test_empty_becomes_placeholder(self):
        assert safe_filename("") == "_"
