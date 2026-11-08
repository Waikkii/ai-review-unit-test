from io import BytesIO
import json

import pytest
from openpyxl import Workbook

from app.db import Session
from app.keywords import ENTRY_DOMAIN, LEGACY_ENTRY_DOMAIN, entry_key, matching_keywords
from app.models import Run
from app.schemas import COLUMNS, ReviewVerdict
from app.worker import process_once


ENTRY = {"a": "设备A", "b": "TC-01", "c": "功率测试", "d": "常温", "e": "平均功率", "i": "功率"}


def submit(client, h, action="create", version=0, entry=None, **extra):
    return client.post("/api/keyword-entry-requests", headers=h, json={
        "action": action, "entry": entry or ENTRY, "base_version": version,
        "reason": "维护关键词", **extra})


def test_entry_approval_version_duplicate_and_delete(client, headers):
    user, admin = headers(), headers("admin")
    response = submit(client, user)
    assert response.status_code == 201
    assert client.get("/api/keywords", headers=user).json() == []
    request_id = response.json()["id"]
    assert client.post(f"/api/keyword-requests/{request_id}/decision", headers=admin,
                       json={"approve": True, "note": "同意"}).status_code == 200
    keyword = client.get("/api/keywords", headers=user).json()[0]
    assert keyword["payload"] == ENTRY
    assert keyword["key"] == entry_key(ENTRY)
    # i 不属于联合主键，换关键词仍然是重复新增。
    changed = {**ENTRY, "i": "输出功率"}
    assert submit(client, admin, entry=changed).status_code == 409
    assert submit(client, admin, "update", 1, changed).status_code == 201
    assert submit(client, admin, "update", 1, ENTRY).status_code == 409
    assert submit(client, admin, "delete", 2, changed).status_code == 201
    assert client.get("/api/keywords", headers=user).json() == []
    audits = client.get("/api/audit?summary=false", headers=admin).json()
    assert {a["action"] for a in audits} >= {"keyword.create", "keyword.update", "keyword.delete"}


@pytest.mark.parametrize("entry", [{**ENTRY, "i": ["功率"]}, {**ENTRY, "i": " "},
                                  {**ENTRY, "b": 123}, {**ENTRY, "f": "额外列"}])
def test_entry_requires_six_text_fields(client, headers, entry):
    assert submit(client, headers("admin"), entry=entry).status_code == 422


def test_exact_five_part_identity():
    snapshot = [{"domain": ENTRY_DOMAIN, "payload": ENTRY, "deleted": False}]
    fields = {"测试对象": ENTRY["a"], "用例编号": ENTRY["b"],
              "用例名称": ENTRY["c"], "测试小项": ENTRY["d"], "指标名称": ENTRY["e"]}
    assert matching_keywords(snapshot, fields, "任意领域") == snapshot
    for field in fields:
        assert matching_keywords(snapshot, {**fields, field: fields[field] + " "}, "任意领域") == []
    assert matching_keywords(snapshot, {k: v for k, v in fields.items() if k != "测试对象"}, "领域") == []
    assert entry_key({**ENTRY, "a": "ab", "b": "c"}) != entry_key({**ENTRY, "a": "a", "b": "bc"})


def test_old_run_uses_current_keywords_by_metric_and_checks_linked_item(client, headers, monkeypatch):
    h = headers("admin")
    assert submit(client, h).status_code == 201
    assert submit(client, h, entry={**ENTRY, "e": "峰值功率", "i": "峰值"}).status_code == 201
    book = Workbook()
    sheet = book.active
    sheet.title = "领域名与主键无关"
    sheet.append(["测试对象", *COLUMNS])
    for metric in (ENTRY["e"], "峰值功率", "无关键词指标"):
        sheet.append([ENTRY["a"], ENTRY["b"], ENTRY["c"], ENTRY["d"], metric, "", "", "", "", "正常", "", ""])
    out = BytesIO()
    book.save(out)
    book.close()
    response = client.post("/api/reports", headers=h, files={"file": ("report.xlsx", out.getvalue())})
    assert response.status_code == 201, response.text
    rid = response.json()["id"]
    table = client.get(f"/api/reports/{rid}/table-view", headers=h,
                       params={"filters": json.dumps({"keyword": ["峰值"]})}).json()
    assert table["total"] == 1 and table["items"][0]["current"]["指标名称"] == "峰值功率"
    run = client.post(f"/api/reports/{rid}/runs", headers=h).json()
    with Session() as db:
        db.get(Run, run["id"]).keyword_snapshot = [{
            "domain": LEGACY_ENTRY_DOMAIN,
            "payload": {**{key: ENTRY[key] for key in "abcd"}, "e": "旧关键词"},
        }]
        db.commit()
    assert submit(client, h, "update", 1, {**ENTRY, "i": "新关键词"}).status_code == 201
    seen = []

    def review(fields, keywords, username):
        seen.append(keywords)
        return ReviewVerdict(status="needs_review", explanation="匹配验证"), {}

    monkeypatch.setattr("app.worker.review", review)
    assert process_once()
    assert [[entry["payload"]["i"] for entry in entries] for entries in seen] == [["新关键词"], ["峰值"], []]
    results = client.get(f'/api/runs/{run["id"]}/results', headers=h).json()
    assert not results[0]["pending_review"]
    assert submit(client, h, "update", 2, result_id=results[1]["id"]).status_code == 422
    assert submit(client, h, "update", 2, result_id=results[0]["id"]).status_code == 201


def test_legacy_api_cannot_forge_composite_identity(client, headers):
    response = client.post("/api/keyword-requests", headers=headers("admin"), json={
        "action": "create", "domain": ENTRY_DOMAIN, "key": "wrong", "payload": ENTRY,
        "base_version": 0, "reason": "验证联合键"})
    assert response.status_code == 422
