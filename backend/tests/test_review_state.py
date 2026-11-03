import pytest
from sqlalchemy import select
from app.db import Session
from app.models import Result
from app.reviewer import review
from app.worker import process_once
from test_workflow import upload
from test_excel import detail_workbook, serialize


@pytest.mark.parametrize("fields,expected", [
    ({"附件链接": "", "判据门限": ">=10"}, "缺少附件"),
    ({"附件链接": "  ", "判据门限": "中文"}, "缺少附件"),
    ({"附件链接": "https://intranet/image", "判据门限": "外观正常"}, "主观类"),
    ({"附件链接": "https://intranet/image", "判据门限": ">=10，且无破损"}, "主观类"),
])
def test_automatic_rules_before_keywords_and_external_calls(monkeypatch, fields, expected):
    import types
    def forbidden(*args):
        raise AssertionError("automatic rules must not download or load local adapters")
    monkeypatch.setattr("app.reviewer.download_with_session", forbidden)
    monkeypatch.setattr("app.reviewer.importlib", types.SimpleNamespace(import_module=forbidden))
    verdict, raw = review(fields, [], "user")
    assert verdict.explanation == expected and verdict.status == "needs_review"
    assert raw["system"]
    assert verdict.ai_threshold == verdict.ai_value == ""


def test_automatic_rules_are_reviewed_and_not_selected_by_missing_mode(client, headers):
    h = headers()
    book, sheet = detail_workbook()
    sheet.cell(21, 7, "外观正常")
    report = client.post('/api/reports', headers=h, files={"file": ("auto.xlsx", serialize(book))}).json()
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    assert process_once()
    rows = client.get(f'/api/runs/{run["id"]}/results', headers=h).json()
    assert [(row["explanation"], row["pending_review"]) for row in rows] == [
        ("缺少附件", False), ("主观类", False), ("缺少关键词", True)]
    items = client.get(f'/api/reports/{report["id"]}/items', headers=h).json()
    assert [item["review"]["result_label"] for item in items] == ["缺少附件", "主观类", "缺少关键词"]
    from io import BytesIO
    from openpyxl import load_workbook
    exported = client.get(f'/api/runs/{run["id"]}/export', headers=h)
    book = load_workbook(BytesIO(exported.content))
    sheet = book["领域1"]
    assert [sheet.cell(row, 15).value for row in (20, 21, 23)] == ["缺少附件", "主观类", "缺少关键词"]
    book.close()
    supplement = client.post(f'/api/reports/{report["id"]}/runs', headers=h,
                             json={"selection_mode": "missing"}).json()
    assert supplement["total"] == 1


def test_owner_can_toggle_review_state_with_conflict_check_without_changing_ai(client, headers):
    owner, admin = headers(), headers("admin")
    report = upload(client, owner)
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=owner).json()
    assert process_once()
    endpoint = f'/api/reports/{report["id"]}/items'
    item = client.get(endpoint, headers=owner).json()[0]
    path = f'/api/items/{item["id"]}/review-state'
    body = {"state": "reviewed", "revision": item["review_revision"], "reviewed_revision": item["reviewed_revision"]}
    assert client.patch(path, headers=admin, json=body).status_code == 404
    with Session() as db:
        result = db.scalar(select(Result).where(Result.item_id == item["id"]))
        original = (result.id, result.status, result.explanation, result.raw)
    assert client.patch(path, headers=owner, json=body).status_code == 200
    assert client.patch(path, headers=owner, json=body).status_code == 409
    current = client.get(endpoint, headers=owner).json()[0]
    assert not current["pending_review"]
    body = {"state": "pending", "revision": current["review_revision"], "reviewed_revision": current["reviewed_revision"]}
    assert client.patch(path, headers=owner, json=body).status_code == 200
    assert client.get(endpoint, headers=owner).json()[0]["pending_review"]
    with Session() as db:
        result = db.get(Result, original[0])
        assert (result.id, result.status, result.explanation, result.raw) == original
    rows = client.get('/api/audit', headers=owner).json()
    assert len([row for row in rows if row["action"] == "item.review_state"]) == 2


def test_manual_state_blocks_active_runs_and_allows_marking_unreviewed_items_done(client, headers):
    h = headers()
    report = upload(client, h)
    item = client.get(f'/api/reports/{report["id"]}/items', headers=h).json()[0]
    path = f'/api/items/{item["id"]}/review-state'
    body = {"state": "reviewed", "revision": item["review_revision"], "reviewed_revision": item["reviewed_revision"]}
    assert client.patch(path, headers=h, json={**body, "state": "passed"}).status_code == 422
    assert client.patch(path, headers=h, json=body).status_code == 200
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=h, json={"selection_mode": "missing"}).json()
    assert run["total"] == report["item_count"] - 1
    current = client.get(f'/api/reports/{report["id"]}/items', headers=h).json()[0]
    assert client.patch(path, headers=h, json={**body, "revision": current["review_revision"],
                                             "reviewed_revision": current["reviewed_revision"]}).status_code == 409
