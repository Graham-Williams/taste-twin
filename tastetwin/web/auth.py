"""Cloudflare Access JWT verification (same posture as todoist-points).

The app has no auth of its own; when fronted by Cloudflare Access every
request carries a ``Cf-Access-Jwt-Assertion`` header (also mirrored in the
``CF_Authorization`` cookie) signed by the team's keys. When the
``CF_ACCESS_AUD`` and ``CF_ACCESS_TEAM_DOMAIN`` env vars are set we verify
that JWT — signature against the team JWKS, plus ``aud``, ``exp`` and
``iss`` — and reject the request otherwise, so a client that can reach the
container directly (e.g. a compromised sibling on the shared Docker
network) still can't use it. Absent env vars = local dev mode (warned).

JWKS is fetched from ``https://<team>/cdn-cgi/access/certs`` and cached
with a TTL; on refresh failure a stale copy is used if present, otherwise
we fail CLOSED (403).
"""

from __future__ import annotations

import logging
import threading
import time

import jwt as pyjwt
import requests

log = logging.getLogger("tastetwin.web")

JWKS_TTL_SECONDS = 3600.0
JWKS_TIMEOUT_SECONDS = 10.0


class AccessAuthError(Exception):
    """The request could not be authenticated."""


def _fetch_jwks(team_domain: str) -> dict:
    """GET the team's JWKS. Split out so tests can monkeypatch it."""
    resp = requests.get(f"https://{team_domain}/cdn-cgi/access/certs",
                        timeout=JWKS_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


class AccessVerifier:
    """Verifies Cloudflare Access JWTs against the team's cached JWKS."""

    def __init__(self, aud: str, team_domain: str,
                 jwks_ttl: float = JWKS_TTL_SECONDS):
        self.aud = aud
        self.team_domain = team_domain
        self.issuer = f"https://{team_domain}"
        self.jwks_ttl = jwks_ttl
        self._jwks: dict | None = None
        self._jwks_fetched_at = 0.0
        self._lock = threading.Lock()

    # -- JWKS cache ---------------------------------------------------------

    def _jwks_keys(self) -> list[dict]:
        with self._lock:
            fresh = (self._jwks is not None and
                     time.time() - self._jwks_fetched_at < self.jwks_ttl)
            if not fresh:
                try:
                    self._jwks = _fetch_jwks(self.team_domain)
                    self._jwks_fetched_at = time.time()
                except Exception as exc:  # noqa: BLE001 - fail closed below
                    if self._jwks is None:
                        raise AccessAuthError(
                            f"could not fetch Access JWKS: {exc}") from exc
                    log.warning("Access JWKS refresh failed (%s) — using "
                                "stale copy", exc)
            return list(self._jwks.get("keys", []))

    # -- verification ---------------------------------------------------------

    def verify(self, token: str) -> dict:
        """Return the validated claims, or raise AccessAuthError."""
        if not token:
            raise AccessAuthError("missing Access JWT")
        try:
            header = pyjwt.get_unverified_header(token)
        except pyjwt.InvalidTokenError as exc:
            raise AccessAuthError(f"malformed token: {exc}") from exc
        kid = header.get("kid")
        key_dict = next(
            (k for k in self._jwks_keys() if k.get("kid") == kid), None)
        if key_dict is None:
            raise AccessAuthError("token signed by unknown key")
        try:
            key = pyjwt.PyJWK(key_dict)
            return pyjwt.decode(
                token, key=key.key, algorithms=["RS256"],
                audience=self.aud, issuer=self.issuer,
                options={"require": ["exp", "iat"]})
        except pyjwt.InvalidTokenError as exc:
            raise AccessAuthError(f"invalid Access JWT: {exc}") from exc
