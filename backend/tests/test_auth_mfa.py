import sys
import types

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import Session
from app.models import User, SessionCredential
from app.auth_sessions import _decode, download_with_session, store_credentials, CookieExpired, SessionAuthFailure


@pytest.fixture
def mfa_adapter(client, monkeypatch):
    module = types.ModuleType("test_mfa_adapter")
    calls = {"login": [], "send": []}

    def login(username, password, code=None):
        calls["login"].append((username, password, code))
        if code is None:
            return {"mfakey": "challenge-for-current-login"}
        if code != "012345":
            raise ValueError("private-adapter-error")
        return {"username": "verified-account", "cookies": {"session": "private-cookie"}}

    def send(key, kind):
        calls["send"].append((key, kind))

    module.login, module.send_verify_code = login, send
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(settings, "local_auth_module", module.__name__)
    monkeypatch.setattr(settings, "app_mode", "local")
    return module, calls


def test_mfa_does_not_authenticate_until_code_is_verified(client, mfa_adapter):
    module, calls = mfa_adapter
    credentials = {"username": "entered-account", "password": "private-password"}
    response = client.post("/api/auth/login", json=credentials)
    assert response.status_code == 200
    assert response.json() == {"mfa_required": True, "mfakey": "challenge-for-current-login"}
    with Session() as db:
        assert db.scalar(select(User)) is None
        assert db.scalar(select(SessionCredential)) is None
    assert client.get("/api/auth/me").status_code == 401
    failure = client.post("/api/auth/login", json={**credentials, "code": "wrong"})
    assert failure.status_code == 401 and "private" not in failure.text
    success = client.post("/api/auth/login", json={**credentials, "code": "012345"})
    assert success.status_code == 200
    assert success.json()["user"]["username"] == "verified-account"
    assert "private" not in success.text and "mfakey" not in success.text
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + success.json()["access_token"]}).status_code == 200
    assert calls["login"] == [("entered-account", "private-password", None),
                              ("entered-account", "private-password", "wrong"),
                              ("entered-account", "private-password", "012345")]
    with Session() as db:
        payload = _decode(db.get(SessionCredential, "verified-account").encrypted)
        assert payload == {"login_username": "entered-account", "password": "private-password",
                           "cookies": {"session": "private-cookie"}}


@pytest.mark.parametrize("kind", ["phone", "email"])
def test_sends_code_without_login_token(client, mfa_adapter, kind):
    _, calls = mfa_adapter
    response = client.post("/api/auth/send-verify-code", json={"mfakey": "challenge", "type": kind})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert calls["send"] == [("challenge", kind)]


@pytest.mark.parametrize("data", [{"mfakey": "", "type": "phone"}, {"mfakey": "key", "type": "sms"}])
def test_rejects_invalid_delivery_requests(client, mfa_adapter, data):
    _, calls = mfa_adapter
    assert client.post("/api/auth/send-verify-code", json=data).status_code == 422
    assert calls["send"] == []


def test_delivery_errors_are_not_exposed(client, mfa_adapter):
    module, _ = mfa_adapter
    def fail(*args):
        raise RuntimeError("private-mfakey-and-response")
    module.send_verify_code = fail
    response = client.post("/api/auth/send-verify-code", json={"mfakey": "challenge", "type": "email"})
    assert response.status_code == 400 and "private" not in response.text


def test_old_two_argument_adapter_still_logs_in(client, mfa_adapter):
    module, _ = mfa_adapter
    module.login = lambda username, password: {"username": username, "cookies": {"session": "cookie"}}
    response = client.post("/api/auth/login", json={"username": "account", "password": "password"})
    assert response.status_code == 200 and "access_token" in response.json()


def test_mfa_during_background_refresh_blocks_instead_of_authenticating(client, mfa_adapter):
    module, calls = mfa_adapter
    with Session() as db:
        store_credentials(db, "account", "account", "password", {"session": "expired"})
        db.commit()
    def expired(*args):
        raise CookieExpired()
    module.fetchimage = expired
    with pytest.raises(SessionAuthFailure):
        download_with_session("https://intranet/image", "account")
    assert len(calls["login"]) == 1 and not calls["send"]
