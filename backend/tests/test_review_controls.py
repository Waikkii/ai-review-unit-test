import pytest
from app.schemas import ReviewVerdict
from app.worker import process_once
from app.auth_sessions import SessionAuthFailure
from test_workflow import upload
from test_excel import detail_workbook, serialize


def start(client, h, report, mode="full"):
    response = client.post(f'/api/reports/{report["id"]}/runs', headers=h, json={"selection_mode": mode})
    assert response.status_code == 201, response.text
    return response.json()


def get_rows(client, h, rid):
    return client.get(f"/api/reports/{rid}/items", headers=h).json()


def test_domain_filters_readonly_and_queue_pause(client, headers, monkeypatch):
    h, admin = headers(), headers("admin")
    report = upload(client, h)
    rid = report["id"]
    assert client.get(f"/api/reports/{rid}/domains", headers=h).json() == ["协议", "射频"]
    rows = client.get(f"/api/reports/{rid}/items?domain=射频&offset=1&limit=1", headers=h).json()
    assert len(rows) == 1 and rows[0]["domain"] == "射频" and rows[0]["row_number"] == 3
    assert client.patch(f'/api/items/{rows[0]["id"]}', headers=h,
        json={"version": 1, "fields": rows[0]["current"], "reason": "不允许原始修订"}).status_code == 409
    run = start(client, h, report)
    assert len(client.get(f'/api/runs/{run["id"]}/results?domain=射频', headers=h).json()) == 2
    assert client.post(f'/api/runs/{run["id"]}/pause', headers=admin).status_code == 404
    paused = client.post(f'/api/runs/{run["id"]}/pause', headers=h).json()
    assert paused["state"] == "paused"
    assert not process_once()
    assert client.post(f"/api/reports/{rid}/runs", headers=h).status_code == 409
    assert client.post(f'/api/runs/{run["id"]}/resume', headers=h).json()["state"] == "queued"
    monkeypatch.setattr("app.worker.review", lambda *args: (ReviewVerdict(status="passed", explanation="验证控制"), {}))
    assert process_once()
    assert client.get(f'/api/runs/{run["id"]}', headers=h).json()["state"] == "completed"
    assert all(not row["pending_review"] for row in get_rows(client, h, rid))
    assert client.post(f"/api/reports/{rid}/runs", headers=h, json={"selection_mode": "missing"}).status_code == 409


def test_pause_finishes_current_item_and_resumes_remaining(client, headers, monkeypatch):
    h = headers()
    report = upload(client, h)
    run = start(client, h, report)
    calls = []

    def review(fields, keywords, username):
        calls.append(fields)
        if len(calls) == 1:
            assert client.post(f'/api/runs/{run["id"]}/pause', headers=h).json()["state"] == "pausing"
            assert client.post(f'/api/runs/{run["id"]}/resume', headers=h).status_code == 409
        return ReviewVerdict(status="passed", explanation="验证暂停"), {}

    monkeypatch.setattr("app.worker.review", review)
    assert process_once()
    detail = client.get(f'/api/runs/{run["id"]}', headers=h).json()
    assert (detail["state"], detail["completed"]) == ("paused", 1)
    assert not process_once()
    assert client.post(f'/api/runs/{run["id"]}/resume', headers=h).status_code == 200
    assert process_once()
    assert len(calls) == 3


def test_restart_fences_inflight_old_result_and_preserves_selected_mode(client, headers, monkeypatch):
    h = headers()
    report = upload(client, h)
    run = start(client, h, report, "missing")
    restarted = []

    def review(*args):
        if not restarted:
            response = client.post(f'/api/runs/{run["id"]}/restart', headers=h)
            assert response.status_code == 200
            restarted.append(response.json())
        return ReviewVerdict(status="passed", explanation="旧调用返回"), {}

    monkeypatch.setattr("app.worker.review", review)
    assert process_once()
    assert restarted[0]["selection_mode"] == "missing" and restarted[0]["total"] == 3
    old = client.get(f'/api/runs/{run["id"]}', headers=h).json()
    assert (old["state"], old["completed"]) == ("cancelled", 0)
    assert all(r["status"] == "pending" for r in client.get(f'/api/runs/{run["id"]}/results', headers=h).json())
    assert process_once()
    assert all(not row["pending_review"] for row in get_rows(client, h, report["id"]))


