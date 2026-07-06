"""Cloudflare Access JWT middleware: valid/invalid/missing tokens against a
fake JWKS, dev-mode bypass only when env is unset, JWKS caching."""

import json
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tastetwin.web import auth as auth_mod
from tastetwin.web.app import create_app

AUD = "test-aud-1234"
TEAM = "unittest.cloudflareaccess.com"
ISS = f"https://{TEAM}"
KID = "test-key-1"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks_for(key, kid=KID) -> dict:
    jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = kid
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return {"keys": [jwk]}


def _token(key, kid=KID, aud=AUD, iss=ISS, exp_delta=600, **extra) -> str:
    now = int(time.time())
    claims = {"aud": aud, "iss": iss, "iat": now, "exp": now + exp_delta,
              "email": "graham@example.com", **extra}
    return pyjwt.encode(claims, key, algorithm="RS256",
                        headers={"kid": kid})


@pytest.fixture
def app(tmp_path, monkeypatch, rsa_key):
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.delenv("APP_HOST", raising=False)
    monkeypatch.setattr(auth_mod, "_fetch_jwks",
                        lambda team: _jwks_for(rsa_key))
    return create_app(data_dir=tmp_path / "data", start_worker=False)


def test_valid_jwt_accepted(app, rsa_key):
    resp = app.test_client().get(
        "/", headers={"Cf-Access-Jwt-Assertion": _token(rsa_key)})
    assert resp.status_code == 200


def test_valid_jwt_in_cookie_accepted(app, rsa_key):
    client = app.test_client()
    client.set_cookie("CF_Authorization", _token(rsa_key))
    assert client.get("/").status_code == 200


def test_missing_jwt_rejected(app):
    assert app.test_client().get("/").status_code == 403


def test_garbage_jwt_rejected(app):
    resp = app.test_client().get(
        "/", headers={"Cf-Access-Jwt-Assertion": "not.a.jwt"})
    assert resp.status_code == 403


def test_expired_jwt_rejected(app, rsa_key):
    resp = app.test_client().get(
        "/", headers={"Cf-Access-Jwt-Assertion":
                      _token(rsa_key, exp_delta=-60)})
    assert resp.status_code == 403


def test_wrong_audience_rejected(app, rsa_key):
    resp = app.test_client().get(
        "/", headers={"Cf-Access-Jwt-Assertion":
                      _token(rsa_key, aud="someone-else")})
    assert resp.status_code == 403


def test_wrong_issuer_rejected(app, rsa_key):
    resp = app.test_client().get(
        "/", headers={"Cf-Access-Jwt-Assertion":
                      _token(rsa_key, iss="https://evil.example.com")})
    assert resp.status_code == 403


def test_wrong_signing_key_rejected(app, other_rsa_key):
    # Signed by a key NOT in the JWKS (but claiming the same kid).
    resp = app.test_client().get(
        "/", headers={"Cf-Access-Jwt-Assertion": _token(other_rsa_key)})
    assert resp.status_code == 403


def test_unknown_kid_rejected(app, rsa_key):
    resp = app.test_client().get(
        "/", headers={"Cf-Access-Jwt-Assertion":
                      _token(rsa_key, kid="unknown-kid")})
    assert resp.status_code == 403


def test_missing_exp_rejected(app, rsa_key):
    now = int(time.time())
    token = pyjwt.encode({"aud": AUD, "iss": ISS, "iat": now}, rsa_key,
                         algorithm="RS256", headers={"kid": KID})
    resp = app.test_client().get(
        "/", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status_code == 403


def test_healthz_exempt_from_auth(app):
    assert app.test_client().get("/healthz").status_code == 200


def test_all_routes_protected(app):
    client = app.test_client()
    assert client.get("/about").status_code == 403
    assert client.get("/run/someone").status_code == 403
    assert client.get("/report/someone").status_code == 403
    assert client.post("/run", data={"username": "someone"}).status_code == 403


def test_jwks_fetch_failure_fails_closed(tmp_path, monkeypatch, rsa_key):
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.delenv("APP_HOST", raising=False)

    def boom(team):
        raise OSError("network down")

    monkeypatch.setattr(auth_mod, "_fetch_jwks", boom)
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    resp = app.test_client().get(
        "/", headers={"Cf-Access-Jwt-Assertion": _token(rsa_key)})
    assert resp.status_code == 403


def test_jwks_cached_between_requests(tmp_path, monkeypatch, rsa_key):
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.delenv("APP_HOST", raising=False)
    calls = []

    def counting_fetch(team):
        calls.append(team)
        return _jwks_for(rsa_key)

    monkeypatch.setattr(auth_mod, "_fetch_jwks", counting_fetch)
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    client = app.test_client()
    for _ in range(3):
        assert client.get(
            "/", headers={"Cf-Access-Jwt-Assertion":
                          _token(rsa_key)}).status_code == 200
    assert len(calls) == 1


def test_dev_mode_only_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("APP_HOST", raising=False)
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    assert app.test_client().get("/").status_code == 200


def test_partial_env_still_dev_mode_never_half_auth(tmp_path, monkeypatch):
    # Only one of the two vars set: auth can't be configured — the app must
    # behave exactly like dev mode rather than half-verify.
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("APP_HOST", raising=False)
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    assert app.test_client().get("/").status_code == 200
