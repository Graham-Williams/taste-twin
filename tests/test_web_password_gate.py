"""App-level shared-password gate: env-gating, redirect-to-login, correct /
wrong password, rate limiting, exempt routes (static + health), open-redirect
safety, session-cookie flags, and that viewer mode still works behind the gate.

SESSION_COOKIE_SECURE is True (correct for prod behind TLS), so the Werkzeug
test client only re-sends the session cookie over https — hence base_url below.
"""

import re

import pytest

from tastetwin.web import password_gate
from tastetwin.web.app import create_app

PASSWORD = "correct horse battery staple"
HTTPS = "https://localhost"


@pytest.fixture
def gated_app(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-session-secret")
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("APP_HOST", raising=False)
    monkeypatch.delenv("TASTE_TWIN_VIEWER_MODE", raising=False)
    return create_app(data_dir=tmp_path / "data", start_worker=False)


def _login(client, password=PASSWORD, next_target=None):
    data = {"password": password}
    if next_target is not None:
        data["next"] = next_target
    return client.post("/login", data=data, base_url=HTTPS)


# -- env gating --------------------------------------------------------------

def test_gate_off_when_password_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("APP_HOST", raising=False)
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    # No login required at all.
    assert app.test_client().get("/", base_url=HTTPS).status_code == 200
    assert app.test_client().get("/about", base_url=HTTPS).status_code == 200


def test_gate_off_empty_password_treated_as_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "")
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("APP_HOST", raising=False)
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    assert app.test_client().get("/", base_url=HTTPS).status_code == 200


# -- gate blocks unauthenticated protected routes ----------------------------

def test_unauth_protected_route_redirects_to_login(gated_app):
    resp = gated_app.test_client().get("/", base_url=HTTPS)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_unauth_redirect_preserves_next(gated_app):
    resp = gated_app.test_client().get("/about", base_url=HTTPS)
    assert resp.status_code == 302
    assert "next=/about" in resp.headers["Location"]


def test_all_protected_routes_gated(gated_app):
    client = gated_app.test_client()
    for path in ("/", "/about", "/run/someone", "/report/someone"):
        assert client.get(path, base_url=HTTPS).status_code == 302


# -- exempt routes stay open -------------------------------------------------

def test_healthz_exempt(gated_app):
    assert gated_app.test_client().get("/healthz", base_url=HTTPS).status_code == 200


def test_login_page_reachable_unauthenticated(gated_app):
    resp = gated_app.test_client().get("/login", base_url=HTTPS)
    assert resp.status_code == 200
    assert b"password" in resp.data.lower()


def test_static_path_exempt(gated_app):
    # No static files exist, so a 404 (not a 302 to /login) proves the static
    # prefix is exempt from the gate rather than being redirected.
    resp = gated_app.test_client().get("/static/nope.css", base_url=HTTPS)
    assert resp.status_code == 404


# -- correct / wrong password ------------------------------------------------

def test_correct_password_grants_access(gated_app):
    client = gated_app.test_client()
    resp = _login(client)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    # Session cookie now carries the marker -> protected route is reachable.
    assert client.get("/", base_url=HTTPS).status_code == 200


def test_wrong_password_rejected(gated_app):
    client = gated_app.test_client()
    resp = _login(client, password="nope")
    assert resp.status_code == 401
    # Still not authenticated.
    assert client.get("/", base_url=HTTPS).status_code == 302


def test_session_cookie_flags(gated_app):
    resp = _login(gated_app.test_client())
    set_cookie = "\n".join(
        v for k, v in resp.headers if k.lower() == "set-cookie")
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=Lax" in set_cookie
    # The raw password must never appear in the cookie.
    assert "battery" not in set_cookie


def test_logout_clears_session(gated_app):
    client = gated_app.test_client()
    _login(client)
    assert client.get("/", base_url=HTTPS).status_code == 200
    client.get("/logout", base_url=HTTPS)
    assert client.get("/", base_url=HTTPS).status_code == 302


# -- rate limiting -----------------------------------------------------------

def test_rate_limit_trips_after_many_failures(gated_app):
    client = gated_app.test_client()
    # 10 failures within the window -> the 11th is blocked with 429.
    for _ in range(10):
        assert _login(client, password="wrong").status_code == 401
    blocked = _login(client, password="wrong")
    assert blocked.status_code == 429
    # Even the CORRECT password is refused while blocked.
    assert _login(client, password=PASSWORD).status_code == 429


def test_rate_limit_window_expiry(monkeypatch):
    limiter = password_gate.LoginRateLimiter(max_failures=3, window_seconds=100)
    ip = "1.2.3.4"
    for t in (0, 10, 20):
        limiter.record_failure(ip, now=t)
    assert limiter.is_blocked(ip, now=25)
    # After the window slides past all three failures, unblocked.
    assert not limiter.is_blocked(ip, now=200)


def test_rate_limiter_bounds_memory_on_ip_flood():
    # A sustained flood of distinct one-shot IPs must not grow _fails without
    # bound: once the table hits the cap, a sweep drops IPs whose failures have
    # aged out, so retained size is bounded by the window (not by total IPs
    # ever seen). 200 distinct IPs over time, but only ~1 window's worth stay.
    limiter = password_gate.LoginRateLimiter(
        max_failures=10, window_seconds=100, max_tracked_ips=50)
    for i in range(200):
        limiter.record_failure(f"10.0.0.{i}", now=float(i))
    # Far fewer than the 200 distinct IPs seen — old ones were swept.
    assert len(limiter._fails) <= 110


# -- open-redirect safety ----------------------------------------------------

@pytest.mark.parametrize("evil", [
    "//evil.com",
    "https://evil.com",
    "http://evil.com/x",
    "/\\evil.com",
    "javascript:alert(1)",
])
def test_open_redirect_next_neutralized(gated_app, evil):
    client = gated_app.test_client()
    resp = _login(client, next_target=evil)
    assert resp.status_code == 302
    # Redirect must stay on-site (relative "/"), never off to the evil host.
    loc = resp.headers["Location"]
    assert loc.endswith("/") and "evil" not in loc and ":" not in loc.split("/")[-1]


def test_local_next_is_honored(gated_app):
    resp = _login(gated_app.test_client(), next_target="/about")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/about")


# -- viewer mode still works behind the gate ---------------------------------

def test_viewer_mode_works_behind_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-session-secret")
    monkeypatch.setenv("TASTE_TWIN_VIEWER_MODE", "1")
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("APP_HOST", raising=False)
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    client = app.test_client()
    # Gated before login.
    assert client.get("/", base_url=HTTPS).status_code == 302
    _login(client)
    # After login the viewer-mode gallery renders (form hidden, POST /run 403).
    home = client.get("/", base_url=HTTPS)
    assert home.status_code == 200
    assert b"view-only" in home.data
    run = client.post("/run", data={"username": "someone"}, base_url=HTTPS)
    assert run.status_code == 403
