"""Parsing tests against saved real Letterboxd pages (no network)."""

from pathlib import Path

import pytest

from tastetwin.scraper import (_Robots, parse_film_members_page,
                               parse_film_stats, parse_user_films_page)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def films_html() -> str:
    return (FIXTURES / "user_films_page.html").read_text()


@pytest.fixture(scope="module")
def members_html() -> str:
    return (FIXTURES / "film_members_page.html").read_text()


class TestUserFilmsPage:
    def test_film_count(self, films_html):
        films, _ = parse_user_films_page(films_html)
        assert len(films) == 72  # full page of the grid

    def test_slugs_and_titles(self, films_html):
        films, _ = parse_user_films_page(films_html)
        assert all(f.slug for f in films)
        assert all(f.title for f in films)
        # slugs look like slugs
        assert all(" " not in f.slug for f in films)

    def test_ratings_are_halfstars(self, films_html):
        films, _ = parse_user_films_page(films_html)
        rated = [f for f in films if f.rating is not None]
        assert len(rated) >= 50  # this fixture page is mostly rated
        assert all(1 <= f.rating <= 10 for f in rated)
        # fixture-specific: rating histogram observed when saved
        assert sum(1 for f in rated if f.rating == 7) == 17

    def test_unrated_films_present(self, films_html):
        films, _ = parse_user_films_page(films_html)
        assert any(f.rating is None for f in films)

    def test_last_page(self, films_html):
        _, last_page = parse_user_films_page(films_html)
        assert last_page == 48  # this profile had 48 pages when saved


class TestFilmMembersPage:
    def test_member_count(self, members_html):
        users, _ = parse_film_members_page(members_html)
        assert len(users) == 25  # members table page size

    def test_usernames_are_clean(self, members_html):
        users, _ = parse_film_members_page(members_html)
        assert all(u and "/" not in u for u in users)

    def test_has_next(self, members_html):
        _, has_next = parse_film_members_page(members_html)
        assert has_next is True


class TestFilmStats:
    def test_watch_count(self):
        html = (FIXTURES / "film_stats_fragment.html").read_text()
        count = parse_film_stats(html)
        assert count is not None
        assert count > 1_000_000  # Parasite; 7.16M when saved

    def test_no_match(self):
        assert parse_film_stats("<div>nothing here</div>") is None


class TestRobots:
    ROBOTS = """
User-agent: SomeBot
Disallow: /

User-agent: *
Disallow: /*/by/*
Disallow: /films/year/*
Disallow: /*/friends/*
"""

    def test_allowed_paths(self):
        r = _Robots(self.ROBOTS)
        assert r.allowed("/someuser/films/")
        assert r.allowed("/film/parasite-2019/members/rated/.5-5/")
        assert r.allowed("/csi/film/parasite-2019/stats/")

    def test_disallowed_paths(self):
        r = _Robots(self.ROBOTS)
        assert not r.allowed("/film/parasite-2019/members/by/name/")
        assert not r.allowed("/films/year/2019/")
        assert not r.allowed("/someuser/friends/films/")

    def test_other_agent_group_ignored(self):
        # 'Disallow: /' belongs to SomeBot, not to us
        r = _Robots(self.ROBOTS)
        assert r.allowed("/")
        assert r.allowed("/anything/")
