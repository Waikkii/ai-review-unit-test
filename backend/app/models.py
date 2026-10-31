from sqlalchemy import String, Text, Integer, Boolean, DateTime, JSON, ForeignKey, UniqueConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, now, uid

class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(128), unique=True)
    role: Mapped[str] = mapped_column(String(16), default="user")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=now)

class Report(Base):
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    item_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[object] = mapped_column(DateTime, default=now)

class Item(Base):
    __tablename__ = "items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"), index=True)
    domain: Mapped[str] = mapped_column(String(128))
    row_number: Mapped[int] = mapped_column(Integer)
    original: Mapped[dict] = mapped_column(JSON)
    current: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)

class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    revision: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    total: Mapped[int] = mapped_column(Integer)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    keyword_snapshot: Mapped[list] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(String(128))
    rule_version: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(16))
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=now)
    __table_args__ = (Index("one_active_run_per_report", "report_id", unique=True,
        sqlite_where=text("state IN ('queued', 'running')"),
        postgresql_where=text("state IN ('queued', 'running')")),)

class Result(Base):
    __tablename__ = "results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"))
    domain: Mapped[str] = mapped_column(String(128))
    row_number: Mapped[int] = mapped_column(Integer)
    fields: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    explanation: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    suggestion: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("run_id", "item_id"),)

class Keyword(Base):
    __tablename__ = "keywords"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    domain: Mapped[str] = mapped_column(String(128))
    key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("domain", "key"),)

class ChangeRequest(Base):
    __tablename__ = "change_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(16))
    domain: Mapped[str] = mapped_column(String(128))
    key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON)
    base_version: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    result_id: Mapped[str | None] = mapped_column(ForeignKey("results.id"), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[object] = mapped_column(DateTime, default=now)

class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(64))
    object_id: Mapped[str] = mapped_column(String(36), index=True)
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[object] = mapped_column(DateTime, default=now)
