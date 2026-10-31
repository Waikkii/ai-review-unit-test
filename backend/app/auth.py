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

def authenticate(username, password):
    if settings.app_mode == "demo":
        if username not in {"demo", "admin"} or not compare_digest(password, settings.demo_password):
            raise ValueError("invalid credentials")
        return {"username": username}
    return importlib.import_module(settings.integration_module).authenticate(username, password)

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
