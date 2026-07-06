"""Background job queue for the web app.

Design constraints (do not weaken):

- ONE worker thread, jobs strictly FIFO and one at a time — the polite
  scraping budget (>= 1 s between requests, single-threaded) is global to
  the process, so two concurrent scrape jobs would double our request rate.
- A small pending cap (:data:`MAX_PENDING`) so the queue can't grow
  unboundedly.
- Job state is persisted to ``data/runs/<user>/job.json`` and the run's
  log to ``job.log`` so a container restart doesn't strand the UI: on
  boot, jobs found queued/running on disk are marked failed with a
  re-run hint (the HTTP cache makes re-runs cheap).
- First boot: if ``data/pool.db`` is missing the worker ingests the
  Kaggle dataset before taking the first job.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import pipeline
from ..ingest import ensure_pool_db
from ..scraper import PoliteSession, is_valid_name
from ..util import safe_filename

log = logging.getLogger("tastetwin.web")

MAX_PENDING = 5
MAX_USERNAME_LEN = 64  # Letterboxd's own cap is 15; this is just a bound

INTERRUPTED_ERROR = ("interrupted by an app restart — hit Re-run "
                     "(cached pages make re-runs cheap)")

# Job parameters (web runs use the CLI defaults).
VERIFY_TOP = 50
MAX_PAGES = 10
MIN_OVERLAP = 15

_PERSISTED_FIELDS = ("username", "status", "stage", "error",
                     "created_at", "started_at", "finished_at")


@dataclass
class Job:
    username: str
    key: str
    status: str = "queued"  # queued | running | done | failed
    stage: str = ""         # fetch | analyze | verify | report
    detail: str = ""        # last pipeline log line (in-memory only)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None


class _JobLogHandler(logging.Handler):
    """Tees pipeline log records into the job's log file + `detail`."""

    def __init__(self, job: Job, log_path: Path):
        super().__init__(level=logging.INFO)
        self.job = job
        self._fh = open(log_path, "w", encoding="utf-8")
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                              datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._fh.write(self.format(record) + "\n")
            self._fh.flush()
            self.job.detail = record.getMessage().strip()[:300]
        except Exception:  # noqa: BLE001 - logging must never kill the job
            pass

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            super().close()


