"""Read-only keyword seed import into a new, empty application database."""
from contextlib import closing
from pathlib import Path
import sqlite3

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session as DatabaseSession

from .db import Base
from .keywords import ENTRY_DOMAIN, KEY_FIELDS, entry_key
from .models import ChangeRequest, Keyword, User
from .review_workflow import invalidate_keyword_entries
from .schemas import KeywordEntry
from .services import audit, keyword_dict


def read_seed(source, table=None, keyword_column=None):
    source = Path(source).resolve(strict=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        candidates = []
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            quoted = '"' + name.replace('"', '""') + '"'
            columns = {row[1].lower(): row[1] for row in db.execute("PRAGMA table_info(" + quoted + ")")}
            choices = [keyword_column.lower()] if keyword_column else [key for key in ("i", "valve") if key in columns]
            if (KEY_FIELDS.keys() <= columns.keys() and len(choices) == 1
                    and choices[0] in columns and choices[0] not in KEY_FIELDS):
                candidates.append((name, quoted, columns, choices[0]))
        if table:
            candidates = [candidate for candidate in candidates if candidate[0] == table]
        if len(candidates) != 1:
            raise ValueError("无法唯一确定 A/B/C/D/E 和 I（或 valve）字段。旧库请先用 expand_keyword_db.py 扩充")
        name, quoted, columns, keyword = candidates[0]
        fields = [columns[key] for key in (*KEY_FIELDS, keyword)]
        query = "SELECT " + ", ".join('"' + field.replace('"', '""') + '"' for field in fields) + " FROM " + quoted
        payloads, seen = [], set()
        for number, row in enumerate(db.execute(query), 1):
            if not all(isinstance(value, str) for value in row):
                raise ValueError(f"源表第 {number} 条记录含 NULL 或非 TEXT 字段")
            try:
                payload = KeywordEntry.model_validate(dict(zip((*KEY_FIELDS, "i"), row))).model_dump()
            except ValueError:
                raise ValueError(f"源表第 {number} 条记录不符合关键词格式") from None
            key = entry_key(payload)
            if key in seen:
                raise ValueError(f"源表第 {number} 条记录的 A/B/C/D/E 联合键重复")
            seen.add(key)
            payloads.append(payload)
        if not payloads:
            raise ValueError("源关键词表为空，未初始化")
        return name, payloads


def import_existing_keywords(target, payloads, admin_user=None, apply=False):
    target = Path(target).resolve(strict=True)
    engine = create_engine("sqlite:///" + target.as_posix())
    try:
        with DatabaseSession(engine) as db:
            if apply:
                db.execute(text("BEGIN IMMEDIATE"))
            existing = {row.key: row for row in db.scalars(select(Keyword).where(Keyword.domain == ENTRY_DOMAIN))}
            pending = set(db.scalars(select(ChangeRequest.key).where(
                ChangeRequest.domain == ENTRY_DOMAIN, ChangeRequest.state == "pending")))
            additions = {}
            for payload in payloads:
                key = entry_key(payload)
                current = existing.get(key)
                if key in pending or (current and (current.deleted or current.payload != payload)):
                    raise ValueError(f"指标 {payload['e']!r} 的五字段键存在待审批、已删除或不同关键词的记录")
                if not current:
                    additions[key] = payload
            if apply:
                admin = db.scalar(select(User).where(
                    User.username == admin_user, User.role == "admin", User.active.is_(True)))
                if admin is None:
                    raise ValueError("请用 --admin-user 指定已有的启用管理员账号")
                keywords = [Keyword(domain=ENTRY_DOMAIN, key=key, payload=payload, version=1, deleted=False)
                            for key, payload in additions.items()]
                db.add_all(keywords)
                db.flush()
                for keyword in keywords:
                    audit(db, admin, "keyword.create", keyword.id, after=keyword_dict(keyword),
                          reason="导入包含指标名称的本地关键词库")
                invalidate_keyword_entries(db, additions.values())
                db.commit()
            return len(additions), len(payloads) - len(additions)
    finally:
        engine.dispose()


def create_business_database(target, payloads):
    """Refuse existing targets; never copy source history, users, or credentials."""
    target = Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create prevents replacing an existing application database.
    with target.open("xb"):
        pass
    engine = None
    try:
        engine = create_engine("sqlite:///" + target.as_posix())
        Base.metadata.create_all(engine)
        with DatabaseSession(engine) as db:
            for payload in payloads:
                payload = KeywordEntry.model_validate(payload).model_dump()
                db.add(Keyword(domain=ENTRY_DOMAIN, key=entry_key(payload), payload=payload,
                               version=1, deleted=False))
            db.commit()
    except Exception:
        if engine:
            engine.dispose()
        target.unlink(missing_ok=True)
        raise
    else:
        engine.dispose()
