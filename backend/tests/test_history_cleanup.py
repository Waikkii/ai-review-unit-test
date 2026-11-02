import pytest
from sqlalchemy import func, select

from app.cleanup import clean_audit, reset_history
from app.config import settings
from app.db import Session
from app.models import (Audit, ChangeRequest, Item, ItemNote, ItemReviewState, Report,
                        Result, ResultMeasurements, ResultWork, Run, RunControl, User)
from app.worker import process_once
from test_workflow import upload, change


def test_clear_report_removes_related_data_preserves_other_report_and_keyword_request(client, headers):
    owner, admin = headers(), headers("admin")
    report = upload(client, owner)
    other = upload(client, owner)
    endpoint = f'/api/reports/{report["id"]}'
    run = client.post(endpoint + "/runs", headers=owner).json()
    assert process_once()
    results = client.get(f'/api/runs/{run["id"]}/results', headers=owner).json()
    result = results[0]
    item = client.get(endpoint + "/items", headers=owner).json()[0]
    assert client.patch(f'/api/items/{item["id"]}/note', headers=owner,
                        json={"note": "note", "version": 0}).status_code == 200
    request = client.post("/api/keyword-requests", headers=owner,
                          json={**change(domain=result["domain"]), "result_id": result["id"]})
    assert request.status_code == 201
    assert client.delete(endpoint, headers=admin).status_code == 404
    path = settings.data_dir / (report["id"] + ".xlsx")
    assert path.exists()
    assert client.delete(endpoint, headers=owner).status_code == 200
    assert not path.exists()
    assert client.get(endpoint + "/items", headers=owner).status_code == 404
    assert client.get(f'/api/reports/{other["id"]}/items', headers=owner).status_code == 200
    with Session() as db:
        assert db.get(Report, other["id"])
        assert db.get(ChangeRequest, request.json()["id"]).result_id is None
        for model in (Run, Result, RunControl, ResultWork, ResultMeasurements, ItemNote):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        assert db.scalar(select(func.count()).select_from(Item)) == other["item_count"]
        assert db.scalar(select(func.count()).select_from(ItemReviewState)) == other["item_count"]
    rows = client.get('/api/audit', headers=owner).json()
    assert any(row["action_label"] == "清除报告历史" and row["target_label"] == "sample.xlsx" for row in rows)


@pytest.mark.parametrize("state", ["queued", "running", "pausing", "paused"])
def test_clear_report_rejects_active_run(client, headers, state):
    owner = headers()
    report = upload(client, owner)
    endpoint = f'/api/reports/{report["id"]}'
    run = client.post(endpoint + "/runs", headers=owner).json()
    with Session() as db:
        db.get(Run, run["id"]).state = state
        db.commit()
    assert client.delete(endpoint, headers=owner).status_code == 409
    assert client.get(endpoint + "/items", headers=owner).status_code == 200


def test_old_audit_cleanup_dry_run_and_apply_preserve_business_data(client, headers):
    owner = headers()
    report = upload(client, owner)
    with Session() as db:
        actor = db.scalar(select(User))
        db.add(Audit(actor_id=actor.id, action="item.reviewed", object_id=report["id"]))
        db.add(Audit(actor_id=actor.id, action="run.start", object_id=report["id"],
                     after={"keywords": [{"payload": {"e": "word"}}], "model": "test"}))
        db.commit()
        assert clean_audit(db) == {"redundant_logs": 1, "duplicate_snapshots": 1}
        assert db.scalar(select(func.count()).select_from(Audit)) == 3
        assert clean_audit(db, apply=True) == {"redundant_logs": 1, "duplicate_snapshots": 1}
        db.commit()
        assert db.scalar(select(func.count()).select_from(Audit)) == 2
        assert db.scalar(select(Audit).where(Audit.action == "run.start")).after == {"model": "test"}
        assert db.get(Report, report["id"])
        assert clean_audit(db, apply=True) == {"redundant_logs": 0, "duplicate_snapshots": 0}


def test_cleanup_command_backs_up_before_deleting_and_vacuums(tmp_path, monkeypatch):
    import sqlite3
    import sys
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import cleanup
    from app.db import Base

    path = tmp_path / "business.db"
    local_engine = create_engine("sqlite:///" + path.as_posix())
    Base.metadata.create_all(local_engine)
    local_session = sessionmaker(bind=local_engine)
    with local_session() as db:
        user = User(username="backup-test")
        db.add(user)
        db.flush()
        db.add(Audit(actor_id=user.id, action="session.login", object_id=user.id))
        db.commit()
    monkeypatch.setattr(cleanup, "engine", local_engine)
    monkeypatch.setattr(cleanup, "Session", local_session)
    monkeypatch.setattr(sys, "argv", ["cleanup_db.py", "--apply", "--vacuum"])
    cleanup.main()
    backups = list(tmp_path.glob("business.db.backup-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
    with local_session() as db:
        assert db.scalar(select(func.count()).select_from(Audit)) == 0
        assert db.scalar(select(func.count()).select_from(User)) == 1
    local_engine.dispose()


def test_reset_history_dry_run_and_apply_preserve_users_keywords_and_approvals(client, headers):
    from app.models import Keyword

    owner, admin = headers(), headers("admin")
    report = upload(client, owner)
    assert client.post('/api/keyword-requests', headers=admin, json=change()).status_code == 201
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=owner).json()
    assert process_once()
    result = next(row for row in client.get(f'/api/runs/{run["id"]}/results', headers=owner).json()
                  if row["domain"] == "射频")
    request = {**change(action="update", version=1, value="new-word"), "result_id": result["id"]}
    assert client.post('/api/keyword-requests', headers=owner, json=request).status_code == 201
    with Session() as db:
        assert reset_history(db) == {"reports": 1, "runs": 1, "results": 3}
        assert db.get(Report, report["id"])
        reset_history(db, apply=True)
        db.commit()
        for model in (Report, Item, Run, Result, ItemReviewState, RunControl, ResultMeasurements, ResultWork):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        assert db.scalar(select(func.count()).select_from(User)) == 2
        assert db.scalar(select(func.count()).select_from(Keyword)) == 1
        approvals = db.scalars(select(ChangeRequest)).all()
        assert len(approvals) == 2 and all(change.result_id is None for change in approvals)
        assert {change.state for change in approvals} == {"approved", "pending"}
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "keyword.create")) == 1
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "run.start")) == 0
