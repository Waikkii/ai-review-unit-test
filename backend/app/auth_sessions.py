"""Encrypted, cross-process login sessions with exactly one refresh/retry."""
import base64
import hashlib
import importlib
import json
from datetime import timedelta
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, update
from .config import settings
from .db import Session, now
from .models import SessionCredential

AUTH_FAILURE_MESSAGE = "图片访问认证失败，自动重新登录或重试后仍失败。请联系管理员处理，后续审查已停止。"


class CookieExpired(Exception):
    """Local download adapter raises this for expired cookies (401/403/login page)."""


class SessionAuthFailure(Exception):
    def __init__(self):
        super().__init__(AUTH_FAILURE_MESSAGE)


def _cipher():
    if len(settings.secret_key) < 32:
        raise SessionAuthFailure()
    key = hashlib.sha256(("auth-session-v1:" + settings.secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def validate_identity(identity):
    if not isinstance(identity, dict) or not isinstance(identity.get("username"), str):
        raise ValueError("Invalid authentication identity")
    cookies = identity.get("cookies")
    if not isinstance(cookies, (dict, list)) or not cookies or len(json.dumps(cookies)) > 64000:
        raise ValueError("Invalid authentication cookies")
    return identity


def store_credentials(db, username, login_username, password, cookies):
    payload = {"login_username": login_username, "password": password, "cookies": cookies}
    encrypted = _cipher().encrypt(json.dumps(payload).encode()).decode()
    db.execute(delete(SessionCredential).where(
        SessionCredential.updated_at < now() - timedelta(seconds=settings.credential_ttl_seconds)))
    updated = db.execute(update(SessionCredential).where(SessionCredential.username == username)
                         .values(encrypted=encrypted, version=SessionCredential.version + 1, updated_at=now()))
    if updated.rowcount == 0:
        db.add(SessionCredential(username=username, encrypted=encrypted))


def _load(username):
    with Session() as db:
        record = db.get(SessionCredential, username)
        if not record:
            raise SessionAuthFailure()
        encrypted, version = record.encrypted, record.version
    return _decode(encrypted), version


def _decode(encrypted):
    try:
        return json.loads(_cipher().decrypt(encrypted.encode(), ttl=settings.credential_ttl_seconds))
    except (InvalidToken, ValueError, TypeError):
        raise SessionAuthFailure() from None


def download_with_session(url, username):
    module = importlib.import_module(settings.local_auth_module)
    payload, version = _load(username)
    try:
        return module.fetchimage(url, payload["cookies"])
    except CookieExpired:
        pass
    try:
        with Session() as db:
            # Serialize refresh across workers. SQLite takes a writer lock; PostgreSQL a row lock.
            locked = db.execute(update(SessionCredential).where(SessionCredential.username == username)
                                .values(version=SessionCredential.version))
            if locked.rowcount != 1:
                raise SessionAuthFailure()
            record = db.get(SessionCredential, username, populate_existing=True)
            current = _decode(record.encrypted)
            if record.version != version or current != payload:
                payload = current
            else:
                identity = validate_identity(module.login(payload["login_username"], payload["password"]))
                if identity["username"] != username:
                    raise SessionAuthFailure()
                payload["cookies"] = identity["cookies"]
                record.encrypted = _cipher().encrypt(json.dumps(payload).encode()).decode()
                record.version, record.updated_at = record.version + 1, now()
            db.commit()
        return module.fetchimage(url, payload["cookies"])
    except Exception:
        raise SessionAuthFailure() from None
