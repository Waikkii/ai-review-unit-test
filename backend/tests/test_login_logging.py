import sys
import types
import logging

import pytest
from app.config import settings
from app.db import Session
from app.models import User
from sqlalchemy import select


def test_invalid_password_logs_stage_and_elapsed_without_password(client, caplog):
    with caplog.at_level(logging.ERROR, logger='app.auth'):
        response = client.post('/api/auth/login', json={'username': 'demo', 'password': 'private-password-123'})
    assert response.status_code == 401
    assert 'stage=demo_credentials' in caplog.text
    assert 'username=' in caplog.text and 'elapsed_ms=' in caplog.text and 'time=' in caplog.text
    assert 'ValueError' in caplog.text and ' in authenticate' in caplog.text
    assert 'private-password-123' not in caplog.text


@pytest.mark.parametrize('failure,stage,code', [('module', 'module_load', None), ('remote', 'adapter_login', 503), ('identity', 'identity_validate', None)])
def test_adapter_failure_locations_and_http_status_are_logged_without_response_data(client, monkeypatch, caplog, failure, stage, code):
    monkeypatch.setattr(settings, 'app_mode', 'local')
    monkeypatch.setattr(settings, 'local_auth_module', 'login_logging_test_adapter')
    if failure != 'module':
        module = types.ModuleType('login_logging_test_adapter')
        def login(username, password):
            if failure == 'remote':
                try:
                    raise TimeoutError('secret-cookie-value secret-token-value ' + password)
                except TimeoutError as exc:
                    error = RuntimeError('secret-response-body secret-cookie-value ' + password)
                    error.response = types.SimpleNamespace(status_code=code, headers={'Set-Cookie': 'secret-cookie-value'}, text='secret-response-body')
                    raise error from exc
            return {'username': 'demo', 'cookies': {}}
        module.login = login
        monkeypatch.setitem(sys.modules, module.__name__, module)
    with caplog.at_level(logging.ERROR, logger='app.auth'):
        response = client.post('/api/auth/login', json={'username': 'demo', 'password': 'private-password-123'})
    assert response.status_code == 401
    assert f'stage={stage}' in caplog.text
    if code:
        assert f'http_status={code}' in caplog.text
        assert 'TimeoutError' in caplog.text and ' in login' in caplog.text
    if failure == 'identity':
        assert 'validation=Invalid authentication cookies' in caplog.text
    assert all(value not in caplog.text for value in ['private-password-123', 'secret-cookie-value', 'secret-token-value', 'secret-response-body', 'Set-Cookie'])
    assert all(record.exc_info is None for record in caplog.records if record.name == 'app.auth')


@pytest.mark.parametrize('failure,stage', [('store_credentials', 'session_store'), ('issue_token', 'token_issue')])
def test_login_persistence_failures_are_logged_and_return_generic_500(client, monkeypatch, caplog, failure, stage):
    monkeypatch.setattr('app.main.authenticate', lambda *args: {'username': 'demo', '_session_cookies': {'session': 'private-cookie'}})
    def fail(*args):
        raise RuntimeError('private-password private-cookie private-token')
    monkeypatch.setattr('app.main.' + failure, fail)
    with caplog.at_level(logging.ERROR, logger='app.auth'):
        response = client.post('/api/auth/login', json={'username': 'demo', 'password': 'private-password'})
    assert response.status_code == 500
    assert f'stage={stage}' in caplog.text and 'RuntimeError' in caplog.text
    assert all(value not in caplog.text + response.text for value in ['private-password', 'private-cookie', 'private-token'])


def test_disabled_account_has_distinct_failure_log(client, headers, caplog):
    headers()
    with Session() as db:
        db.scalar(select(User).where(User.username == 'demo')).active = False
        db.commit()
    with caplog.at_level(logging.ERROR, logger='app.auth'):
        response = client.post('/api/auth/login', json={'username': 'demo', 'password': 'test-password-123'})
    assert response.status_code == 403
    assert 'stage=account_status' in caplog.text and '账号已停用' in caplog.text
    assert 'test-password-123' not in caplog.text


def test_one_service_secret_supports_distinct_internal_users_tokens_and_sessions(client, monkeypatch):
    from app.auth_sessions import _load
    from app.models import SessionCredential
    module = types.ModuleType('multiuser_login_test_adapter')
    passwords = {'internal-user-a': 'private-user-a-password', 'internal-user-b': 'private-user-b-password'}
    def login(username, password):
        if passwords.get(username) != password:
            raise ValueError('invalid credentials')
        return {'username': username, 'cookies': {'session': 'cookie-for-' + username}}
    module.login = login
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(settings, 'app_mode', 'production')
    monkeypatch.setattr(settings, 'local_auth_module', module.__name__)
    monkeypatch.setattr(settings, 'bootstrap_admins', 'internal-user-a')
    tokens, ids = {}, set()
    for username, password in passwords.items():
        response = client.post('/api/auth/login', json={'username': username, 'password': password})
        assert response.status_code == 200, response.text
        body = response.json()
        ids.add(body['user']['id'])
        tokens[username] = body['access_token']
        assert body['user']['username'] == username
        assert body['user']['role'] == ('admin' if username == 'internal-user-a' else 'user')
        payload, _ = _load(username)
        assert payload['login_username'] == username and payload['password'] == password
        assert payload['cookies'] == {'session': 'cookie-for-' + username}
    assert len(ids) == len(set(tokens.values())) == 2
    assert client.post('/api/auth/login', json={'username':'internal-user-a', 'password':'wrong'}).status_code == 401
    for username, token in tokens.items():
        response = client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + token})
        assert response.status_code == 200 and response.json()['username'] == username
    with Session() as db:
        sessions = db.scalars(select(SessionCredential)).all()
        assert {s.username for s in sessions} == set(passwords)
        assert all(not any(secret in s.encrypted for secret in passwords.values()) for s in sessions)
