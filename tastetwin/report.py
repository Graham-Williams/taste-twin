"""Report generation: report.md + a standalone report.html."""

from __future__ import annotations

import html as html_mod
import re
from datetime import date
from pathlib import Path
from urllib.parse import quote

from .similarity import Match

PROFILE_URL = "https://letterboxd.com/{user}/"
FILM_URL = "https://letterboxd.com/film/{slug}/"


def _profile_url(user: str) -> str:
    """Profile URL with the (remote-derived) username URL-encoded."""
    return PROFILE_URL.format(user=quote(user, safe=""))


def _film_url(slug: str) -> str:
    """Film URL with the (remote-derived) slug URL-encoded."""
    return FILM_URL.format(slug=quote(slug, safe=""))


# Markdown link-text metacharacters: escaping these keeps remote-derived
# strings (titles, usernames, slugs) from breaking out of [text](url) or
# injecting markup. Backslash itself must be in the set.
_MD_SPECIALS = "\\[]()<>`"

# Any run of whitespace — including \r, \n, \v, \f and Unicode line/paragraph
# separators (U+0085, U+2028, U+2029), all matched by \s — collapses to one
# space. Without this, a newline inside a remote-derived title would start a
# new Markdown line and could hoist block-level markup (headings, bare URLs)
# out of its link text and into the document structure.
_WS_RUN_RE = re.compile(r"\s+")


def _flatten_ws(text: str) -> str:
    """Collapse all vertical/horizontal whitespace runs to a single space."""
    return _WS_RUN_RE.sub(" ", text)


def _md_text(text: str) -> str:
    return "".join(
        "\\" + ch if ch in _MD_SPECIALS else ch for ch in _flatten_ws(text))

METHODOLOGY = (
    "Every rater has a personal scale — one person's 3 stars is another's "
    "4.5. To compare fairly, each user's ratings are first normalized "
    "against their own average and spread (z-scores), so \"loved it by "
    "their standards\" means the same thing for everyone. Similarity is the "
    "Pearson correlation of two users' ratings over the films both have "
    "rated (equivalent to comparing those normalized scores), and it is "
    "then shrunk for small overlaps — agreeing on 12 films is weaker "
    "evidence than agreeing on 50 — by multiplying by "
    "min(overlap, 50) / 50. A score near 1.0 means: large overlap, and "
    "within it you consistently love and dislike the same films. "
    "The candidate pool is a public snapshot of ~11,000 of Letterboxd's "
    "most active members (Kaggle: freeth/letterboxd-film-ratings, CC0, "
    "Oct 2023); matches marked “verified live” were re-scored "
    "against the account's current public ratings."
)

_SOURCE_LABELS = {
    "live": "verified live",
    "dataset": "dataset snapshot (Oct 2023)",
    "scraped": "scraped pool",
}


def _source_note(m: Match) -> str:
    label = _SOURCE_LABELS.get(m.source, m.source)
    if m.source == "live" and m.dataset_score is not None:
        label += f"; dataset score was {m.dataset_score:.3f}"
    return label


def _fmt_loves(match: Match, titles: dict[str, str],
               popularity: dict[str, int], limit: int = 5) -> list[str]:
    """Shared loves, obscure first (films with known low watch-counts lead)."""
    ordered = sorted(
        match.shared_loves,
        key=lambda slug: popularity.get(slug, 10**9))
    return ordered[:limit]


def _title(slug: str, titles: dict[str, str]) -> str:
    return titles.get(slug, slug.replace("-", " ").title())


def render_markdown(target: str, matches: list[Match],
                    titles: dict[str, str], popularity: dict[str, int],
                    top_n: int = 20) -> str:
    lines = [
        f"# Taste twins for [{_md_text(target)}]({_profile_url(target)})",
        "",
        f"*Generated {date.today().isoformat()} · top {min(top_n, len(matches))} "
        f"of {len(matches)} scored candidates*",
        "",
    ]
    for rank, m in enumerate(matches[:top_n], 1):
        lines.append(
            f"## {rank}. [{_md_text(m.username)}]({_profile_url(m.username)})"
            f" — score {m.score:.3f}")
        lines.append("")
        lines.append(f"- **Pearson r:** {m.pearson:.3f} over "
                     f"**{m.overlap}** co-rated films")
        lines.append(f"- **Data:** {_md_text(_source_note(m))}")
        loves = _fmt_loves(m, titles, popularity)
        if loves:
            loved = ", ".join(
                f"[{_md_text(_title(s, titles))}]({_film_url(s)})"
                for s in loves)
            lines.append(f"- **Films you both love:** {loved}")
        if m.disagreements:
            dis = "; ".join(
                f"[{_md_text(_title(s, titles))}]({_film_url(s)}) "
                f"(you {tz:+.1f}σ, them {cz:+.1f}σ)"
                for s, tz, cz in m.disagreements[:3])
            lines.append(f"- **Biggest disagreements:** {dis}")
        lines.append("")
    lines += ["---", "", f"**Methodology.** {METHODOLOGY}", ""]
    return "\n".join(lines)


