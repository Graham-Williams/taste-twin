"""HTTPS enforcement at the origin: http->https redirect, HSTS, cookie flags.

Defence in depth behind the Cloudflare edge (repo issue #9). The load-bearing
rules being pinned here:

- Redirect ONLY when X-Forwarded-Proto, trimmed and case-folded, is exactly
  "http". Schemes are case-insensitive (RFC 3986/9110), so "HTTP" must redirect
  too — a case-SENSITIVE check fails in the dangerous direction (plain http
  served 200). A multi-hop "http, https" must still NOT redirect. An ABSENT
  header must never redirect — the compose healthcheck calls
  http://127.0.0.1:8080/healthz in-network with no such header, and so do
  local dev and this test suite.
- The redirect is a 307 (not a 301) and carries Cache-Control: no-store +
  Vary: X-Forwarded-Proto. The Location is byte-identical to the request URL,
  so a cacheable 301 could be stored by a shared cache and replayed to https
  visitors (broken /static assets, or a loop); 307 also keeps the method so a
  plain-http POST is re-sent rather than downgraded to a bodiless GET.
- The Location is always built from the configured APP_HOST, never from the
  request's own Host header (that would be an open redirect).
- APP_HOST unset => no redirect at all (fail open).
- Path + query must survive byte-for-byte, percent-encoding included.
"""

import pytest

from tastetwin.web.app import create_app

APP_HOST = "taste-twin.example.com"
HSTS = "max-age=31536000"


@pytest.fixture
def pinned_app(tmp_path, monkeypatch):
    for var in ("CF_ACCESS_AUD", "CF_ACCESS_TEAM_DOMAIN", "APP_PASSWORD",
                "TASTE_TWIN_VIEWER_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APP_HOST", APP_HOST)
    return create_app(data_dir=tmp_path / "data", start_worker=False)


@pytest.fixture
def unpinned_app(tmp_path, monkeypatch):
    for var in ("CF_ACCESS_AUD", "CF_ACCESS_TEAM_DOMAIN", "APP_HOST",
                "APP_PASSWORD", "TASTE_TWIN_VIEWER_MODE"):
        monkeypatch.delenv(var, raising=False)
    return create_app(data_dir=tmp_path / "data", start_worker=False)


def _get(app, path, proto=None, host=APP_HOST, **kwargs):
    headers = dict(kwargs.pop("headers", {}))
    if proto is not None:
        headers["X-Forwarded-Proto"] = proto
    return app.test_client().get(
        path, headers=headers, base_url=f"https://{host}", **kwargs)


# -- redirect: the happy path -------------------------------------------------

def test_xfp_http_redirects_to_https(pinned_app):
    resp = _get(pinned_app, "/", proto="http")
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{APP_HOST}/"


def test_redirect_preserves_path_and_query(pinned_app):
    resp = _get(pinned_app, "/run/someone?a=1&b=two", proto="http")
    assert resp.status_code == 307
    assert resp.headers["Location"] == (
        f"https://{APP_HOST}/run/someone?a=1&b=two")


def test_redirect_preserves_percent_encoding(pinned_app):
    """request.path is URL-DECODED, so a naive f-string would mangle these."""
    resp = _get(pinned_app, "/report/a%20b%3Fc%2Fd?q=1%202&z=%3F%26",
                proto="http")
    assert resp.status_code == 307
    assert resp.headers["Location"] == (
        f"https://{APP_HOST}/report/a%20b%3Fc%2Fd?q=1%202&z=%3F%26")
    # Specifically: no decoded space, no decoded '?' or '/' inside the path.
    assert " " not in resp.headers["Location"]


def test_redirect_runs_before_the_password_gate(tmp_path, monkeypatch):
    """A plain-http visitor is upgraded before any credential handling."""
    for var in ("CF_ACCESS_AUD", "CF_ACCESS_TEAM_DOMAIN",
                "TASTE_TWIN_VIEWER_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APP_HOST", APP_HOST)
    monkeypatch.setenv("APP_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-session-secret")
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    resp = _get(app, "/", proto="http")
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{APP_HOST}/"


