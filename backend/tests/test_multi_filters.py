from io import BytesIO
from openpyxl import Workbook
from sqlalchemy import select

from app.db import Session
from app.models import ItemReviewState, Result, Run
from app.schemas import COLUMNS


def batch(client, headers):
    book = Workbook()
    book.remove(book.active)
    for domain in ("领域 A&B", "领域二", "协议"):
        sheet = book.create_sheet(domain)
        sheet.append(COLUMNS)
        for index in range(3):
            values = [""] * len(COLUMNS)
            values[0], values[1], values[8] = str(index), "测试用例", "0"
            sheet.append(values)
    stream = BytesIO()
    book.save(stream)
    book.close()
    report = client.post('/api/reports', headers=headers,
                         files={"file": ("filters.xlsx", stream.getvalue())}).json()
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=headers).json()
    statuses = [("passed", "通过说明"), ("needs_review", "功率错误，请人工排查。"),
                ("failed", "电压判断错误。"), ("needs_review", "无有效图片。"),
                ("missing_keyword", "缺少关键词"), ("error", "内部错误不作为选项"),
                ("needs_review", "频率错误，请人工排查。"),
                ("needs_review", "功率错误，请人工排查。"), ("pending", "")]
    with Session() as db:
        rows = db.scalars(select(Result).where(Result.run_id == run["id"])
                          .order_by(Result.domain, Result.row_number)).all()
        for row, (status, explanation) in zip(rows, statuses):
            row.status, row.explanation = status, explanation
            state = db.get(ItemReviewState, row.item_id)
            if status not in {"pending", "missing_keyword", "error"}:
                state.reviewed_revision = state.revision
        db.get(Run, run["id"]).state = "partial_failed"
        db.commit()
    return report, run


def test_domain_and_result_multiselect_apply_before_pagination(client, headers):
    h = headers()
    report, run = batch(client, h)
    endpoint = f'/api/runs/{run["id"]}/results'
    all_rows = client.get(endpoint, headers=h).json()
    domains = ["领域 A&B", "协议"]
    expected = [row for row in all_rows if row["domain"] in domains]
    rows = client.get(endpoint, headers=h, params=[("domain", d) for d in domains] +
                      [("offset", 3), ("limit", 2)]).json()
    assert [row["id"] for row in rows] == [row["id"] for row in expected[3:5]]
    item_rows = client.get(f'/api/reports/{report["id"]}/items', headers=h,
                          params=[("domain", d) for d in domains] + [("offset", 3), ("limit", 2)]).json()
    assert [(row["domain"], row["row_number"]) for row in item_rows] == [
        (row["domain"], row["row_number"]) for row in expected[3:5]]
    choices = ["通过", "功率错误，请人工排查。"]
    rows = client.get(endpoint, headers=h, params=[("domain", d) for d in domains] +
                      [("result", label) for label in choices]).json()
    assert {row["id"] for row in rows} == {row["id"] for row in expected if row["result_label"] in choices}
    assert len(rows) == 2
    assert client.get(endpoint, headers=h, params={"result": "不存在的结果"}).json() == []


def test_result_options_include_full_batch_custom_messages_and_system_states(client, headers):
    h = headers()
    _, run = batch(client, h)
    endpoint = f'/api/runs/{run["id"]}'
    first_page = client.get(endpoint + '/results?limit=1', headers=h).json()
    assert first_page[0]["result_label"] == "通过"
    choices = client.get(endpoint + '/result-options', headers=h).json()
    assert set(choices) == {"通过", "功率错误，请人工排查。", "电压判断错误。", "无有效图片。",
                            "缺少关键词", "执行失败", "频率错误，请人工排查。", "待审查"}
    rows = client.get(endpoint + '/results', headers=h,
                      params=[("status", "error"), ("status", "missing_keyword")]).json()
    assert {row["result_label"] for row in rows} == {"执行失败", "缺少关键词"}
    pending = client.get(endpoint + '/results?status=pending', headers=h).json()
    assert {row["result_label"] for row in pending} == {"待审查", "缺少关键词"}
    display_pending = client.get(endpoint + '/results', headers=h, params={"result": "待审查"}).json()
    assert len(display_pending) == 1


def test_filter_options_do_not_expose_other_users_reports(client, headers):
    owner, admin = headers(), headers("admin")
    _, run = batch(client, admin)
    assert client.get(f'/api/runs/{run["id"]}/result-options', headers=owner).status_code == 404


def test_invalidated_results_move_to_pending_filter(client, headers):
    h = headers()
    _, run = batch(client, h)
    with Session() as db:
        row = db.scalar(select(Result).where(Result.run_id == run["id"], Result.status == "failed"))
        state = db.get(ItemReviewState, row.item_id)
        state.revision += 1
        db.commit()
    endpoint = f'/api/runs/{run["id"]}/results'
    assert client.get(endpoint, headers=h, params={"result": "电压判断错误。"}).json() == []
    assert len(client.get(endpoint, headers=h, params={"result": "待审查"}).json()) == 2
