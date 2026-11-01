from io import BytesIO

import pytest
from openpyxl import Workbook

from app.excel import parse_excel
from app.schemas import COLUMNS
from app.worker import process_once


def detail_workbook():
    book = Workbook()
    book.active.title = "封面"
    book.active["A1"] = "测试报告"
    book.create_sheet("目录")["A1"] = "领域列表"
    sheet = book.create_sheet("领域1")
    sheet["A1"] = "测试总结"
    sheet["J9"] = "总结中的值不参与审查"
    sheet["A14"] = "测试详情"
    sheet.merge_cells("A14:C14")
    sheet["A15"] = "测试对象"
    for col, name in enumerate(COLUMNS[:7], start=2):
        sheet.cell(15, col, name)
        sheet.merge_cells(start_row=15, end_row=16, start_column=col, end_column=col)
    sheet["I15"] = "第一轮"
    sheet.merge_cells("I15:L15")
    for col, name in enumerate(COLUMNS[7:], start=9):
        sheet.cell(16, col, name)
    sheet["D17"] = "供应商名称"
    sheet.merge_cells("D17:H17")
    sheet["I17"] = "/"
    sheet.merge_cells("I17:L17")
    sheet["B18"] = "TC-01"
    sheet["A18"] = "设备A"
    sheet.merge_cells("A18:A19")
    sheet.merge_cells("B18:B19")
    sheet["J18"] = 0
    sheet["J19"] = "正常"
    sheet["L19"] = "附件占位"
    sheet["J20"] = "  "
    sheet["L20"] = "https://intranet.example/skip.jpg"
    sheet["J21"] = "通过"
    sheet["L21"] = "查看附件"
    sheet["L21"].hyperlink = "https://intranet.example/ok.jpg"
    return book, sheet


def serialize(book):
    stream = BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def test_detail_two_row_header_and_value_selection():
    book, _ = detail_workbook()
    rows = parse_excel(serialize(book))
    assert [r["row_number"] for r in rows] == [18, 19, 21]
    assert all(r["domain"] == "领域1" for r in rows)
    assert rows[0]["fields"]["值"] == "0"
    assert rows[0]["fields"]["附件链接"] == ""
    assert rows[1]["fields"]["用例编号"] == "TC-01"
    assert rows[1]["fields"]["测试对象"] == "设备A"
    assert rows[1]["fields"]["附件链接"] == "附件占位"
    assert rows[2]["fields"]["附件链接"] == "https://intranet.example/ok.jpg"


def test_empty_value_skips_other_formulas_but_selected_formula_is_rejected():
    book, sheet = detail_workbook()
    sheet["D20"] = "=1+1"
    assert len(parse_excel(serialize(book))) == 3
    book, sheet = detail_workbook()
    sheet["J18"] = "=1+1"
    with pytest.raises(ValueError, match="公式转成值"):
        parse_excel(serialize(book))


def test_ambiguous_rounds_fail_instead_of_silently_omitting_rows():
    book, sheet = detail_workbook()
    sheet["N16"] = "值"
    with pytest.raises(ValueError, match="完整、唯一"):
        parse_excel(serialize(book))


def test_detail_upload_edit_review_and_export(client, headers):
    book, _ = detail_workbook()
    h = headers()
    response = client.post("/api/reports", headers=h,
                           files={"file": ("detail.xlsx", serialize(book))})
    assert response.status_code == 201, response.text
    report = response.json()
    items = client.get(f'/api/reports/{report["id"]}/items', headers=h).json()
    item = next(i for i in items if i["row_number"] == 18)
    edit = {"version": 1, "fields": item["current"], "reason": "修订文字项"}
    assert client.patch(f'/api/items/{item["id"]}', headers=h, json=edit).status_code == 200
    edit["fields"] = {**edit["fields"], "值": " "}
    edit["version"] = 2
    assert client.patch(f'/api/items/{item["id"]}', headers=h, json=edit).status_code == 422
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    assert process_once()
    results = client.get(f'/api/runs/{run["id"]}/results', headers=h).json()
    assert len(results) == 3
    assert all(r["status"] == "needs_review" for r in results)
    from openpyxl import load_workbook
    exported = client.get(f'/api/runs/{run["id"]}/export', headers=h)
    with BytesIO(exported.content) as stream:
        out = load_workbook(stream)
        assert out.sheetnames[:3] == ["封面", "目录", "领域1"]
        assert out["领域1"]["J18"].value == 0
        assert out.worksheets[-1].max_row == 4
        out.close()