class JobManager:
    """FIFO queue + single worker thread around the pipeline."""

    def __init__(self, data_dir: Path, runner=None,
                 max_pending: int = MAX_PENDING):
        self.data_dir = Path(data_dir)
        self.max_pending = max_pending
        self._runner = runner or self._run_pipeline
        self._jobs: dict[str, Job] = {}
        self._pending: deque[str] = deque()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.pool_state = "ready" if self.pool_db_path.exists() else "missing"

    @property
    def pool_db_path(self) -> Path:
        return self.data_dir / "pool.db"

    # -- lifecycle ------------------------------------------------------------

    def recover(self) -> None:
        """Mark jobs left queued/running by a previous process as failed."""
        runs = self.data_dir / "runs"
        if not runs.is_dir():
            return
        for state_path in runs.glob("*/job.json"):
            try:
                state = json.loads(state_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if state.get("status") in ("queued", "running"):
                state["status"] = "failed"
                state["error"] = INTERRUPTED_ERROR
                state["finished_at"] = time.time()
                state_path.write_text(json.dumps(state))
                log.warning("Recovered interrupted job for %r -> failed",
                            state.get("username"))

    def start(self) -> None:
        if self._thread is not None:
            return
        logging.getLogger("tastetwin").setLevel(logging.INFO)
        self._thread = threading.Thread(
            target=self._worker, name="tastetwin-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    # -- API ------------------------------------------------------------------

    def enqueue(self, username: str) -> tuple[Job | None, str]:
        """Validate + enqueue. Returns (job, "") or (None, error)."""
        username = (username or "").strip()
        if not username or len(username) > MAX_USERNAME_LEN \
                or not is_valid_name(username):
            return None, ("That doesn't look like a Letterboxd username "
                          "(letters, digits, _ and - only).")
        key = safe_filename(username.lower())
        with self._cond:
            active = self._jobs.get(key)
            if active and active.status in ("queued", "running"):
                return None, (f"A run for {username!r} is already "
                              f"{active.status}.")
            if len(self._pending) >= self.max_pending:
                return None, (f"Queue is full ({self.max_pending} pending) — "
                              f"try again once a run finishes.")
            job = Job(username=username, key=key)
            self._jobs[key] = job
            self._pending.append(key)
            self._cond.notify_all()
        self._persist(job)
        return job, ""

    def get(self, key: str) -> Job | None:
        with self._cond:
            job = self._jobs.get(key)
        if job is not None:
            return job
        return self._load_from_disk(key)

    def position(self, key: str) -> int | None:
        """1-based queue position for a queued job, else None."""
        with self._cond:
            try:
                return list(self._pending).index(key) + 1
            except ValueError:
                return None

    def log_tail(self, key: str, lines: int = 15) -> list[str]:
        path = self.data_dir / "runs" / safe_filename(key) / "job.log"
        try:
            return path.read_text(encoding="utf-8",
                                  errors="replace").splitlines()[-lines:]
        except OSError:
            return []

    def list_runs(self) -> list[dict]:
        """All known runs (in-memory jobs + finished runs on disk)."""
        entries: dict[str, dict] = {}
        runs = self.data_dir / "runs"
        if runs.is_dir():
            for d in sorted(runs.iterdir()):
                if not d.is_dir() or not is_valid_name(d.name):
                    continue
                job = self._load_from_disk(d.name)
                report = d / "report.html"
                if job is None:
                    if not report.exists():
                        continue  # partial CLI run, nothing to show
                    job = Job(username=d.name, key=d.name, status="done",
                              created_at=report.stat().st_mtime,
                              finished_at=report.stat().st_mtime)
                entries[d.name] = self._entry(job, report.exists())
        with self._cond:
            live = list(self._jobs.values())
        for job in live:
            report = runs / job.key / "report.html"
            entries[job.key] = self._entry(job, report.exists())
        order = {"running": 0, "queued": 1, "failed": 2, "done": 3}
        return sorted(entries.values(),
                      key=lambda e: (order.get(e["status"], 4),
                                     -(e["finished_at"] or e["created_at"])))

    # -- internals --------------------------------------------------------------

    def _entry(self, job: Job, has_report: bool) -> dict:
        top_match = self._top_match(job.key) if has_report else None
        return {**asdict(job), "has_report": has_report,
                "top_match": top_match,
                "position": self.position(job.key)}

    def _top_match(self, key: str) -> str | None:
        path = self.data_dir / "runs" / key / "matches_verified.json"
        try:
            matches = json.loads(path.read_text())
            return matches[0]["username"] if matches else None
        except (OSError, json.JSONDecodeError, LookupError, TypeError):
            return None

    def _run_dir(self, job: Job) -> Path:
        return pipeline.run_dir_for(self.data_dir, job.username)

    def _persist(self, job: Job) -> None:
        state = {k: getattr(job, k) for k in _PERSISTED_FIELDS}
        try:
            (self._run_dir(job) / "job.json").write_text(json.dumps(state))
        except OSError as exc:
            log.warning("could not persist job state for %r: %s",
                        job.username, exc)

    def _load_from_disk(self, key: str) -> Job | None:
        if not is_valid_name(key):
            return None
        path = self.data_dir / "runs" / safe_filename(key) / "job.json"
        try:
            state = json.loads(path.read_text())
            return Job(key=key,
                       **{k: state[k] for k in _PERSISTED_FIELDS
                          if k in state})
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def _ensure_pool(self) -> None:
        if self.pool_db_path.exists():
            self.pool_state = "ready"
            return
        self.pool_state = "building"
        log.info("pool.db missing — running one-time dataset ingest")
        try:
            ensure_pool_db(self.data_dir)
            self.pool_state = "ready"
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            self.pool_state = f"error: {exc}"
            log.error("dataset ingest failed: %s", exc)

    def _worker(self) -> None:
        self._ensure_pool()
        while not self._stop.is_set():
            with self._cond:
                while not self._pending and not self._stop.is_set():
                    self._cond.wait(timeout=1.0)
                if self._stop.is_set():
                    return
                key = self._pending.popleft()
                job = self._jobs[key]
                job.status = "running"
                job.started_at = time.time()
            self._persist(job)
            try:
                self._runner(job)
                job.status = "done"
                job.stage = "report"
            except pipeline.PipelineError as exc:
                job.status = "failed"
                job.error = str(exc)
            except (Exception, SystemExit) as exc:  # noqa: BLE001
                job.status = "failed"
                job.error = f"internal error: {exc}"
                log.exception("job for %r crashed", job.username)
            job.finished_at = time.time()
            self._persist(job)

    def _run_pipeline(self, job: Job) -> None:
        """Default runner: fetch -> analyze -> verify -> report."""
        run_dir = self._run_dir(job)
        handler = _JobLogHandler(job, run_dir / "job.log")
        pipeline_log = logging.getLogger("tastetwin")
        pipeline_log.addHandler(handler)
        try:
            session = PoliteSession(cache_dir=self.data_dir / "cache")
            job.stage = "fetch"
            self._persist(job)
            pipeline.stage_fetch(session, run_dir, job.username)
            job.stage = "analyze"
            self._persist(job)
            pipeline.stage_analyze(self.data_dir, run_dir, MIN_OVERLAP)
            job.stage = "verify"
            self._persist(job)
            pipeline.stage_verify(session, run_dir, job.username,
                                  VERIFY_TOP, MAX_PAGES, MIN_OVERLAP)
        finally:
            pipeline_log.removeHandler(handler)
            handler.close()
