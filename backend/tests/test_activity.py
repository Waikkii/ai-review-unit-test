from sqlalchemy import select

from app.db import Session
from app.models import User
from app.services import audit
from app.worker import process_once
from test_workflow import upload, change


def test_activity_feed_is_concise_named_and_paginated_before_display(client, headers):
    user, admin = headers(), headers("admin")
    report = upload(client, user)
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=user).json()
    assert process_once()
    assert client.get(f'/api/reports/{report["id"]}/original', headers=user).status_code == 200
    assert client.get(f'/api/runs/{run["id"]}/export', headers=user).status_code == 200
    assert client.post("/api/keyword-requests", headers=user, json=change()).status_code == 201
    with Session() as db:
        actor = db.scalar(select(User).where(User.username == "demo"))
        for _ in range(40):
            audit(db, actor, "item.reviewed", report["id"])
        db.commit()
    rows = client.get("/api/audit", headers=admin).json()
    assert len(rows) == 5
    assert {r["action"] for r in rows} == {"report.upload", "run.start", "report.download", "run.export", "keyword.request"}
    assert all(r["actor_username"] == "demo" for r in rows)
    assert {r["action_label"] for r in rows} == {"上传报告", "发起 AI 审查", "下载原始报告", "导出审查结果", "提交关键词新增"}
    assert all(r["target_label"] == "sample.xlsx" for r in rows if r["category"] != "keyword")
    page_one = client.get("/api/audit?limit=2", headers=admin).json()
    page_two = client.get("/api/audit?limit=2&offset=2", headers=admin).json()
    assert len(page_one) == len(page_two) == 2
    assert not ({r["id"] for r in page_one} & {r["id"] for r in page_two})
    assert len(client.get("/api/audit?summary=false&limit=200", headers=admin).json()) > 40
    upload(client, admin)
    assert len(client.get("/api/audit", headers=admin).json()) == 6
    own = client.get("/api/audit", headers=user).json()
    assert len(own) == 5 and all(r["actor_username"] == "demo" for r in own)