def test_redirect_applies_in_viewer_mode(tmp_path, monkeypatch):
    for var in ("CF_ACCESS_AUD", "CF_ACCESS_TEAM_DOMAIN", "APP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APP_HOST", APP_HOST)
    monkeypatch.setenv("TASTE_TWIN_VIEWER_MODE", "1")
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    assert _get(app, "/", proto="http").status_code == 307
    # ...and viewer mode itself is otherwise untouched.
    assert _get(app, "/", proto="https").status_code == 200


# -- redirect: no host reflection (open-redirect defence) ---------------------

@pytest.mark.parametrize("evil_host", [
    "evil.example.net",
    "taste-twin.example.com.evil.net",
    "localhost:1337",
])
def test_crafted_host_is_not_reflected(pinned_app, evil_host):
    resp = _get(pinned_app, "/", proto="http", host=evil_host)
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{APP_HOST}/"
    assert evil_host.split(":")[0] not in resp.headers["Location"]


def test_crafted_host_header_is_not_reflected(pinned_app):
    """Same, but with Host supplied as an explicit header override."""
    resp = pinned_app.test_client().get(
        "/", headers={"X-Forwarded-Proto": "http", "Host": "evil.example.net"},
        base_url=f"https://{APP_HOST}")
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{APP_HOST}/"


# -- no redirect: the cases that must stay untouched --------------------------

def test_xfp_https_is_not_redirected(pinned_app):
    resp = _get(pinned_app, "/", proto="https")
    assert resp.status_code == 200


def test_absent_xfp_is_not_redirected(pinned_app):
    """The documented in-network healthcheck sends no X-Forwarded-Proto."""
    resp = pinned_app.test_client().get(
        "/healthz", base_url="http://127.0.0.1:8080")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}
    assert "Location" not in resp.headers


def test_absent_xfp_on_a_normal_route_is_not_redirected(pinned_app):
    assert _get(pinned_app, "/").status_code == 200


@pytest.mark.parametrize("proto", ["https", "HTTPS", "http, https",
                                   "https, http", "", " ", "httpx", "xhttp",
                                   "ws"])
def test_values_that_must_not_redirect(pinned_app, proto):
    """Anything that isn't an unambiguous single "http" fails open.

    "http, https" in particular is a MULTI-HOP value (two proxies appended
    their own scheme); we refuse to guess which hop the visitor was on.
    """
    resp = _get(pinned_app, "/healthz", proto=proto)
    assert resp.status_code == 200
    assert "Location" not in resp.headers


@pytest.mark.parametrize("proto", ["http", "HTTP", "Http", "hTTp", " http",
                                   "http ", "  HTTP\t"])
def test_http_is_matched_case_insensitively_and_trimmed(pinned_app, proto):
    """URI schemes are case-insensitive (RFC 3986 §3.1, RFC 9110).

    A case-SENSITIVE `!= "http"` failed in the dangerous direction: measured
    live against gunicorn, `X-Forwarded-Proto: HTTP` was served 200 over plain
    http with no upgrade at all.
    """
    resp = _get(pinned_app, "/", proto=proto)
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{APP_HOST}/"


# -- the redirect must not be cached ------------------------------------------

def test_redirect_is_not_cacheable_and_declares_its_vary(pinned_app):
    """Location is byte-identical to the request URL, so a cacheable redirect
    is a trap: a shared cache (Cloudflare caches .css/.js by default) could
    store it and replay it to https visitors — broken assets, or a loop. The
    response depends on X-Forwarded-Proto, so it has to say so."""
    resp = _get(pinned_app, "/static/does-not-matter.css", proto="http")
    assert resp.status_code == 307
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Vary"] == "X-Forwarded-Proto"


