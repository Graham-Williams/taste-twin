"""Viewer mode (TASTE_TWIN_VIEWER_MODE): read-only report gallery.

When on: homepage hides the username form and shows a note; POST /run refuses
to enqueue (still behind auth/host pin); no worker/ingest is started. When off,
behavior is unchanged (covered by test_web_routes.py)."""

import pytest

from tastetwin.web.app import create_app

APP_HOST = "taste-twin.example.com"


@pytest.fixture
def viewer_app(tmp_path, monkeypatch):
    for var in ("CF_ACCESS_AUD", "CF_ACCESS_TEAM_DOMAIN", "APP_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TASTE_TWIN_VIEWER_MODE", "1")
    # start_worker defaults True: viewer mode must suppress the worker itself.
    return create_app(data_dir=tmp_path / "data")


@pytest.fixture
def viewer_pinned_app(tmp_path, monkeypatch):
    for var in ("CF_ACCESS_AUD", "CF_ACCESS_TEAM_DOMAIN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APP_HOST", APP_HOST)
    monkeypatch.setenv("TASTE_TWIN_VIEWER_MODE", "1")
    return create_app(data_dir=tmp_path / "data", start_worker=False)


# -- homepage -----------------------------------------------------------------

def test_viewer_home_hides_form_and_shows_note(viewer_app):
    resp = viewer_app.test_client().get("/")
    assert resp.status_code == 200
    body = resp.data
    assert b"<form" not in body
    assert b'name="username"' not in body
    assert b"Find taste twins" not in body
    assert b"view-only gallery" in body


def test_viewer_home_keeps_runs_gallery(viewer_app):
    # Plant a finished CLI-style run; the gallery must still list it.
    data_dir = viewer_app.extensions["tastetwin_jobs"].data_dir
    run_dir = data_dir / "runs" / "someuser"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.html").write_text("<html>ok</html>")
    resp = viewer_app.test_client().get("/")
    assert resp.status_code == 200
    assert b"someuser" in resp.data
    assert b"/report/someuser" in resp.data


def test_viewer_does_not_start_worker(viewer_app):
    manager = viewer_app.extensions["tastetwin_jobs"]
    assert manager._thread is None


def test_viewer_boots_without_pool_db(viewer_app):
    manager = viewer_app.extensions["tastetwin_jobs"]
    assert not manager.pool_db_path.exists()  # nothing ingested
    assert viewer_app.test_client().get("/").status_code == 200


# -- POST /run refused --------------------------------------------------------

def test_viewer_post_run_refused_and_not_enqueued(viewer_app):
    resp = viewer_app.test_client().post("/run", data={"username": "gooduser"})
    assert resp.status_code == 403
    assert b"view-only" in resp.data
    assert not viewer_app.extensions["tastetwin_jobs"].list_runs()


def test_viewer_post_run_still_behind_host_pin(viewer_pinned_app):
    client = viewer_pinned_app.test_client()
    # Cross-origin POST rejected by the pin BEFORE the viewer refusal.
    assert client.post("/run", data={"username": "x"},
                       headers={"Host": APP_HOST,
                                "Origin": "https://evil.example.com"}
                       ).status_code == 403
    # Same-origin POST clears the pin, then hits the viewer refusal (403).
    resp = client.post("/run", data={"username": "x"},
                       headers={"Host": APP_HOST,
                                "Origin": f"https://{APP_HOST}"})
    assert resp.status_code == 403
    assert b"view-only" in resp.data
    assert not viewer_pinned_app.extensions["tastetwin_jobs"].list_runs()


# -- about --------------------------------------------------------------------

def test_viewer_about_notes_pregenerated(viewer_app):
    resp = viewer_app.test_client().get("/about")
    assert resp.status_code == 200
    assert b"pre-generated reports" in resp.data
