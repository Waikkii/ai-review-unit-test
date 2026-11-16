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
            return {"result": "verify", "mfakey": "challenge-for-current-login"}
        if code != "012345":
            return {"result": "fail", "reason": "验证码错误，请重试"}
        return {"result": "success", "info": {"username": "verified-account", "cookies": {"session": "private-cookie"}}}

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
    assert failure.json() == {"detail": "验证码错误，请重试"}
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


@pytest.mark.parametrize('wrapped', [False, True])
def test_two_argument_adapter_still_logs_in(client, mfa_adapter, wrapped):
    module, _ = mfa_adapter
    def login(username, password):
        identity = {"username": username, "cookies": {"session": "cookie"}}
        return {"result": "success", "info": identity} if wrapped else identity
    module.login = login
    response = client.post("/api/auth/login", json={"username": "account", "password": "password"})
    assert response.status_code == 200 and "access_token" in response.json()


@pytest.mark.parametrize('reason', ['用户名密码错误', '验证码错误，请重试', '认证暂不可用，请稍后重试'])
def test_explicit_login_failure_displays_only_reason_and_creates_no_session(client, mfa_adapter, reason):
    module, _ = mfa_adapter
    module.login = lambda *args: {"result": "fail", "reason": reason, "mfakey": "must-not-trigger-mfa",
                                 "info": {"username": "account", "cookies": {"secret": "private-cookie"}}}
    response = client.post('/api/auth/login', json={"username": "account", "password": "password"})
    assert response.status_code == 401 and response.json() == {"detail": reason}
    with Session() as db:
        assert db.scalar(select(User)) is None
        assert db.scalar(select(SessionCredential)) is None


@pytest.mark.parametrize('result', [
    {"result": "success", "info": {}}, {"result": "success", "info": None},
    {"result": "fail", "reason": {"secret": "private-error"}},
    {"result": "fail", "reason": ""}, {"result": "unknown", "reason": "private-error"},
    {"result": "verify"}, {"result": "verify", "mfakey": ""},
    {"result": "verify", "mfakey": 123},
])
def test_invalid_adapter_results_use_generic_error(client, mfa_adapter, result):
    module, _ = mfa_adapter
    module.login = lambda *args: result
    response = client.post('/api/auth/login', json={"username": "account", "password": "password"})
    assert response.status_code == 401
    assert response.json() == {"detail": "用户名或密码错误，或内网认证服务不可用"}


def test_login_exception_is_not_exposed(client, mfa_adapter):
    module, _ = mfa_adapter
    def fail(*args):
        raise RuntimeError('private-exception')
    module.login = fail
    response = client.post('/api/auth/login', json={"username": "account", "password": "password"})
    assert response.status_code == 401 and 'private' not in response.text


@pytest.mark.parametrize('failure', [False, True])
def test_mfa_during_background_refresh_blocks_instead_of_authenticating(client, mfa_adapter, failure):
    module, calls = mfa_adapter
    if failure:
        original = module.login
        def rejected(*args):
            original(*args)
            return {"result": "fail", "reason": "private-background-failure"}
        module.login = rejected
    with Session() as db:
        store_credentials(db, "account", "account", "password", {"session": "expired"})
        db.commit()
    def expired(*args):
        raise CookieExpired()
    module.fetchimage = expired
    with pytest.raises(SessionAuthFailure) as exc:
        download_with_session("https://intranet/image", "account")
    assert 'private' not in str(exc.value)
    assert len(calls["login"]) == 1 and not calls["send"]
