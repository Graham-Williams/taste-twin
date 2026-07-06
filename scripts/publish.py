#!/usr/bin/env python3
"""Generate a taste-twin report locally (on a residential IP) and publish it
to the view-only box instance.

Why this exists
---------------
Letterboxd sits behind Cloudflare bot management that serves a JS challenge to
the box's datacenter/server IP, so live scraping from the box fails. A
residential IP (Graham's Mac) is not challenged. So: the box runs a *view-only*
report gallery (``TASTE_TWIN_VIEWER_MODE=1``), and new reports are generated
here on the Mac and synced over to the box.

Usage
-----
    python scripts/publish.py <letterboxd_username>
        [--box graham@100.101.1.28] [--container taste-twin]
        [--url-base https://taste-twin.graham-williams.com]

Steps
-----
1. Validate ``<username>`` with the SAME rule the app uses
   (``tastetwin.scraper.is_valid_name`` — imported, never reimplemented) plus a
   64-char bound, BEFORE it is used anywhere. The value flows into subprocess /
   ssh / docker arg lists, so it must be charset-clean; every command is built
   as an ARG LIST (no shell string, no interpolation into a shell) and no
   subprocess call ever enables shell interpretation.
2. Run the pipeline locally: ``<venv-python> -m tastetwin run <username>``.
3. Copy ONLY ``report.html`` + ``matches_verified.json`` (never the large
   ``matches_dataset.json``) to a box temp dir, ``docker cp`` them into the
   container's run dir, drop any stale ``job.json`` / ``job.log`` there (so the
   UI synthesizes a "done" run), and clean up the temp dir.
4. Print the public report URL.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Make the repo importable when run as `python scripts/publish.py`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tastetwin.scraper import is_valid_name  # noqa: E402
from tastetwin.util import safe_filename      # noqa: E402

MAX_USERNAME_LEN = 64  # mirrors tastetwin.web.jobs.MAX_USERNAME_LEN
DEFAULT_BOX = "graham@100.101.1.28"
DEFAULT_CONTAINER = "taste-twin"
DEFAULT_URL_BASE = "https://taste-twin.graham-williams.com"

# Only these two files are ever shipped to the box. The big per-run artifact
# (matches_dataset.json) is deliberately excluded.
PUBLISH_FILES = ("report.html", "matches_verified.json")


class PublishError(Exception):
    """A user-visible failure; the CLI prints it and exits nonzero."""


def _run(cmd: list[str], *, what: str, stream: bool = False) -> None:
    """Run a subprocess from an ARG LIST (never a shell string).

    `stream=True` lets the child inherit stdout/stderr (used for the long
    pipeline run). Otherwise output is captured and surfaced on failure.
    """
    try:
        if stream:
            proc = subprocess.run(cmd, check=False)  # noqa: S603 - arg list, no shell
            if proc.returncode != 0:
                raise PublishError(f"{what} failed (exit {proc.returncode}).")
        else:
            proc = subprocess.run(cmd, check=False, capture_output=True,  # noqa: S603
                                  text=True)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                raise PublishError(
                    f"{what} failed (exit {proc.returncode}): {detail}")
    except FileNotFoundError as exc:
        raise PublishError(f"{what} failed — command not found: {exc}") from exc


def validate_username(username: str) -> str:
    """Charset- and length-validate BEFORE the name touches any command."""
    username = (username or "").strip()
    if not username:
        raise PublishError("Username is empty.")
    if len(username) > MAX_USERNAME_LEN:
        raise PublishError(
            f"Username too long (max {MAX_USERNAME_LEN} characters).")
    if not is_valid_name(username):
        raise PublishError(
            "Invalid username — only letters, digits, '_' and '-' are allowed.")
    return username


def generate_report(username: str, data_dir: Path) -> Path:
    """Run the local pipeline; return the run dir. Raises on failure."""
    print(f"==> Generating report for {username!r} on this machine...")
    _run([sys.executable, "-m", "tastetwin", "run", username],
         what="local pipeline (python -m tastetwin run)", stream=True)
    run_dir = data_dir / "runs" / safe_filename(username.lower())
    report = run_dir / "report.html"
    if not report.is_file():
        raise PublishError(
            f"Pipeline finished but {report} is missing — nothing to publish.")
    return run_dir


def publish_to_box(run_dir: Path, key: str, box: str, container: str) -> None:
    """scp the two files to the box, docker cp into the container, tidy up."""
    remote_tmp = f"/tmp/taste-twin-publish-{key}"
    container_dir = f"/app/data/runs/{key}"

    files = []
    for name in PUBLISH_FILES:
        p = run_dir / name
        if not p.is_file():
            if name == "report.html":
                raise PublishError(f"missing {name} in {run_dir}")
            print(f"    (note: {name} absent locally — skipping)")
            continue
        files.append(p)

    print(f"==> Publishing to {box} container {container!r}...")
    # 1) fresh temp dir on the box
    _run(["ssh", box, "mkdir", "-p", remote_tmp],
         what="ssh mkdir remote temp dir")
    try:
        # 2) scp the files up
        _run(["scp", *[str(p) for p in files], f"{box}:{remote_tmp}/"],
             what="scp report files to box")
        # 3) ensure the container run dir exists (runs as the container user)
        _run(["ssh", box, "docker", "exec", container,
              "mkdir", "-p", container_dir],
             what="docker exec mkdir container run dir")
        # 4) docker cp each file into the container run dir
        for p in files:
            _run(["ssh", box, "docker", "cp",
                  f"{remote_tmp}/{p.name}", f"{container}:{container_dir}/{p.name}"],
                 what=f"docker cp {p.name}")
        # 5) drop any stale job state so the UI synthesizes a "done" run
        _run(["ssh", box, "docker", "exec", container,
              "rm", "-f", f"{container_dir}/job.json", f"{container_dir}/job.log"],
             what="docker exec rm stale job state")
    finally:
        # 6) always clean up the box temp dir
        _run(["ssh", box, "rm", "-rf", remote_tmp],
             what="ssh cleanup remote temp dir")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a taste-twin report locally and publish it to "
                    "the view-only box instance.")
    parser.add_argument("username", help="Letterboxd username to report on")
    parser.add_argument("--box", default=DEFAULT_BOX,
                        help=f"ssh target of the box (default: {DEFAULT_BOX})")
    parser.add_argument("--container", default=DEFAULT_CONTAINER,
                        help="Docker container name on the box "
                             f"(default: {DEFAULT_CONTAINER})")
    parser.add_argument("--url-base", default=DEFAULT_URL_BASE,
                        help="public base URL for the success message "
                             f"(default: {DEFAULT_URL_BASE})")
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "data"),
                        help="local data dir holding runs/ (default: ./data)")
    args = parser.parse_args(argv)

    try:
        username = validate_username(args.username)
        data_dir = Path(args.data_dir).resolve()
        key = safe_filename(username.lower())
        run_dir = generate_report(username, data_dir)
        publish_to_box(run_dir, key, args.box, args.container)
    except PublishError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    url = f"{args.url_base.rstrip('/')}/report/{username}"
    print(f"==> Published. View it at: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
