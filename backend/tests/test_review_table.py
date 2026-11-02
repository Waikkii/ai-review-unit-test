from io import BytesIO

from openpyxl import load_workbook
from sqlalchemy import create_engine, inspect

from app.models import Base, ItemNote, ResultMeasurements
from app.schemas import ReviewVerdict
from app.worker import process_once
from test_workflow import upload


def test_notes_owner_permissions_conflicts_and_original_fields(client, headers):
    h, admin = headers(), headers("admin")
    report = upload(client, h)
    path = f'/api/reports/{report["id"]}/items'
    item = client.get(path, headers=h).json()[0]
    endpoint = f'/api/items/{item["id"]}/note'
    assert (item["note"], item["note_version"]) == ("", 0)
    assert client.patch(endpoint, headers=admin, json={"note": "越权", "version": 0}).status_code == 404
    response = client.patch(endpoint, headers=h, json={"note": "需核实图片", "version": 0})
    assert response.status_code == 200
    assert response.json() == {"note": "需核实图片", "version": 1}
    assert client.patch(endpoint, headers=h, json={"note": "旧版本", "version": 0}).status_code == 409
    assert client.patch(endpoint, headers=h, json={"note": "x", "version": 1, "status": "passed"}).status_code == 422
    updated = client.get(path, headers=h).json()[0]
    assert updated["note"] == "需核实图片"
    assert updated["current"] == item["current"]
    assert updated["version"] == item["version"]
    assert client.patch(endpoint, headers=h, json={"note": "", "version": 1}).json()["version"] == 2
    assert client.get(path, headers=h).json()[0]["note"] == ""
    assert any(a["action"] == "item.note" for a in client.get("/api/audit?summary=false", headers=h).json())


def test_progress_measurements_notes_filters_and_export(client, headers, monkeypatch):
    h = headers()
    report = upload(client, h)
    rid = report["id"]
    items = client.get(f"/api/reports/{rid}/items", headers=h).json()
    run = client.post(f"/api/reports/{rid}/runs", headers=h).json()
    endpoint = f'/api/runs/{run["id"]}/results'
    before = client.get(endpoint, headers=h).json()
    assert all(r["status"] == "pending" and r["ai_threshold"] == r["ai_value"] == r["note"] == "" for r in before)
    first_id = before[0]["item_id"]
    assert client.patch(f'/api/items/{first_id}/note', headers=h,
                        json={"note": "用户补充", "version": 0}).status_code == 200
    calls = []

    def review(fields, keywords, username):
        calls.append(fields)
        if len(calls) == 2:
            # First row is committed while later rows remain pending.
            during = client.get(endpoint, headers=h).json()
            assert during[0]["status"] == "passed"
            assert during[0]["ai_threshold"] == "≤ 10 dB"
            assert during[0]["ai_value"] == "8.5 dB"
            assert during[0]["note"] == "用户补充"
            assert all(r["status"] == "pending" for r in during[1:])
        return ReviewVerdict(status="passed", explanation="图片读取结果符合规则",
                             ai_threshold="≤ 10 dB", ai_value="8.5 dB"), {"original": True}

    monkeypatch.setattr("app.worker.review", review)
    assert process_once()
    rows = client.get(endpoint, headers=h).json()
    assert len(rows) == len(items)
    assert rows[0]["fields"] == before[0]["fields"]
    assert rows[0]["raw"] == {"original": True}
    assert len(client.get(endpoint + "?status=passed", headers=h).json()) == 3
    assert client.get(endpoint + "?status=pending", headers=h).json() == []
    assert client.get(endpoint + "?status=failed", headers=h).json() == []
    keyword_request = client.post("/api/keyword-requests", headers=h, json={
        "action": "create", "domain": rows[0]["domain"], "key": "completed-row-keyword",
        "payload": {"keyword": "测试"}, "reason": "调整已审查项关键词", "result_id": rows[0]["id"]})
    assert keyword_request.status_code == 201
    # Notes are editable separately from the completed AI verdict.
    assert client.patch(f'/api/items/{first_id}/note', headers=h,
                        json={"note": "=自定义备注", "version": 1}).status_code == 200
    rows = client.get(endpoint, headers=h).json()
    assert rows[0]["status"] == "passed" and rows[0]["note_version"] == 2
    exported = client.get(f'/api/runs/{run["id"]}/export', headers=h)
    book = load_workbook(BytesIO(exported.content))
    sheet = book.worksheets[-1]
    names = [c.value for c in sheet[1]]
    assert sheet.cell(2, names.index("AI门限") + 1).value == "≤ 10 dB"
    assert sheet.cell(2, names.index("AI值") + 1).value == "8.5 dB"
    assert sheet.cell(2, names.index("用户备注") + 1).value == "'=自定义备注"
    book.close()


def test_additive_upgrade_is_idempotent_and_preserves_existing_tables(tmp_path, monkeypatch):
    import app.migrate as migration
    engine = create_engine("sqlite:///" + (tmp_path / "legacy.db").as_posix())
    old_tables = [t for t in Base.metadata.sorted_tables if t not in (ItemNote.__table__, ResultMeasurements.__table__)]
    Base.metadata.create_all(engine, tables=old_tables)
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO users (id, username, role, active, created_at) VALUES ('old', 'existing', 'user', 1, CURRENT_TIMESTAMP)")
    monkeypatch.setattr(migration, "engine", engine)
    migration.main()
    migration.main()
    assert {"item_notes", "result_measurements"} <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT username FROM users WHERE id='old'").scalar() == "existing"
    engine.dispose()