def test_effective_keyword_only_invalidates_exact_items_and_missing_only_reviews_those(client, headers, monkeypatch):
    h, admin = headers(), headers("admin")
    book, sheet = detail_workbook()
    for row, metric in ((20, "平均功率"), (21, "峰值功率")):
        sheet[f"C{row}"], sheet[f"D{row}"], sheet[f"E{row}"] = "功率测试", "常温", metric
    response = client.post("/api/reports", headers=h, files={"file": ("detail.xlsx", serialize(book))})
    report = response.json()
    monkeypatch.setattr("app.worker.review", lambda *args: (ReviewVerdict(status="needs_review", explanation="验证关键词版本"), {}))
    run = start(client, h, report)
    assert process_once()
    original_results = client.get(f'/api/runs/{run["id"]}/results', headers=h).json()
    assert all(not r["pending_review"] for r in get_rows(client, h, report["id"]))
    change = client.post("/api/keyword-entry-requests", headers=h, json={"action": "create",
        "entry": {"a": "设备A", "b": "TC-01", "c": "功率测试", "d": "常温", "e": "平均功率", "i": "功率"},
        "base_version": 0, "reason": "补充关键词"}).json()
    assert all(not r["pending_review"] for r in get_rows(client, h, report["id"]))
    assert client.post(f'/api/keyword-requests/{change["id"]}/decision', headers=admin,
                       json={"approve": True, "note": "生效"}).status_code == 200
    pending = [r for r in get_rows(client, h, report["id"]) if r["pending_review"]]
    assert len(pending) == 1 and pending[0]["row_number"] == 20
    filtered = client.get(f'/api/runs/{run["id"]}/results?status=pending', headers=h).json()
    assert len(filtered) == 1 and filtered[0]["status"] == "needs_review"
    supplement = start(client, h, report, "missing")
    assert supplement["total"] == 1
    calls = []

    def review(fields, keywords, username):
        calls.append((fields, keywords))
        return ReviewVerdict(status="passed", explanation="补审"), {}

    monkeypatch.setattr("app.worker.review", review)
    assert process_once()
    assert len(calls) == 1 and calls[0][1][0]["payload"]["i"] == "功率"
    assert all(not r["pending_review"] for r in get_rows(client, h, report["id"]))
    old_results = client.get(f'/api/runs/{run["id"]}/results', headers=h).json()
    assert [r["raw"] for r in old_results] == [r["raw"] for r in original_results]
    restarted = client.post(f'/api/runs/{supplement["id"]}/restart', headers=h, json={"selection_mode": "full"}).json()
    assert restarted["selection_mode"] == "full" and restarted["total"] == 3


def test_keyword_change_during_inflight_review_cannot_clear_pending_revision(client, headers, monkeypatch):
    h = headers("admin")
    report = upload(client, h)
    run = start(client, h, report)
    calls = []

    def review(fields, keywords, username):
        calls.append(fields)
        if len(calls) == 1:
            assert client.post("/api/keyword-requests", headers=h, json={"action": "create", "domain": "协议",
                "key": "updated", "payload": {"word": "新词"}, "reason": "审查期间更新"}).status_code == 201
        return ReviewVerdict(status="passed", explanation="旧规则"), {}

    monkeypatch.setattr("app.worker.review", review)
    assert process_once()
    pending = [r for r in get_rows(client, h, report["id"]) if r["pending_review"]]
    assert len(pending) == 1 and pending[0]["domain"] == "协议"


