"""Opaque browser sessions; only explicit user activity extends their idle timeout."""
from datetime import timedelta
from hashlib import sha256
from secrets import token_urlsafe
from sqlalchemy import delete
from .config import settings
from .db import now
from .models import BrowserSession

COOKIE_NAME = "review_session"


def session_hash(value):
    return sha256(value.encode()).hexdigest()


def idle_seconds():
    return settings.token_minutes * 60


def idle_deadline():
    return now() - timedelta(seconds=idle_seconds())


def set_session_cookie(response, request, value):
    response.set_cookie(COOKIE_NAME, value, max_age=idle_seconds(), httponly=True,
                        secure=request.url.scheme == "https", samesite="strict", path="/api")


def create_browser_session(db, user_id, request, response):
    db.execute(delete(BrowserSession).where(BrowserSession.last_activity_at <= idle_deadline()))
    previous = request.cookies.get(COOKIE_NAME)
    if previous:
        db.execute(delete(BrowserSession).where(BrowserSession.token_hash == session_hash(previous)))
    value = token_urlsafe(32)
    db.add(BrowserSession(token_hash=session_hash(value), user_id=user_id))
    set_session_cookie(response, request, value)
