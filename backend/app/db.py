from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings

def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)

def uid():
    return str(uuid4())

class Base(DeclarativeBase):
    pass

engine = create_engine(settings.database_url, pool_pre_ping=True,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {})
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def sqlite_pragmas(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")

Session = sessionmaker(bind=engine, expire_on_commit=False)

def get_db():
    with Session() as db:
        yield db