_CSS = """
  body { font-family: Georgia, 'Times New Roman', serif; background: #14181c;
         color: #d8e0e8; max-width: 720px; margin: 2rem auto; padding: 0 1rem;
         line-height: 1.55; }
  h1 { font-family: 'Helvetica Neue', Arial, sans-serif; color: #fff;
       border-bottom: 3px solid #00c030; padding-bottom: .4rem; }
  .sub { color: #89a; font-style: italic; }
  .match { background: #1b2228; border-radius: 10px; padding: 1rem 1.25rem;
           margin: 1rem 0; border-left: 4px solid #00c030; }
  .match h2 { margin: 0 0 .3rem; font-family: 'Helvetica Neue', Arial,
              sans-serif; font-size: 1.15rem; }
  .match .stats { color: #9ab; font-size: .9rem; margin-bottom: .5rem; }
  .score { color: #00e054; font-weight: bold; }
  a { color: #40bcf4; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .label { color: #789; font-variant: small-caps; letter-spacing: .04em; }
  .foot { color: #9ab; font-size: .85rem; border-top: 1px solid #345;
          margin-top: 2rem; padding-top: 1rem; }
"""


def render_html(target: str, matches: list[Match], titles: dict[str, str],
                popularity: dict[str, int], top_n: int = 20) -> str:
    def esc(text: str) -> str:
        # Flatten whitespace for consistency with the Markdown renderer
        # (harmless in HTML — escaping alone already neutralizes markup —
        # but keeps remote-derived strings uniform across both outputs).
        # URLs passed through here are already percent-encoded, so they
        # contain no whitespace to flatten.
        return html_mod.escape(_flatten_ws(text))

    def film_link(slug: str) -> str:
        # slug is remote-derived: URL-encode it, then HTML-escape the
        # whole attribute value.
        return (f'<a href="{esc(_film_url(slug))}">'
                f'{esc(_title(slug, titles))}</a>')

    cards = []
    for rank, m in enumerate(matches[:top_n], 1):
        loves = _fmt_loves(m, titles, popularity)
        loves_html = (
            f'<div><span class="label">films you both love</span> — '
            f'{", ".join(film_link(s) for s in loves)}</div>' if loves else "")
        dis_html = ""
        if m.disagreements:
            items = "; ".join(
                f'{film_link(s)} (you {tz:+.1f}&sigma;, them {cz:+.1f}&sigma;)'
                for s, tz, cz in m.disagreements[:3])
            dis_html = (f'<div><span class="label">biggest disagreements'
                        f'</span> — {items}</div>')
        cards.append(f"""
  <div class="match">
    <h2>{rank}. <a href="{esc(_profile_url(m.username))}">{esc(m.username)}</a>
        <span class="score">score {m.score:.3f}</span></h2>
    <div class="stats">Pearson r = {m.pearson:.3f} over {m.overlap} co-rated
        films &middot; {esc(_source_note(m))}</div>
    {loves_html}
    {dis_html}
  </div>""")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Taste twins for {esc(target)}</title>
<style>{_CSS}</style>
</head>
<body>
  <h1>Taste twins for <a href="{esc(_profile_url(target))}">{esc(target)}</a></h1>
  <p class="sub">Generated {date.today().isoformat()} &middot;
     top {min(top_n, len(matches))} of {len(matches)} scored candidates</p>
  {''.join(cards)}
  <div class="foot"><strong>Methodology.</strong> {esc(METHODOLOGY)}</div>
</body>
</html>
"""


def write_reports(run_dir: Path, target: str, matches: list[Match],
                  titles: dict[str, str], popularity: dict[str, int],
                  top_n: int = 20) -> tuple[Path, Path]:
    md_path = run_dir / "report.md"
    html_path = run_dir / "report.html"
    md_path.write_text(
        render_markdown(target, matches, titles, popularity, top_n))
    html_path.write_text(
        render_html(target, matches, titles, popularity, top_n))
    return md_path, html_path
