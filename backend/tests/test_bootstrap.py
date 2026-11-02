import importlib.util
from pathlib import Path
import sqlite3
import sys

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.bootstrap import create_business_database, read_seed
from app.config import ROOT, Settings
from app.keywords import ENTRY_DOMAIN, entry_key
from app.models import Keyword, Report, Run, User


def source_db(tmp_path, column="I", rows=None):
    path = tmp_path / "test.db"
    with sqlite3.connect(path) as db:
        db.execute(f'CREATE TABLE seed (A TEXT, B TEXT, C TEXT, D TEXT, "{column}" TEXT)')
        db.executemany("INSERT INTO seed VALUES (?, ?, ?, ?, ?)", rows or [
            ("对象", "001", "用例", "", "关键词"), ("对象", "001", "用例", " ", "第二个关键词")])
    return path


@pytest.mark.parametrize("column", ["I", "valve"])
def test_seed_import_preserves_exact_keys_and_creates_empty_history(tmp_path, column):
    source = source_db(tmp_path, column)
    original = source.read_bytes()
    table, payloads = read_seed(source)
    assert table == "seed"
    assert payloads[0] == {"a": "对象", "b": "001", "c": "用例", "d": "", "e": "关键词"}
    target = tmp_path / "review.db"
    create_business_database(target, payloads)
    engine = create_engine("sqlite:///" + target.as_posix())
    with Session(engine) as db:
        keywords = db.scalars(select(Keyword)).all()
        assert len(keywords) == 2
        assert all(keyword.domain == ENTRY_DOMAIN and keyword.key == entry_key(keyword.payload)
                   and keyword.version == 1 and not keyword.deleted for keyword in keywords)
        for model in (User, Report, Run):
            assert db.scalar(select(func.count()).select_from(model)) == 0
    engine.dispose()
    assert source.read_bytes() == original
    with pytest.raises(FileExistsError):
        create_business_database(target, payloads)


@pytest.mark.parametrize("rows", [
    [("a", "b", "c", "d", " ")], [("a", "b", "c", None, "word")],
    [("a", "b", "c", "d", "one"), ("a", "b", "c", "d", "two")]])
def test_invalid_source_rejected(tmp_path, rows):
    with pytest.raises(ValueError):
        read_seed(source_db(tmp_path, rows=rows))


def test_missing_source_does_not_create_empty_database(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        read_seed(path)
    assert not path.exists()


def test_ambiguous_source_tables_require_table_argument(tmp_path):
    path = source_db(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE other AS SELECT * FROM seed')
    with pytest.raises(ValueError):
        read_seed(path)
    assert read_seed(path, table="seed")[0] == "seed"


def test_setup_preview_then_apply_preserves_secrets_and_old_database(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("formal_setup_test", ROOT / "scripts" / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    source = source_db(tmp_path)
    target = tmp_path / "data" / "review.db"
    env = tmp_path / ".env"
    text = "APP_MODE=demo\nSECRET_KEY=" + "s" * 40 + "\nMODEL=model-name\nAPPLY_TOKEN='token # secret'\nDEMO_PASSWORD=old-password\n"
    env.write_text(text, encoding="utf-8")
    old = tmp_path / "demo.db"
    old.write_bytes(b"existing business data")
    monkeypatch.setattr(sys, "argv", ["setup.py", "--source", str(source), "--target", str(target)])
    module.main()
    assert not target.exists() and env.read_text(encoding="utf-8") == text
    monkeypatch.setattr(sys, "argv", ["setup.py", "--source", str(source), "--target", str(target),
                                      "--admin-user", "real-admin", "--apply"])
    module.main()
    for name in ("APP_MODE", "DATABASE_URL", "DATA_DIR", "SECRET_KEY", "DEMO_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=env)
    assert settings.app_mode == "production" and settings.database_url.endswith("/review.db")
    assert settings.model == "model-name" and settings.apply_token == "token # secret"
    assert settings.secret_key == "s" * 40 and settings.bootstrap_admins == "real-admin"
    assert "DEMO_PASSWORD" not in env.read_text(encoding="utf-8")
    settings.validate_runtime()
    assert old.read_bytes() == b"existing business data"
    assert len(list((tmp_path / "data" / "config_backups").glob("*.bak"))) == 1


def test_production_requires_real_adapters_and_accepts_sqlite():
    settings = Settings(_env_file=None, app_mode="production", database_url="sqlite:///review.db",
                        secret_key="s" * 40, local_auth_module="", local_review_module="")
    with pytest.raises(RuntimeError):
        settings.validate_runtime()
    settings.local_auth_module = "local_adapters.login_adapter"
    settings.local_review_module = "local_adapters.review_rules"
    settings.validate_runtime()
