"""Regression tests: remote-derived strings must render inert in reports.

Usernames, film slugs, and titles all come from scraped pages or the Kaggle
CSVs — a hostile value must never become live HTML or a live Markdown link.
"""

from urllib.parse import quote

from tastetwin.report import render_html, render_markdown
from tastetwin.similarity import Match

HOSTILE_SLUG = 'x"><script>alert(1)</script>'
HOSTILE_TITLE = '<img src=x onerror=alert(1)>'
HOSTILE_USER = 'eve"><script>alert(2)</script>'


def _match(username: str = "someuser", slug: str = "some-film") -> Match:
    return Match(username=username, score=0.9, pearson=0.95, overlap=30,
                 source="live", dataset_score=0.8,
                 shared_loves=[slug],
                 disagreements=[(slug, 1.2, -0.8)])


class TestHtmlEscaping:
    def test_hostile_slug_and_title_render_inert(self):
        titles = {HOSTILE_SLUG: HOSTILE_TITLE}
        out = render_html("target", [_match(slug=HOSTILE_SLUG)], titles, {})
        # no live markup from either the slug (href) or the title (text)
        assert "<script>" not in out
        assert "<img" not in out
        assert HOSTILE_SLUG not in out  # raw slug never appears verbatim
        # the slug is URL-encoded inside the href, then HTML-escaped
        assert quote(HOSTILE_SLUG, safe="") in out.replace("&amp;", "&")
        # the title survives, escaped, as visible text
        assert "&lt;img src=x onerror=alert(1)&gt;" in out

    def test_hostile_username_renders_inert(self):
        out = render_html(HOSTILE_USER, [_match(username=HOSTILE_USER)],
                          {}, {})
        assert "<script>" not in out
        assert HOSTILE_USER not in out
        assert "&lt;script&gt;" in out

    def test_href_attribute_cannot_be_broken_out_of(self):
        titles = {HOSTILE_SLUG: "Fine Title"}
        out = render_html("target", [_match(slug=HOSTILE_SLUG)], titles, {})
        # the '">' breakout sequence from the slug must not survive raw
        assert 'x">' not in out


class TestMarkdownEscaping:
    def test_hostile_title_cannot_forge_link(self):
        slug = "some-film"
        titles = {slug: "cool](javascript:alert(1))"}
        out = render_markdown("target", [_match(slug=slug)], titles, {})
        assert "](javascript:" not in out
        # brackets/parens arrive backslash-escaped
        assert "cool\\]\\(javascript:alert\\(1\\)\\)" in out

    def test_hostile_username_cannot_forge_link(self):
        user = "eve](javascript:alert(1))"
        out = render_markdown(user, [_match(username=user)], {}, {})
        assert "](javascript:" not in out
        # inside (...) the username is percent-encoded
        assert quote(user, safe="") in out

    def test_hostile_slug_is_percent_encoded_in_url(self):
        slug = "a-film)/../evil"
        out = render_markdown("target", [_match(slug=slug)], {slug: "T"}, {})
        assert f"https://letterboxd.com/film/{quote(slug, safe='')}/" in out
        assert ")/../evil" not in out

    def test_angle_brackets_and_backticks_escaped(self):
        slug = "some-film"
        titles = {slug: "<b>bold</b> `code`"}
        out = render_markdown("target", [_match(slug=slug)], titles, {})
        assert "<b>" not in out
        assert "`code`" not in out
        assert "\\<b\\>" in out


# Second-round finding: a newline inside a remote-derived title could start
# a new Markdown line and hoist block-level markup (headings, bare URLs)
# out of the link text and into the document structure.
INJECTION_TITLE = "Nice Film\n\n## INJECTED\n\nhttp://evil.example/phish"


class TestMarkdownBlockInjection:
    def test_newline_title_cannot_hoist_blocks(self):
        slug = "some-film"
        titles = {slug: INJECTION_TITLE}
        out = render_markdown("target", [_match(slug=slug)], titles, {})
        for line in out.splitlines():
            # no heading and no bare-URL line originating from the title
            assert not line.startswith("## INJECTED")
            assert not line.strip().startswith("http://evil.example")
        # the whole title stays inside the link text, flattened to one line
        assert ("[Nice Film ## INJECTED http://evil.example/phish]"
                "(https://letterboxd.com/film/some-film/)") in out

    def test_all_vertical_whitespace_flattened(self):
        slug = "some-film"
        for ws in ["\n", "\r", "\r\n", "\v", "\f", "\x85",
                   "\u2028", "\u2029"]:
            titles = {slug: f"A{ws}{ws}## B"}
            out = render_markdown("target", [_match(slug=slug)], titles, {})
            assert "[A ## B](" in out, repr(ws)
            assert not any(line.startswith("## B")
                           for line in out.splitlines()), repr(ws)

    def test_html_text_flattened_for_consistency(self):
        slug = "some-film"
        out = render_html("target", [_match(slug=slug)],
                          {slug: INJECTION_TITLE}, {})
        assert ">Nice Film ## INJECTED http://evil.example/phish</a>" in out
