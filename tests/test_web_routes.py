"""Web routes: username validation on POST, host/origin pinning, report
serving refuses non-existent/unsanitized names, status pages."""

import pytest

from tastetwin.web.app import create_app

APP_HOST = "taste-twin.example.com"


@pytest.fixture
def dev_app(tmp_path, monkeypatch):
    for var in ("CF_ACCESS_AUD", "CF_ACCESS_TEAM_DOMAIN", "APP_HOST"):
        monkeypatch.delenv(var, raising=False)
    return create_app(data_dir=tmp_path / "data", start_worker=False)


@pytest.fixture
def pinned_app(tmp_path, monkeypatch):
    for var in ("CF_ACCESS_AUD", "CF_ACCESS_TEAM_DOMAIN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APP_HOST", APP_HOST)
    return create_app(data_dir=tmp_path / "data", start_worker=False)


def _write_report(app, name: str, body: str = "<html>report</html>"):
    data_dir = app.extensions["tastetwin_jobs"].data_dir
    run_dir = data_dir / "runs" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.html").write_text(body)


# -- homepage / about ---------------------------------------------------------

def test_homepage_renders(dev_app):
    resp = dev_app.test_client().get("/")
    assert resp.status_code == 200
    assert b'name="username"' in resp.data


def test_about_renders_methodology(dev_app):
    resp = dev_app.test_client().get("/about")
    assert resp.status_code == 200
    assert b"Pearson correlation" in resp.data


def test_security_headers_present(dev_app):
    resp = dev_app.test_client().get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in resp.headers


# -- POST /run validation -----------------------------------------------------

def test_post_valid_username_enqueues_and_redirects(dev_app):
    resp = dev_app.test_client().post("/run", data={"username": "Good_user-1"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/run/Good_user-1")
    job = dev_app.extensions["tastetwin_jobs"].get("good_user-1")
    assert job is not None and job.status == "queued"


@pytest.mark.parametrize("bad", [
    "", "  ", "a b", "../etc", "a/b", "user.name", "<script>alert(1)</script>",
    "us\ner", "ûser", "a" * 65, "%2e%2e", "user;rm", "user?x=1",
])
def test_post_invalid_username_rejected(dev_app, bad):
    resp = dev_app.test_client().post("/run", data={"username": bad})
    assert resp.status_code == 400
    assert not dev_app.extensions["tastetwin_jobs"].list_runs()


def test_post_username_trims_surrounding_whitespace(dev_app):
    resp = dev_app.test_client().post("/run", data={"username": " user \n"})
    assert resp.status_code == 302
    assert dev_app.extensions["tastetwin_jobs"].get("user") is not None


def test_post_missing_field_rejected(dev_app):
    assert dev_app.test_client().post("/run", data={}).status_code == 400


# -- host / origin pin ----------------------------------------------------------

def test_pin_rejects_wrong_host(pinned_app):
    client = pinned_app.test_client()
    assert client.get("/", headers={"Host": "evil.example.com"}
                      ).status_code == 403
    assert client.post("/run", data={"username": "x"},
                       headers={"Host": "evil.example.com"}).status_code == 403


def test_pin_accepts_matching_host(pinned_app):
    resp = pinned_app.test_client().get("/", headers={"Host": APP_HOST})
    assert resp.status_code == 200


def test_pin_rejects_cross_origin_post(pinned_app):
    resp = pinned_app.test_client().post(
        "/run", data={"username": "someuser"},
        headers={"Host": APP_HOST, "Origin": "https://evil.example.com"})
    assert resp.status_code == 403


def test_pin_accepts_same_origin_post(pinned_app):
    resp = pinned_app.test_client().post(
        "/run", data={"username": "someuser"},
        headers={"Host": APP_HOST, "Origin": f"https://{APP_HOST}"})
    assert resp.status_code == 302


def test_pin_healthz_exempt(pinned_app):
    resp = pinned_app.test_client().get(
        "/healthz", headers={"Host": "localhost:8080"})
    assert resp.status_code == 200


# -- report serving --------------------------------------------------------------

def test_report_serves_existing_inline(dev_app):
    _write_report(dev_app, "someuser", "<html>hello twins</html>")
    resp = dev_app.test_client().get("/report/someuser")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    assert resp.headers["Content-Disposition"] == "inline"
    assert b"hello twins" in resp.data


def test_report_username_case_insensitive(dev_app):
    _write_report(dev_app, "someuser")
    assert dev_app.test_client().get("/report/SomeUser").status_code == 200


def test_report_404_for_missing_run(dev_app):
    assert dev_app.test_client().get("/report/nosuchuser").status_code == 404


@pytest.mark.parametrize("bad", [
    "..", "%2e%2e", "..%2f..%2fetc", "a.b", "a%00b", "x%2fy",
    "a" * 65,
])
def test_report_refuses_unsanitized_names(dev_app, bad):
    # Plant a file OUTSIDE runs/ that a traversal would reach.
    data_dir = dev_app.extensions["tastetwin_jobs"].data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "report.html").write_text("outside")
    resp = dev_app.test_client().get(f"/report/{bad}")
    assert resp.status_code == 404


def test_report_refuses_other_files_in_run_dir(dev_app):
    # Only report.html is servable — target.json etc. are not reachable
    # because the filename is fixed, but make sure a run dir without a
    # report 404s instead of erroring.
    data_dir = dev_app.extensions["tastetwin_jobs"].data_dir
    run_dir = data_dir / "runs" / "hasnoreport"
    run_dir.mkdir(parents=True)
    (run_dir / "target.json").write_text("{}")
    assert dev_app.test_client().get("/report/hasnoreport").status_code == 404


# -- status page ------------------------------------------------------------------

def test_status_unknown_user_404(dev_app):
    assert dev_app.test_client().get("/run/nosuchuser").status_code == 404


def test_status_invalid_name_404(dev_app):
    assert dev_app.test_client().get("/run/a.b").status_code == 404


def test_status_queued_shows_position_and_refreshes(dev_app):
    dev_app.test_client().post("/run", data={"username": "queueduser"})
    resp = dev_app.test_client().get("/run/queueduser")
    assert resp.status_code == 200
    assert b"queued" in resp.data
    assert b'http-equiv="refresh"' in resp.data


def test_status_cli_only_run_shows_report_link(dev_app):
    # A run made by the CLI (no job.json) must still be browsable.
    _write_report(dev_app, "cliuser")
    resp = dev_app.test_client().get("/run/cliuser")
    assert resp.status_code == 200
    assert b"/report/cliuser" in resp.data
    home = dev_app.test_client().get("/")
    assert b"cliuser" in home.data