def test_redirect_is_307_not_301(pinned_app):
    """307 preserves the method: a plain-http POST is re-sent over https
    instead of being silently downgraded to a bodiless GET. HSTS already
    supplies the durable client-side upgrade, so permanence buys nothing."""
    resp = pinned_app.test_client().post(
        "/run", headers={"X-Forwarded-Proto": "http"},
        data={"username": "someone"}, base_url=f"https://{APP_HOST}")
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{APP_HOST}/run"


def test_no_redirect_when_app_host_unset(unpinned_app):
    """Fail open: local dev / CLI / tests keep working over plain http."""
    resp = _get(unpinned_app, "/", proto="http", host="localhost")
    assert resp.status_code == 200


@pytest.mark.parametrize("bad_host", [
    "https://taste-twin.example.com",
    "taste-twin.example.com/evil",
    "taste-twin.example.com:8080",
    "evil.net\r\nX-Injected: 1",
])
def test_malformed_app_host_disables_the_redirect(tmp_path, monkeypatch,
                                                  bad_host):
    """A bad APP_HOST must never reach a Location header."""
    for var in ("CF_ACCESS_AUD", "CF_ACCESS_TEAM_DOMAIN", "APP_PASSWORD",
                "TASTE_TWIN_VIEWER_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APP_HOST", bad_host)
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    resp = app.test_client().get(
        "/healthz", headers={"X-Forwarded-Proto": "http"},
        base_url="https://anything.example")
    assert resp.status_code == 200
    assert "Location" not in resp.headers


# -- HSTS ---------------------------------------------------------------------

def test_hsts_header_exact_value(pinned_app):
    resp = _get(pinned_app, "/", proto="https")
    assert resp.headers["Strict-Transport-Security"] == HSTS
    # No includeSubDomains, no preload — each host owns its own policy.
    assert "includeSubDomains" not in resp.headers[
        "Strict-Transport-Security"]
    assert "preload" not in resp.headers["Strict-Transport-Security"]


@pytest.mark.parametrize("path", ["/", "/about", "/healthz"])
def test_hsts_on_every_route(pinned_app, path):
    resp = _get(pinned_app, path, proto="https")
    assert resp.headers["Strict-Transport-Security"] == HSTS


def test_hsts_on_the_redirect_itself(pinned_app):
    resp = _get(pinned_app, "/", proto="http")
    assert resp.status_code == 307
    assert resp.headers["Strict-Transport-Security"] == HSTS


def test_hsts_present_without_app_host(unpinned_app):
    """HSTS does not depend on the pin — it ships in dev mode too."""
    resp = _get(unpinned_app, "/", host="localhost")
    assert resp.headers["Strict-Transport-Security"] == HSTS


def test_referrer_policy_unchanged(pinned_app):
    """Must stay same-origin: under no-referrer a real browser sends
    `Origin: null` and no Referer on the app's own form POST, which the CSRF
    host/origin pin then 403s (learned in PR #3; not reproducible in the test
    client). Do not "harden" this alongside HSTS."""
    resp = _get(pinned_app, "/", proto="https")
    assert resp.headers["Referrer-Policy"] == "same-origin"


# -- session cookie flags -----------------------------------------------------

def test_session_cookie_config_flags(pinned_app):
    cfg = pinned_app.config
    assert cfg["SESSION_COOKIE_SECURE"] is True
    assert cfg["SESSION_COOKIE_HTTPONLY"] is True
    assert cfg["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_login_cookie_is_secure_httponly_samesite(tmp_path, monkeypatch):
    for var in ("CF_ACCESS_AUD", "CF_ACCESS_TEAM_DOMAIN", "APP_HOST",
                "TASTE_TWIN_VIEWER_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APP_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-session-secret")
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    resp = app.test_client().post(
        "/login", data={"password": "correct horse battery staple"},
        base_url="https://localhost")
    set_cookie = "\n".join(
        v for k, v in resp.headers if k.lower() == "set-cookie")
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=Lax" in set_cookie
