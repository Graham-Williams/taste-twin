"""App-level shared-password gate (env-gated by ``APP_PASSWORD``).

When ``APP_PASSWORD`` is set the whole app sits behind ONE shared password:
any request that isn't the login/logout route, a static asset, or the
healthcheck is redirected to ``/login`` until the visitor presents the
password. A correct password (compared in constant time) stores only a
*marker* in Flask's signed session cookie — signed with ``SESSION_SECRET``
via itsdangerous, so the raw password is never stored in the cookie or logged.
Unset / empty ``APP_PASSWORD`` = gate OFF: the app behaves exactly as before,
which lets the gate ship dormant behind Cloudflare Access and be switched on
at cutover.

This module holds the stateless helpers and the in-memory per-IP failed-login
rate limiter; the routes + ``before_request`` gate live in :mod:`.app`.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

from flask import request, url_for


def client_ip() -> str:
    """Best-effort client IP. Behind the Cloudflare tunnel the real client is
    in ``CF-Connecting-IP``; ``remote_addr`` is the tunnel/proxy hop. Fall back
    to ``remote_addr`` for local/dev requests."""
    return (request.headers.get("CF-Connecting-IP")
            or request.remote_addr or "").strip()


def safe_next(target: str | None, fallback_endpoint: str = "index") -> str:
    """Return ``target`` only if it is a safe *local* path, else the fallback.

    Open-redirect defense: a valid ``next`` must be a single-slash, same-site
    path with no scheme and no network location. This rejects absolute URLs
    (``https://evil``), protocol-relative URLs (``//evil``) and the
    backslash trick (``/\\evil`` which some browsers treat as ``//evil``).
    """
    fallback = url_for(fallback_endpoint)
    if not target:
        return fallback
    if (not target.startswith("/")
            or target.startswith("//")
            or target[:2] == "/\\"):
        return fallback
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return fallback
    return target


class LoginRateLimiter:
    """In-memory sliding-window limiter for failed logins, keyed by client IP.

    After ``max_failures`` failures within ``window_seconds`` an IP is
    blocked; as failures age out of the window the IP is unblocked (so a burst
    of 10 bad guesses locks that IP for ~15 minutes). Process-local — fine for
    the single-gunicorn-worker deployment; a restart clears it.
    """

    def __init__(self, max_failures: int = 10, window_seconds: int = 900,
                 max_tracked_ips: int = 10000):
        self.max_failures = max_failures
        self.window = window_seconds
        self.max_tracked_ips = max_tracked_ips
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, ip: str, now: float) -> list[float]:
        fails = [t for t in self._fails.get(ip, ()) if now - t < self.window]
        if fails:
            self._fails[ip] = fails
        else:
            self._fails.pop(ip, None)
        return fails

    def is_blocked(self, ip: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            return len(self._prune(ip, now)) >= self.max_failures

    def _sweep(self, now: float) -> None:
        """Drop every IP whose failures have all aged out. Bounds memory even
        if IPs are never re-queried (e.g. spoofed CF-Connecting-IP off-tunnel)."""
        for ip in [k for k, ts in self._fails.items()
                   if all(now - t >= self.window for t in ts)]:
            self._fails.pop(ip, None)

    def record_failure(self, ip: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            # Opportunistic full sweep when the table grows large, so a flood
            # of distinct IPs can't grow _fails without bound.
            if len(self._fails) >= self.max_tracked_ips:
                self._sweep(now)
            self._prune(ip, now)
            self._fails.setdefault(ip, []).append(now)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)
