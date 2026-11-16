from datetime import timedelta
import sys
import types

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.config import settings
from app.db import Session, now
from app.models import BrowserSession, SessionCredential
from app.browser_sessions import COOKIE_NAME, session_hash
from app.auth_sessions import CookieExpired, SessionAuthFailure, download_with_session, store_credentials, _decode


def login(client):
    response = client.post('/api/auth/login', json={'username': 'admin', 'password': 'test-password-123'})
    assert response.status_code == 200, response.text
    return response


def test_cookie_restores_identity_without_bearer_or_password(client):
    response = login(client)
    assert response.json()['idle_timeout_seconds'] == settings.token_minutes * 60
    header = response.headers['set-cookie'].lower()
    assert 'httponly' in header and 'samesite=strict' in header and 'path=/api' in header
    cookie = client.cookies.get(COOKIE_NAME)
    with Session() as db:
        session = db.scalar(select(BrowserSession))
        assert session.token_hash == session_hash(cookie) and session.token_hash != cookie
    with TestClient(app) as refreshed:
        refreshed.cookies.set(COOKIE_NAME, cookie, path='/api')
        result = refreshed.get('/api/auth/session')
        assert result.status_code == 200
        assert result.json()['user']['username'] == 'admin'
        assert result.json()['mode'] == 'demo'
        assert 'access_token' not in result.text and 'password' not in result.text


def test_polling_does_not_extend_idle_but_user_activity_does(client, monkeypatch):
    monkeypatch.setattr(settings, 'token_minutes', 1)
    login(client)
    key = session_hash(client.cookies.get(COOKIE_NAME))
    earlier = now() - timedelta(seconds=20)
    with Session() as db:
        db.get(BrowserSession, key).last_activity_at = earlier
        db.commit()
    for _ in range(2):
        assert client.get('/api/reports').status_code == 200
        assert client.get('/api/auth/session').status_code == 200
    with Session() as db:
        assert db.get(BrowserSession, key).last_activity_at == earlier
    response = client.post('/api/auth/activity')
    assert response.status_code == 200 and 'Max-Age=60' in response.headers['set-cookie']
    with Session() as db:
        assert db.get(BrowserSession, key).last_activity_at > earlier


def test_expired_session_cannot_be_revived_by_activity(client, monkeypatch):
    monkeypatch.setattr(settings, 'token_minutes', 1)
    login(client)
    key = session_hash(client.cookies.get(COOKIE_NAME))
    with Session() as db:
        db.get(BrowserSession, key).last_activity_at = now() - timedelta(seconds=61)
        db.commit()
    assert client.get('/api/auth/session').status_code == 401
    assert client.get('/api/reports').status_code == 401
    assert client.post('/api/auth/activity').status_code == 401


def test_logout_revokes_saved_cookie(client):
    login(client)
    cookie = client.cookies.get(COOKIE_NAME)
    assert client.post('/api/auth/logout').status_code == 200
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, cookie, path='/api')
    assert client.get('/api/auth/session').status_code == 401


def test_cookie_authenticated_writes_reject_cross_origin(client):
    login(client)
    assert client.post('/api/auth/activity', headers={'Origin': 'https://other-site.example'}).status_code == 403
    assert client.post('/api/auth/activity', headers={'Origin': 'http://testserver'}).status_code == 200


def test_idle_exit_keeps_worker_credentials_and_activity_preserves_credential_lifetime(client):
    login(client)
    with Session() as db:
        store_credentials(db, 'admin', 'admin', 'private-password', {'session': 'cookie'})
        db.commit()
        record = db.get(SessionCredential, 'admin')
        record.updated_at = now() - timedelta(hours=1)
        previous, version = record.encrypted, record.version
        db.commit()
    assert client.post('/api/auth/activity').status_code == 200
    with Session() as db:
        record = db.get(SessionCredential, 'admin')
        assert record.updated_at > now() - timedelta(minutes=1)
        assert record.encrypted != previous and record.version == version
        assert _decode(record.encrypted)['cookies'] == {'session': 'cookie'}
    assert client.post('/api/auth/session/end').status_code == 200
    assert client.get('/api/auth/session').status_code == 401
    with Session() as db:
        assert db.get(SessionCredential, 'admin') is not None


@pytest.mark.parametrize('refresh_fails', [False, True])
def test_external_cookie_refresh_only_revokes_browser_session_on_failure(client, monkeypatch, refresh_fails):
    module = types.ModuleType('browser_cookie_adapter')
    calls = []
    def authenticate(username, password, code, kind, maskkey, mfakey):
        calls.append(username)
        if len(calls) > 1 and refresh_fails:
            return {'result': 'verify', 'mask_key': 'mask', 'mfa_key': 'key'}
        return {'result': 'success', 'info': {'username': username, 'cookies': {'session': str(len(calls))}}}
    def fetch(url, cookies):
        if cookies['session'] == '1':
            raise CookieExpired()
        return []
    module.login, module.fetchimage = authenticate, fetch
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(settings, 'app_mode', 'local')
    monkeypatch.setattr(settings, 'local_auth_module', module.__name__)
    login(client)
    if refresh_fails:
        with pytest.raises(SessionAuthFailure):
            download_with_session('https://intranet/image', 'admin')
    else:
        assert download_with_session('https://intranet/image', 'admin') == []
    assert client.get('/api/auth/session').status_code == (401 if refresh_fails else 200)
    assert len(calls) == 2


def test_mfa_challenge_does_not_create_browser_session(client, monkeypatch):
    module = types.ModuleType('browser_challenge_adapter')
    module.login = lambda *args: {'result': 'verify', 'mask_key': 'mask', 'mfa_key': 'key'}
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(settings, 'app_mode', 'local')
    monkeypatch.setattr(settings, 'local_auth_module', module.__name__)
    response = client.post('/api/auth/login', json={'username': 'account', 'password': 'password'})
    assert response.status_code == 200 and response.json()['mfa_required']
    assert 'set-cookie' not in response.headers
    with Session() as db:
        assert db.scalar(select(BrowserSession)) is None
