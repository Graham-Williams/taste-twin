"""Tests for scripts/publish.py (the Mac-side generate-and-publish CLI).

Focus: the username is charset/length validated (reusing the app's
is_valid_name) BEFORE it can reach any subprocess/ssh/docker command, and the
script only ever uses subprocess ARG LISTS (never shell=True)."""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLISH_PATH = REPO_ROOT / "scripts" / "publish.py"


def _load_publish():
    spec = importlib.util.spec_from_file_location("publish", PUBLISH_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


publish = _load_publish()


HOSTILE = [
    "",
    "   ",
    "../etc",
    "a/b",
    "user;rm -rf /",
    "user name",
    "$(whoami)",
    "`id`",
    "user|nc",
    "user&&reboot",
    "user\nrm",
    "a" * 65,
    "user.name",
    "%2e%2e",
    "<script>",
]


@pytest.mark.parametrize("bad", HOSTILE)
def test_validate_rejects_hostile(bad):
    with pytest.raises(publish.PublishError):
        publish.validate_username(bad)


@pytest.mark.parametrize("good", ["gooduser", "Good_user-1", "a", "A1_-"])
def test_validate_accepts_good(good):
    assert publish.validate_username(good) == good


@pytest.mark.parametrize("bad", HOSTILE)
def test_main_never_invokes_subprocess_for_bad_name(bad, monkeypatch):
    calls = []
    monkeypatch.setattr(publish.subprocess, "run",
                        lambda *a, **k: calls.append((a, k)))
    rc = publish.main([bad])
    assert rc == 1
    assert calls == []  # validation refused before any command was built


def test_pipeline_uses_arg_list_with_current_python(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        # Simulate a successful pipeline that produced a report.
        run_dir = tmp_path / "runs" / "gooduser"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.html").write_text("<html>ok</html>")

        class _P:
            returncode = 0
        return _P()

    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    run_dir = publish.generate_report("gooduser", tmp_path)

    cmd = captured["cmd"]
    assert isinstance(cmd, list)  # ARG LIST, not a shell string
    assert cmd[0] == publish.sys.executable
    assert cmd[1:] == ["-m", "tastetwin", "run", "gooduser"]
    assert captured["kwargs"].get("shell") is not True
    assert (run_dir / "report.html").is_file()


def test_pipeline_failure_aborts(monkeypatch, tmp_path):
    class _P:
        returncode = 2

    monkeypatch.setattr(publish.subprocess, "run", lambda *a, **k: _P())
    with pytest.raises(publish.PublishError):
        publish.generate_report("gooduser", tmp_path)


def test_missing_report_aborts(monkeypatch, tmp_path):
    class _P:
        returncode = 0  # "succeeds" but writes no report

    monkeypatch.setattr(publish.subprocess, "run", lambda *a, **k: _P())
    with pytest.raises(publish.PublishError):
        publish.generate_report("gooduser", tmp_path)


def test_publish_to_box_builds_only_arg_lists(monkeypatch, tmp_path):
    # Only report.html + matches_verified.json may leave the machine, and
    # every command must be an arg list with no shell=True.
    run_dir = tmp_path / "runs" / "gooduser"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.html").write_text("r")
    (run_dir / "matches_verified.json").write_text("[]")
    (run_dir / "matches_dataset.json").write_text("HUGE")  # must NOT be shipped

    seen = []

    class _P:
        returncode = 0

    def fake_run(cmd, **kwargs):
        assert isinstance(cmd, list)
        assert kwargs.get("shell") is not True
        seen.append(cmd)
        return _P()

    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    publish.publish_to_box(run_dir, "gooduser", "graham@box", "taste-twin")

    flat = " ".join(" ".join(c) for c in seen)
    assert "matches_dataset.json" not in flat
    assert "report.html" in flat
    assert "matches_verified.json" in flat
    # scp targets a temp dir; docker cp lands in the container run dir.
    assert any(c[0] == "scp" for c in seen)
    assert any("docker" in c and "cp" in c for c in seen)


def test_no_shell_true_anywhere_in_source():
    src = PUBLISH_PATH.read_text()
    assert "shell=True" not in src
