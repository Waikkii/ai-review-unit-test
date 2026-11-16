"""Encrypted, cross-process login sessions with exactly one refresh/retry."""
import base64
import hashlib
import importlib
import json
from datetime import timedelta
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, update, select
from .config import settings
from .db import Session, now
from .models import SessionCredential, BrowserSession, User

AUTH_FAILURE_MESSAGE = "图片访问认证失败，自动重新登录或重试后仍失败。请联系管理员处理，后续审查已停止。"


class CookieExpired(Exception):
    """Signal an expired login session; request/relogin logic lives in the wrapper."""

    def __init__(self, message="登录状态已失效"):
        super().__init__(message)


class SessionAuthFailure(Exception):
    def __init__(self):
        super().__init__(AUTH_FAILURE_MESSAGE)


class AuthenticationRejected(ValueError):
    """适配器明确返回的、允许向登录用户展示的失败原因。"""


def unwrap_login_result(response):
    if isinstance(response, dict) and "result" in response:
        if response["result"] == "fail":
            reason = response.get("reason")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                raise ValueError("Invalid authentication failure reason")
            raise AuthenticationRejected(reason)
        if response["result"] == "verify":
            if "mask_key" in response or "mfa_key" in response:
                if any(not isinstance(response.get(key), str) or not response[key].strip()
                       or len(response[key]) > 4096 for key in ("mask_key", "mfa_key")):
                    raise ValueError("Invalid MFA context")
                return {"mask_key": response["mask_key"], "mfa_key": response["mfa_key"]}
            return {"mfa_key": response.get("mfakey")}
        if response["result"] != "success" or not isinstance(response.get("info"), dict):
            raise ValueError("Invalid authentication result")
        return response["info"]
    return response


def _cipher():
    if len(settings.secret_key) < 32:
        raise SessionAuthFailure()
    key = hashlib.sha256(("auth-session-v1:" + settings.secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def validate_identity(identity):
    identity = unwrap_login_result(identity)
    if not isinstance(identity, dict) or not isinstance(identity.get("username"), str):
        raise ValueError("Invalid authentication identity")
    cookies = identity.get("cookies")
    if not isinstance(cookies, (dict, list)) or not cookies or len(json.dumps(cookies)) > 64000:
        raise ValueError("Invalid authentication cookies")
    return identity


def call_adapter_login(module, username, password, code=None, verification_type="sms", maskkey=None, mfakey=None):
    return module.login(username, password, code, verification_type, maskkey, mfakey)


def store_credentials(db, username, login_username, password, cookies, verification_type="sms"):
    payload = {"login_username": login_username, "password": password, "cookies": cookies}
    payload["verification_type"] = verification_type
    encrypted = _cipher().encrypt(json.dumps(payload).encode()).decode()
    db.execute(delete(SessionCredential).where(
        SessionCredential.updated_at < now() - timedelta(seconds=settings.credential_ttl_seconds)))
    updated = db.execute(update(SessionCredential).where(SessionCredential.username == username)
                         .values(encrypted=encrypted, version=SessionCredential.version + 1, updated_at=now()))
    if updated.rowcount == 0:
        db.add(SessionCredential(username=username, encrypted=encrypted))


def retain_active_credentials(db, username):
    record = db.get(SessionCredential, username)
    cutoff = now() - timedelta(seconds=max(1, min(1800, settings.credential_ttl_seconds // 2)))
    if record is None or record.updated_at >= cutoff:
        return
    previous = record.encrypted
    payload = _decode(previous)
    encrypted = _cipher().encrypt(json.dumps(payload).encode()).decode()
    # 保留活跃用户的加密凭据；不覆盖并发重登获取的新 Cookie，也不改变 Cookie 版本。
    db.execute(update(SessionCredential).where(SessionCredential.username == username,
               SessionCredential.encrypted == previous).values(encrypted=encrypted, updated_at=now()))


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


def _request_images(module, url, cookies):
    try:
        return module.fetchimage(url, cookies)
    except Exception as exc:
        response = getattr(exc, "response", None)
        if isinstance(exc, CookieExpired) or getattr(response, "status_code", None) in {401, 403}:
            raise CookieExpired() from None
        raise


def invalidate_browser_sessions(username):
    with Session() as db:
        db.execute(delete(BrowserSession).where(BrowserSession.user_id.in_(
            select(User.id).where(User.username == username))))
        db.commit()


def download_with_session(url: str, username: str) -> list[tuple[str, str]]:
    module = importlib.import_module(settings.local_auth_module)
    try:
        payload, version = _load(username)
    except SessionAuthFailure:
        invalidate_browser_sessions(username)
        raise
    try:
        return _request_images(module, url, payload["cookies"])
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
                identity = validate_identity(call_adapter_login(
                    module, payload["login_username"], payload["password"],
                    verification_type=payload.get("verification_type", "sms")))
                if identity["username"] != username:
                    raise SessionAuthFailure()
                payload["cookies"] = identity["cookies"]
                record.encrypted = _cipher().encrypt(json.dumps(payload).encode()).decode()
                record.version, record.updated_at = record.version + 1, now()
            db.commit()
        return _request_images(module, url, payload["cookies"])
    except Exception:
        invalidate_browser_sessions(username)
        raise SessionAuthFailure() from None
