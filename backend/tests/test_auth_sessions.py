import sys
import base64
import types
import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from sqlalchemy import select
from app.config import settings
from app.db import Session
from app.models import SessionCredential, Keyword
from app.auth_sessions import store_credentials, download_with_session, CookieExpired, SessionAuthFailure
from app.worker import process_once
from test_workflow import upload


def adapter(monkeypatch, *, fail_retry=False, initial_valid=False, fail_login=False):
    module = types.ModuleType("fake_auth_sessions")
    calls = {"login": [], "fetch": []}

    def login(username, password):
        calls["login"].append((username, password))
        if fail_login:
            raise RuntimeError("do-not-expose-password")
        return {"username": "demo", "cookies": {"session": "fresh-cookie-secret"}}

    def fetch(url, cookies):
        calls["fetch"].append(cookies)
        if not initial_valid and cookies["session"] == "old-cookie-secret":
            raise CookieExpired()
        if fail_retry:
            raise RuntimeError("do-not-expose-cookie")
        return [(base64.b64encode(b"\xff\xd8\xfftest-jpeg").decode("ascii"), "image/jpeg")]

    module.login, module.fetchimage = login, fetch
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(settings, "local_auth_module", module.__name__)
    with Session() as db:
        store_credentials(db, "demo", "original-login-name", "password-secret-123", {"session": "old-cookie-secret"})
        db.commit()
    return calls


@pytest.mark.parametrize("initial_valid", [True, False])
def test_cookies_reused_or_refreshed_once_and_only_encrypted_at_rest(client, monkeypatch, initial_valid):
    calls = adapter(monkeypatch, initial_valid=initial_valid)
    result = download_with_session("http://intranet.example/a.jpg", "demo")
    assert result[0][1] == "image/jpeg"
    assert len(calls["fetch"]) == (1 if initial_valid else 2)
    assert calls["login"] == ([] if initial_valid else [("original-login-name", "password-secret-123")])
    with Session() as db:
        encrypted = db.get(SessionCredential, "demo").encrypted
        assert all(secret not in encrypted for secret in ["password-secret-123", "old-cookie-secret", "fresh-cookie-secret"])
    if not initial_valid:
        download_with_session("http://intranet.example/b.jpg", "demo")
        assert len(calls["login"]) == 1


@pytest.mark.parametrize("fail_login,fail_retry", [(True, False), (False, True)])
def test_refresh_failure_is_bounded_and_never_exposes_credentials(client, monkeypatch, fail_login, fail_retry):
    calls = adapter(monkeypatch, fail_login=fail_login, fail_retry=fail_retry)
    with pytest.raises(SessionAuthFailure) as exc:
        download_with_session("http://intranet.example/a.jpg", "demo")
    assert len(calls["login"]) == 1
    assert len(calls["fetch"]) == (1 if fail_login else 2)
    assert "联系管理员" in str(exc.value)
    assert "secret" not in str(exc.value) and "do-not-expose" not in str(exc.value)


def test_real_download_retry_failure_blocks_batch_and_preserves_pending_rows(client, headers, monkeypatch):
    h = headers()
    report = upload(client, h)
    with Session() as db:
        db.add(Keyword(domain="协议", key="auth-test", payload={"e": "测试关键词"}))
        db.commit()
    calls = adapter(monkeypatch, fail_retry=True)
    rules = types.ModuleType("fake_local_review")
    rules.RULE_VERSION = "test-rules-v1"
    def review_item(unit, keyword, attachment_url, criterion, threshold, value, fetchimage):
        fetchimage(attachment_url)
        raise AssertionError("must not reach model")
    rules.review_item = review_item
    monkeypatch.setitem(sys.modules, rules.__name__, rules)
    monkeypatch.setattr(settings, "local_review_module", rules.__name__)
    monkeypatch.setattr(settings, "app_mode", "local")
    monkeypatch.setattr(settings, "attachment_hosts", "intranet.example")
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    assert process_once()
    detail = client.get(f'/api/runs/{run["id"]}', headers=h).json()
    assert detail["state"] == "blocked" and detail["error_code"] == "auth_session_failed"
    assert len(calls["login"]) == 1 and len(calls["fetch"]) == 2
    rows = client.get(f'/api/runs/{run["id"]}/results', headers=h).json()
    assert [r["status"] for r in rows] == ["error", "pending", "pending"]
    assert all("secret" not in str(r) and "do-not-expose" not in str(r) for r in rows)
    assert not process_once()


def test_local_login_stores_cookie_without_returning_it_and_logout_removes_secret(client, headers, monkeypatch):
    h = headers()
    calls = adapter(monkeypatch)
    rules = types.ModuleType("fake_login_review")
    rules.RULE_VERSION = "test-v1"
    monkeypatch.setitem(sys.modules, rules.__name__, rules)
    monkeypatch.setattr(settings, "local_review_module", rules.__name__)
    monkeypatch.setattr(settings, "app_mode", "local")
    response = client.post("/api/auth/login", json={"username": "original-login-name", "password": "password-secret-123"})
    assert response.status_code == 200
    assert response.json()["user"]["username"] == "demo"
    assert all(s not in response.text for s in ["password-secret-123", "fresh-cookie-secret", "cookies"])
    assert client.post("/api/auth/logout", headers=h).status_code == 200
    with Session() as db:
        assert db.get(SessionCredential, "demo") is None
    with pytest.raises(SessionAuthFailure):
        download_with_session("http://intranet.example/a.jpg", "demo")


def test_concurrent_expiry_refreshes_once_and_shares_new_cookie(client, monkeypatch):
    calls = adapter(monkeypatch)
    module = sys.modules[settings.local_auth_module]
    original = module.fetchimage
    barrier = Barrier(2)

    def fetch(url, cookies):
        if cookies["session"] == "old-cookie-secret":
            barrier.wait(timeout=5)
        return original(url, cookies)

    module.fetchimage = fetch
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda index: download_with_session(f"http://intranet.example/{index}.jpg", "demo"), [1, 2]))
    assert all(value[0][1] == "image/jpeg" for value in values)
    assert len(calls["login"]) == 1