def test_auth_failure_stops_batch_and_manual_resume_retries_failed_item(client, headers, monkeypatch):
    h = headers()
    report = upload(client, h)
    run = start(client, h, report)
    calls = []

    def failing(*args):
        calls.append(args)
        raise SessionAuthFailure()

    monkeypatch.setattr("app.worker.review", failing)
    assert process_once()
    assert len(calls) == 1
    detail = client.get(f'/api/runs/{run["id"]}', headers=h).json()
    assert (detail["state"], detail["completed"], detail["failed"]) == ("blocked", 1, 1)
    assert detail["error_code"] == "auth_session_failed" and "联系管理员" in detail["error_message"]
    assert not process_once()
    monkeypatch.setattr("app.worker.review", lambda *args: (ReviewVerdict(status="passed", explanation="恢复"), {}))
    resumed = client.post(f'/api/runs/{run["id"]}/resume', headers=h).json()
    assert resumed["completed"] == resumed["failed"] == 0
    assert process_once()
    detail = client.get(f'/api/runs/{run["id"]}', headers=h).json()
    assert (detail["state"], detail["completed"], detail["failed"]) == ("completed", 3, 0)


def test_restart_uses_ui_selected_mode_and_keeps_completed_items_in_missing_mode(client, headers, monkeypatch):
    h = headers()
    report = upload(client, h)
    run = start(client, h, report, "full")
    calls = []

    def review(*args):
        calls.append(args)
        if len(calls) == 1:
            client.post(f'/api/runs/{run["id"]}/pause', headers=h)
        return ReviewVerdict(status="passed", explanation="完成当前项"), {}

    monkeypatch.setattr("app.worker.review", review)
    assert process_once()
    restarted = client.post(f'/api/runs/{run["id"]}/restart', headers=h,
                            json={"selection_mode": "missing"}).json()
    assert restarted["selection_mode"] == "missing" and restarted["total"] == 2
    assert process_once()
    assert len(calls) == 3
    rejected = client.post(f'/api/runs/{restarted["id"]}/restart', headers=h,
                           json={"selection_mode": "missing"})
    assert rejected.status_code == 409
    assert client.get(f'/api/runs/{restarted["id"]}', headers=h).json()["state"] == "completed"
    full = client.post(f'/api/runs/{restarted["id"]}/restart', headers=h,
                       json={"selection_mode": "full"}).json()
    assert full["selection_mode"] == "full" and full["total"] == 3


def test_missing_keyword_is_visible_exported_and_retried_after_keyword_is_added(client, headers):
    from io import BytesIO
    from openpyxl import load_workbook
    h, admin = headers(), headers("admin")
    report = upload(client, h)
    # Two rows have a usable keyword; the other domain has no matching keyword.
    assert client.post("/api/keyword-requests", headers=admin, json={"action": "create", "domain": "射频",
        "key": "existing", "payload": {"e": "功率"}, "reason": "已有关键词"}).status_code == 201
    run = start(client, h, report)
    assert process_once()
    endpoint = f'/api/runs/{run["id"]}/results'
    missing = client.get(endpoint + "?status=missing_keyword", headers=h).json()
    assert len(missing) == 1 and missing[0]["domain"] == "协议"
    assert missing[0]["explanation"] == "缺少关键词" and missing[0]["pending_review"]
    items = get_rows(client, h, report["id"])
    item = next(row for row in items if row["domain"] == "协议")
    assert item["pending_review"] and item["review"]["status"] == "missing_keyword"
    assert len(client.get(endpoint + "?status=pending", headers=h).json()) == 1
    book = load_workbook(BytesIO(client.get(f'/api/runs/{run["id"]}/export', headers=h).content))
    sheet = book.worksheets[-1]
    headers_row = [cell.value for cell in sheet[1]]
    assert sheet.cell(2, headers_row.index("AI结果") + 1).value == "缺少关键词"
    book.close()
    assert client.post("/api/keyword-requests", headers=admin, json={"action": "create", "domain": "协议",
        "key": "added", "payload": {"e": "协议"}, "reason": "补齐关键词"}).status_code == 201
    assert client.get(endpoint + "?status=missing_keyword", headers=h).json() == []
    supplement = start(client, h, report, "missing")
    assert supplement["total"] == 1
    assert process_once()
    assert all(not row["pending_review"] for row in get_rows(client, h, report["id"]))
    old = client.get(endpoint, headers=h).json()
    assert old[0]["status"] == "missing_keyword"  # Historical conclusion is retained.
