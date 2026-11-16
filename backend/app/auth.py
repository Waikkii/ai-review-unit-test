from datetime import timedelta
from secrets import compare_digest
import importlib
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from .config import settings
from .db import get_db, now
from .models import User

bearer = HTTPBearer(auto_error=False)

def authenticate(username, password, code=None, verification_type="sms", mask_key=None, mfa_key=None):
    if settings.app_mode == "demo":
        if username not in {"demo", "admin"} or not compare_digest(password, settings.demo_password):
            raise ValueError("invalid credentials")
        return {"username": username}
    from .auth_sessions import validate_identity, unwrap_login_result, call_adapter_login
    adapter = importlib.import_module(settings.local_auth_module)
    identity = call_adapter_login(adapter, username, password, code, verification_type, mask_key, mfa_key)
    identity = unwrap_login_result(identity)
    if isinstance(identity, dict) and "mfa_key" in identity:
        key = identity["mfa_key"]
        if not isinstance(key, str) or not key.strip() or len(key) > 4096:
            raise ValueError("Invalid MFA challenge")
        response = {"mfa_required": True, "mfa_key": key}
        if "mask_key" in identity:
            response["mask_key"] = identity["mask_key"]
        return response
    identity = validate_identity(identity)
    return {"username": identity["username"], "_session_cookies": identity["cookies"]}

def send_verify_code(verification_type, maskkey, mfakey):
    if settings.app_mode == "demo":
        raise ValueError("MFA unavailable in demo mode")
    result = importlib.import_module(settings.local_auth_module).send_verify_code(verification_type, maskkey, mfakey)
    if result != "success":
        raise ValueError("Verification code delivery failed")

def issue_token(user):
    return jwt.encode({"sub": user.id, "exp": now() + timedelta(minutes=settings.token_minutes),
                       "iat": now(), "iss": "unit-test-review", "aud": "review-ui"},
                      settings.secret_key, algorithm="HS256")

def authenticated_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db=Depends(get_db)):
    try:
        if credentials is None:
            raise ValueError()
        payload = jwt.decode(credentials.credentials, settings.secret_key, algorithms=["HS256"],
                             issuer="unit-test-review", audience="review-ui")
        user = db.scalar(select(User).where(User.id == payload["sub"], User.active.is_(True)))
        if not user:
            raise ValueError()
        return user
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "登录已失效，请重新登录")

def current_user(user=Depends(authenticated_user)):
    if user.role not in {"user", "admin"}:
        raise HTTPException(403, "当前账号无权限，请联系管理员授权")
    return user

def admin_user(user=Depends(current_user)):
    if user.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user
