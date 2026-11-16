import re
import sys
import types

from fastapi.routing import APIRoute
from sqlalchemy import select

from app.config import settings
from app.db import Session
from app.main import app
from app.models import User
from test_workflow import upload


def raw_login(client, username='demo'):
    response = client.post('/api/auth/login', json={'username': username, 'password': 'test-password-123'})
    assert response.status_code == 200, response.text
    return response.json(), {'Authorization': 'Bearer ' + response.json()['access_token']}


def test_new_user_has_no_business_access_but_can_check_identity_and_logout(client):
    identity, token = raw_login(client)
    assert identity['user']['role'] == 'none'
    assert client.get('/api/auth/me', headers=token).json()['role'] == 'none'
    # 每个受保护业务路由都拒绝，即使用户猜到资源路径也不能绕过界面。
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path.startswith('/api/auth/') or route.path == '/api/health':
            continue
        path = re.sub(r'\{[^}]+\}', 'missing-resource', route.path)
        for method in route.methods:
            response = client.request(method, path, headers=token, **({'json': {}} if method in {'POST', 'PATCH'} else {}))
            assert response.status_code == 403, (method, path, response.text)
            assert response.json()['detail'] == '当前账号无权限，请联系管理员授权'
    assert client.post('/api/auth/logout', headers=token).status_code == 200


def test_admin_grants_and_revokes_access_with_existing_token(client, headers):
    admin = headers('admin')
    identity, token = raw_login(client)
    user_id = identity['user']['id']
    endpoint = '/api/users/' + user_id
    for role in ('user', 'none', 'admin', 'none'):
        response = client.patch(endpoint, headers=admin, json={'role': role, 'active': True, 'reason': '测试权限配置'})
        assert response.status_code == 200, response.text
        assert client.get('/api/auth/me', headers=token).json()['role'] == role
        assert client.get('/api/reports', headers=token).status_code == (403 if role == 'none' else 200)
        assert client.get('/api/users', headers=token).status_code == (200 if role == 'admin' else 403)
    # 再次登录不能绕过管理员设置的无权限角色。
    assert raw_login(client)[0]['user']['role'] == 'none'
    listed = client.get('/api/users', headers=admin).json()
    assert next(row for row in listed if row['id'] == user_id)['role'] == 'none'
    assert client.patch(endpoint, headers=token, json={'role': 'admin', 'active': True, 'reason': '自助提权'}).status_code == 403


def test_existing_authorized_users_and_reports_are_preserved(client, headers):
    token = headers()
    report = upload(client, token)
    identity, again = raw_login(client)
    assert identity['user']['role'] == 'user'
    assert client.get('/api/reports', headers=again).json()[0]['id'] == report['id']


def test_bootstrap_admin_uses_authenticated_identity_and_other_accounts_default_none(client, monkeypatch):
    module = types.ModuleType('permission_login_adapter')
    module.login = lambda username, password, code, kind, maskkey, mfakey: {
        'result': 'success', 'info': {'username': 'owner' if username == 'owner-alias' else username,
                                    'cookies': {'session': 'test-cookie'}}}
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(settings, 'local_auth_module', module.__name__)
    monkeypatch.setattr(settings, 'app_mode', 'local')
    monkeypatch.setattr(settings, 'bootstrap_admins', 'owner')
    assert raw_login(client, 'owner-alias')[0]['user']['role'] == 'admin'
    assert raw_login(client, 'new-account')[0]['user']['role'] == 'none'
    with Session() as db:
        db.add(User(username='created-without-role'))
        db.commit()
        assert db.scalar(select(User).where(User.username == 'created-without-role')).role == 'none'


def test_disabled_user_cannot_login_even_if_role_is_none(client, headers):
    admin = headers('admin')
    identity, token = raw_login(client)
    assert client.patch('/api/users/' + identity['user']['id'], headers=admin,
                        json={'role': 'none', 'active': False, 'reason': '停用'}).status_code == 200
    assert client.get('/api/auth/me', headers=token).status_code == 401
    assert client.post('/api/auth/login', json={'username': 'demo', 'password': 'test-password-123'}).status_code == 403
