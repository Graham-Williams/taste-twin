"""Job queue behavior: pending cap, strict one-at-a-time execution,
duplicate rejection, state persistence, restart recovery — with a mocked
pipeline runner (no network, no pool DB)."""

import json
import threading
import time

import pytest

from tastetwin.pipeline import PipelineError
from tastetwin.web import jobs as jobs_mod
from tastetwin.web.jobs import INTERRUPTED_ERROR, Job, JobManager


def wait_until(predicate, timeout=5.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "pool.db").write_bytes(b"")  # pretend ingest already happened
    return d


class BlockingRunner:
    """Runner that blocks until released; records concurrency."""

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.ran: list[str] = []

    def __call__(self, job: Job) -> None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.ran.append(job.username)
        self.started.set()
        try:
            assert self.release.wait(timeout=10)
        finally:
            with self._lock:
                self.active -= 1


def test_enqueue_validates_username(data_dir):
    mgr = JobManager(data_dir, runner=lambda job: None)
    for bad in ("", "a b", "../x", "<u>", "a" * 65):
        job, err = mgr.enqueue(bad)
        assert job is None and err


def test_pending_cap(data_dir):
    runner = BlockingRunner()
    mgr = JobManager(data_dir, runner=runner, max_pending=5)
    mgr.start()
    try:
        job, _ = mgr.enqueue("user0")
        assert job is not None
        assert wait_until(lambda: runner.started.is_set())  # user0 running
        for i in range(1, 6):  # 5 pending — fills the cap
            job, err = mgr.enqueue(f"user{i}")
            assert job is not None, err
        job, err = mgr.enqueue("user6")
        assert job is None
        assert "full" in err
    finally:
        runner.release.set()
        mgr.stop()


def test_jobs_run_one_at_a_time_fifo(data_dir):
    runner = BlockingRunner()
    mgr = JobManager(data_dir, runner=runner)
    mgr.start()
    try:
        mgr.enqueue("first")
        mgr.enqueue("second")
        mgr.enqueue("third")
        assert wait_until(lambda: runner.started.is_set())
        # While 'first' runs, the others must stay queued.
        time.sleep(0.1)
        assert mgr.get("first").status == "running"
        assert mgr.get("second").status == "queued"
        assert mgr.get("third").status == "queued"
        assert mgr.position("second") == 1
        assert mgr.position("third") == 2
        runner.release.set()
        assert wait_until(
            lambda: all(mgr.get(k).status == "done"
                        for k in ("first", "second", "third")))
        assert runner.max_active == 1
        assert runner.ran == ["first", "second", "third"]
    finally:
        runner.release.set()
        mgr.stop()


def test_duplicate_active_rejected(data_dir):
    runner = BlockingRunner()
    mgr = JobManager(data_dir, runner=runner)
    mgr.start()
    try:
        job, _ = mgr.enqueue("dupuser")
        assert job is not None
        job, err = mgr.enqueue("dupuser")
        assert job is None and "already" in err
        # Case-insensitive: same run dir.
        job, err = mgr.enqueue("DupUser")
        assert job is None and "already" in err
    finally:
        runner.release.set()
        mgr.stop()


def test_rerun_allowed_after_failure(data_dir):
    fail = True

    def runner(job):
        if fail:
            raise PipelineError("user 'x' not found (404)")

    mgr = JobManager(data_dir, runner=runner)
    mgr.start()
    try:
        mgr.enqueue("retryuser")
        assert wait_until(lambda: mgr.get("retryuser").status == "failed")
        assert "404" in mgr.get("retryuser").error
        fail = False
        job, err = mgr.enqueue("retryuser")
        assert job is not None, err
        assert wait_until(lambda: mgr.get("retryuser").status == "done")
    finally:
        mgr.stop()


def test_unexpected_exception_marks_failed(data_dir):
    def runner(job):
        raise ValueError("boom")

    mgr = JobManager(data_dir, runner=runner)
    mgr.start()
    try:
        mgr.enqueue("crashuser")
        assert wait_until(lambda: mgr.get("crashuser").status == "failed")
        assert "internal error" in mgr.get("crashuser").error
    finally:
        mgr.stop()


def test_state_persisted_to_disk(data_dir):
    mgr = JobManager(data_dir, runner=lambda job: None)
    mgr.start()
    try:
        mgr.enqueue("diskuser")
        assert wait_until(lambda: mgr.get("diskuser").status == "done")
        state = json.loads(
            (data_dir / "runs" / "diskuser" / "job.json").read_text())
        assert state["status"] == "done"
        assert state["username"] == "diskuser"
    finally:
        mgr.stop()


def test_restart_recovery_marks_interrupted_jobs_failed(data_dir):
    run_dir = data_dir / "runs" / "stranded"
    run_dir.mkdir(parents=True)
    (run_dir / "job.json").write_text(json.dumps(
        {"username": "stranded", "status": "running", "stage": "verify",
         "error": "", "created_at": 1.0, "started_at": 2.0,
         "finished_at": None}))
    mgr = JobManager(data_dir, runner=lambda job: None)
    mgr.recover()
    job = mgr.get("stranded")
    assert job.status == "failed"
    assert job.error == INTERRUPTED_ERROR


def test_first_boot_runs_ingest_before_first_job(data_dir, monkeypatch):
    (data_dir / "pool.db").unlink()
    ingested = threading.Event()

    def fake_ingest(d):
        (d / "pool.db").write_bytes(b"")
        ingested.set()
        return d / "pool.db"

    monkeypatch.setattr(jobs_mod, "ensure_pool_db", fake_ingest)
    order = []

    def runner(job):
        order.append(("job", ingested.is_set()))

    mgr = JobManager(data_dir, runner=runner)
    assert mgr.pool_state == "missing"
    mgr.start()
    try:
        mgr.enqueue("earlyuser")
        assert wait_until(lambda: mgr.get("earlyuser").status == "done")
        assert mgr.pool_state == "ready"
        assert order == [("job", True)]  # ingest finished before the job ran
    finally:
        mgr.stop()


def test_ingest_failure_reported(data_dir, monkeypatch):
    (data_dir / "pool.db").unlink()

    def fake_ingest(d):
        raise SystemExit("kaggle download failed")

    monkeypatch.setattr(jobs_mod, "ensure_pool_db", fake_ingest)
    mgr = JobManager(data_dir, runner=lambda job: None)
    mgr.start()
    try:
        assert wait_until(lambda: mgr.pool_state.startswith("error"))
        assert "kaggle download failed" in mgr.pool_state
    finally:
        mgr.stop()


def test_list_runs_merges_disk_and_memory(data_dir):
    # A finished CLI run on disk (no job.json) ...
    cli_dir = data_dir / "runs" / "cliuser"
    cli_dir.mkdir(parents=True)
    (cli_dir / "report.html").write_text("<html></html>")
    (cli_dir / "matches_verified.json").write_text(
        json.dumps([{"username": "topmatch1"}]))

    runner = BlockingRunner()
    mgr = JobManager(data_dir, runner=runner)
    mgr.start()
    try:
        mgr.enqueue("webuser")
        assert wait_until(lambda: runner.started.is_set())
        runs = {r["username"]: r for r in mgr.list_runs()}
        assert runs["cliuser"]["status"] == "done"
        assert runs["cliuser"]["has_report"] is True
        assert runs["cliuser"]["top_match"] == "topmatch1"
        assert runs["webuser"]["status"] == "running"
    finally:
        runner.release.set()
        mgr.stop()
