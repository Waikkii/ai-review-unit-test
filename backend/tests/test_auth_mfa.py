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

    def login(username, password, code, verification_type, mask_key, mfa_key):
        calls["login"].append((username, password, code, verification_type, mask_key, mfa_key))
        if code is None:
            return {"result": "verify", "mask_key": "mask-for-current-login", "mfa_key": "challenge-for-current-login"}
        assert (mask_key, mfa_key) == ('mask-for-current-login', 'challenge-for-current-login')
        if code != "012345":
            return {"result": "fail", "reason": "验证码错误，请重试"}
        return {"result": "success", "info": {"username": "verified-account", "cookies": {"session": "private-cookie"}}}

    def send(key, kind):
        calls["send"].append((key, kind))
        return "success"

    module.login, module.send_verify_code = login, send
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(settings, "local_auth_module", module.__name__)
    monkeypatch.setattr(settings, "app_mode", "local")
    return module, calls


@pytest.mark.parametrize('verification_type', ['sms', 'email'])
def test_mfa_does_not_authenticate_until_code_is_verified(client, mfa_adapter, verification_type):
    module, calls = mfa_adapter
    credentials = {"username": "entered-account", "password": "private-password", "verification_type": verification_type}
    response = client.post("/api/auth/login", json=credentials)
    assert response.status_code == 200
    assert response.json() == {"mfa_required": True, "mfakey": "challenge-for-current-login",
                               "mask_key": "mask-for-current-login", "mfa_key": "challenge-for-current-login"}
    with Session() as db:
        assert db.scalar(select(User)) is None
        assert db.scalar(select(SessionCredential)) is None
    assert client.get("/api/auth/me").status_code == 401
    credentials.update(mask_key=response.json()['mask_key'], mfa_key=response.json()['mfa_key'])
    failure = client.post("/api/auth/login", json={**credentials, "code": "wrong"})
    assert failure.status_code == 401 and "private" not in failure.text
    assert failure.json() == {"detail": "验证码错误，请重试"}
    success = client.post("/api/auth/login", json={**credentials, "code": "012345"})
    assert success.status_code == 200
    assert success.json()["user"]["username"] == "verified-account"
    assert "private" not in success.text and "mfakey" not in success.text
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + success.json()["access_token"]}).status_code == 200
    assert calls["login"] == [("entered-account", "private-password", None, verification_type, None, None),
                              ("entered-account", "private-password", "wrong", verification_type, 'mask-for-current-login', 'challenge-for-current-login'),
                              ("entered-account", "private-password", "012345", verification_type, 'mask-for-current-login', 'challenge-for-current-login')]
    with Session() as db:
        payload = _decode(db.get(SessionCredential, "verified-account").encrypted)
        assert payload == {"login_username": "entered-account", "password": "private-password",
                           "cookies": {"session": "private-cookie"}, "verification_type": verification_type}


@pytest.mark.parametrize("kind", ["sms", "email"])
def test_sends_code_without_login_token(client, mfa_adapter, kind):
    _, calls = mfa_adapter
    response = client.post("/api/auth/send-verify-code", json={"mfakey": "challenge", "type": kind})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert calls["send"] == [("challenge", kind)]


@pytest.mark.parametrize("data", [{"mfakey": "", "type": "sms"}, {"mfakey": "key", "type": "phone"}])
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
    assert response.json() == {"detail": "发送失败，请联系管理员解决"}


@pytest.mark.parametrize('result', ['fail', None, False, 'unexpected'])
def test_delivery_failure_results_do_not_report_success(client, mfa_adapter, result):
    module, _ = mfa_adapter
    module.send_verify_code = lambda *args: result
    response = client.post('/api/auth/send-verify-code', json={"mfakey": "challenge", "type": "sms"})
    assert response.status_code == 400
    assert response.json() == {"detail": "发送失败，请联系管理员解决"}


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
    {"result": "verify", "mask_key": "mask"},
    {"result": "verify", "mask_key": "mask", "mfa_key": ""},
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
    assert calls['login'][0][2:] == (None, 'sms', None, None)


def test_invalid_login_verification_type_is_rejected(client, mfa_adapter):
    _, calls = mfa_adapter
    response = client.post('/api/auth/login', json={"username": "account", "password": "password",
                                                  "verification_type": "phone"})
    assert response.status_code == 422 and not calls['login']


def test_refresh_uses_saved_email_channel(client, mfa_adapter):
    module, calls = mfa_adapter
    with Session() as db:
        store_credentials(db, 'account', 'account', 'password', {"session": "old"}, 'email')
        db.commit()
    def login(username, password, code, verification_type):
        calls['login'].append((username, password, code, verification_type))
        return {"result": "success", "info": {"username": username, "cookies": {"session": "new"}}}
    def fetch(url, cookies):
        if cookies['session'] == 'old':
            raise CookieExpired()
        return []
    module.login, module.fetchimage = login, fetch
    assert download_with_session('https://intranet/image', 'account') == []
    assert calls['login'] == [('account', 'password', None, 'email')]
