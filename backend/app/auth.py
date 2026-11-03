from datetime import timedelta
from secrets import compare_digest
import importlib
import logging
import time
import traceback
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from .config import settings
from .db import get_db, now
from .models import User

bearer = HTTPBearer(auto_error=False)
log = logging.getLogger(__name__)


class AuthenticationFailure(Exception):
    def __init__(self, stage):
        super().__init__("Authentication failed")
        self.stage = stage


def log_login_failure(stage, username, started, exc=None):
    """记录定位信息，不打印异常原文、源码行、局部变量或 HTTP 请求响应。"""
    reasons = {
        "demo_credentials": "演示账号或密码校验失败", "module_load": "加载登录适配器失败",
        "adapter_login": "调用本地登录接口失败", "identity_validate": "认证返回格式校验失败（需 username 和非空 cookies）",
        "identity_name": "认证返回的用户名无效", "account_status": "账号已停用",
        "user_lookup": "读取用户失败", "user_create": "保存用户失败", "session_store": "保存加密会话失败",
        "database_commit": "提交登录数据失败", "token_issue": "签发访问令牌失败",
    }
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen and len(chain) < 6:
        seen.add(id(exc))
        status = getattr(getattr(exc, "response", None), "status_code", None)
        code = getattr(exc, "errno", None)
        summary = f"exception={type(exc).__name__}"
        if isinstance(exc, ValueError) and str(exc) in {"invalid credentials", "Invalid authentication identity", "Invalid authentication cookies"}:
            summary += f" validation={str(exc)}"
        if isinstance(status, int):
            summary += f" http_status={status}"
        if isinstance(code, int):
            summary += f" errno={code}"
        frames = "\n".join(f"  {f.filename}:{f.lineno} in {f.name}" for f in traceback.extract_tb(exc.__traceback__))
        chain.append(summary + ("\n" + frames if frames else ""))
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    log.error("Login failed time=%sZ username=%r stage=%s elapsed_ms=%d reason=%s\n%s",
              now().isoformat(timespec="seconds"), username, stage, (time.perf_counter() - started) * 1000,
              reasons.get(stage, "登录处理失败"), "\nCaused by:\n".join(chain) or "No exception (account policy)")

def authenticate(username, password):
    stage = "demo_credentials"
    try:
        if settings.app_mode == "demo":
            if username not in {"demo", "admin"} or not compare_digest(password, settings.demo_password):
                raise ValueError("invalid credentials")
            return {"username": username}
        from .auth_sessions import validate_identity
        stage = "module_load"
        module = importlib.import_module(settings.local_auth_module)
        stage = "adapter_login"
        identity = module.login(username, password)
        stage = "identity_validate"
        identity = validate_identity(identity)
        return {"username": identity["username"], "_session_cookies": identity["cookies"]}
    except Exception as exc:
        raise AuthenticationFailure(stage) from exc

def issue_token(user):
    return jwt.encode({"sub": user.id, "exp": now() + timedelta(minutes=settings.token_minutes),
                       "iat": now(), "iss": "unit-test-review", "aud": "review-ui"},
                      settings.secret_key, algorithm="HS256")

def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db=Depends(get_db)):
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

def admin_user(user=Depends(current_user)):
    if user.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user
