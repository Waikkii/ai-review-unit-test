"""Read-only keyword seed import into a new, empty application database."""
from pathlib import Path
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DatabaseSession

from .db import Base
from .keywords import ENTRY_DOMAIN, entry_key
from .models import Keyword
from .schemas import KeywordEntry


def read_seed(source, table=None, keyword_column=None):
    source = Path(source).resolve(strict=True)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
        candidates = []
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            quoted = '"' + name.replace('"', '""') + '"'
            columns = {row[1].lower(): row[1] for row in db.execute("PRAGMA table_info(" + quoted + ")")}
            choices = [keyword_column.lower()] if keyword_column else [key for key in ("i", "valve") if key in columns]
            if all(key in columns for key in ("a", "b", "c", "d")) and len(choices) == 1 and choices[0] in columns:
                candidates.append((name, quoted, columns, choices[0]))
        if table:
            candidates = [candidate for candidate in candidates if candidate[0] == table]
        if len(candidates) != 1:
            raise ValueError("无法唯一确定 A/B/C/D 和 I（或 valve）字段，请指定 --table 和 --keyword-column")
        name, quoted, columns, keyword = candidates[0]
        fields = [columns[key] for key in ("a", "b", "c", "d", keyword)]
        query = "SELECT " + ", ".join('"' + field.replace('"', '""') + '"' for field in fields) + " FROM " + quoted
        payloads, seen = [], set()
        for number, row in enumerate(db.execute(query), 1):
            if not all(isinstance(value, str) for value in row):
                raise ValueError(f"源表第 {number} 条记录含 NULL 或非 TEXT 字段")
            try:
                payload = KeywordEntry.model_validate(dict(zip(("a", "b", "c", "d", "e"), row))).model_dump()
            except ValueError:
                raise ValueError(f"源表第 {number} 条记录不符合关键词格式") from None
            key = entry_key(payload)
            if key in seen:
                raise ValueError(f"源表第 {number} 条记录的 A/B/C/D 联合键重复")
            seen.add(key)
            payloads.append(payload)
        if not payloads:
            raise ValueError("源关键词表为空，未初始化")
        return name, payloads


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
