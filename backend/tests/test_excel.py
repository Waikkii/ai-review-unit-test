from io import BytesIO

import pytest
from openpyxl import Workbook

from app.excel import parse_excel
from app.schemas import COLUMNS
from app.worker import process_once


def detail_workbook(header_rows=3, title_rows=2):
    book = Workbook()
    book.active.title = "封面"
    book.active["A1"] = "测试报告"
    book.create_sheet("目录")["A1"] = "领域列表"
    sheet = book.create_sheet("领域1")
    sheet["A1"] = "测试总结"
    sheet["J9"] = "总结中的值不参与审查"
    sheet["A14"] = "测试详情"
    sheet.merge_cells(start_row=14, end_row=13 + title_rows, start_column=1, end_column=3)
    top = 14 + title_rows
    bottom = top + header_rows - 1
    for col, name in enumerate(["测试对象", *COLUMNS[:7]], start=1):
        sheet.cell(top, col, name)
        sheet.merge_cells(start_row=top, end_row=bottom, start_column=col, end_column=col)
    sheet.cell(top, 9, "第一轮")
    sheet.merge_cells(start_row=top, end_row=bottom - 1, start_column=9, end_column=12)
    for col, name in enumerate(COLUMNS[7:], start=9):
        sheet.cell(bottom, col, name)
    sheet.cell(bottom + 1, 4, "供应商名称")
    sheet.merge_cells(start_row=bottom + 1, end_row=bottom + 1, start_column=4, end_column=8)
    sheet.cell(bottom + 1, 9, "/")
    sheet.merge_cells(start_row=bottom + 1, end_row=bottom + 1, start_column=9, end_column=12)
    first = bottom + 2
    sheet.cell(first, 2, "TC-01")
    sheet.cell(first, 1, "设备A")
    for col in (1, 2):
        sheet.merge_cells(start_row=first, end_row=first + 1, start_column=col, end_column=col)
    sheet.cell(first, 10, 0)
    sheet.cell(first + 1, 10, "正常")
    sheet.cell(first + 1, 12, "附件占位")
    sheet.cell(first + 2, 10, "  ")
    sheet.cell(first + 2, 12, "https://intranet.example/skip.jpg")
    sheet.cell(first + 3, 10, "通过")
    sheet.cell(first + 3, 12, "查看附件").hyperlink = "https://intranet.example/ok.jpg"
    return book, sheet


def serialize(book):
    stream = BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def test_detail_three_row_header_and_value_selection():
    book, _ = detail_workbook()
    rows = parse_excel(serialize(book))
    assert [r["row_number"] for r in rows] == [20, 21, 23]
    assert all(r["domain"] == "领域1" for r in rows)
    assert rows[0]["fields"]["值"] == "0"
    assert rows[0]["fields"]["附件链接"] == ""
    assert rows[1]["fields"]["用例编号"] == "TC-01"
    assert rows[1]["fields"]["测试对象"] == "设备A"
    assert rows[1]["fields"]["附件链接"] == "附件占位"
    assert rows[2]["fields"]["附件链接"] == "https://intranet.example/ok.jpg"


def test_three_row_header_accepts_display_whitespace_without_changing_data(client, headers):
    book, sheet = detail_workbook()
    sheet.title = "USB接口"
    sheet["A14"] = " 测试\n详情 "
    for coord, value in {"A16": "测试\n对象", "B16": "用例 编号", "C16": "用例\u00a0名称", "D16": "测试\u200b小项",
                         "E16": "指标\u3000名称", "F16": "测试\t条件", "G16": "判据\n门限", "H16": " 单位\ufeff ",
                         "I16": "第 一\n轮", "I18": "门 限", "J18": " 值 ", "K18": "备\n注", "L18": "附件\n链接"}.items():
        sheet[coord] = value
    sheet["D20"] = "业务 内容\n保持原样"
    content = serialize(book)
    rows = parse_excel(content)
    assert len(rows) == 3 and rows[0]["fields"]["测试小项"] == "业务 内容\n保持原样"
    h = headers()
    uploaded = client.post('/api/reports', headers=h, files={'file': ('usb.xlsx', content)})
    assert uploaded.status_code == 201, uploaded.text
    run = client.post(f'/api/reports/{uploaded.json()["id"]}/runs', headers=h).json()
    assert process_once()
    response = client.get(f'/api/runs/{run["id"]}/export', headers=h)
    assert response.status_code == 200, response.text
    from openpyxl import load_workbook
    exported = load_workbook(BytesIO(response.content))
    assert exported['USB接口']['D20'].value == '业务 内容\n保持原样'
    assert exported['USB接口']['A16'].value == '测试\n对象'
    assert exported['USB接口']['O20'].value == '缺少附件'
    exported.close()


@pytest.mark.parametrize('error,expected', [('merge', 'A15:A16'), ('field', '单位'), ('round', 'I16:L16'), ('stray', 'N18')])
def test_header_errors_identify_the_actual_field_or_range(error, expected):
    book, sheet = detail_workbook(header_rows=2, title_rows=1) if error == 'merge' else detail_workbook()
    if error == 'field':
        sheet['H16'] = '其他字段'
    elif error == 'round':
        sheet.unmerge_cells('I16:L17'); sheet.merge_cells('I16:L16')
    elif error == 'stray':
        sheet['N18'] = '值'
    with pytest.raises(ValueError) as exc:
        parse_excel(serialize(book))
    assert expected in str(exc.value)


def test_obsolete_two_row_detail_header_is_rejected():
    book, _ = detail_workbook(header_rows=2, title_rows=1)
    with pytest.raises(ValueError, match="三行表头"):
        parse_excel(serialize(book))


def test_three_row_header_rejects_duplicate_value_columns():
    book, sheet = detail_workbook(header_rows=3, title_rows=2)
    sheet["N18"] = "值"
    with pytest.raises(ValueError, match="完整、唯一"):
        parse_excel(serialize(book))


def test_empty_value_skips_other_formulas_but_selected_formula_is_rejected():
    book, sheet = detail_workbook()
    sheet["D22"] = "=1+1"
    assert len(parse_excel(serialize(book))) == 3
    book, sheet = detail_workbook()
    sheet["J20"] = "=1+1"
    with pytest.raises(ValueError, match="公式转成值"):
        parse_excel(serialize(book))


def test_ambiguous_rounds_fail_instead_of_silently_omitting_rows():
    book, sheet = detail_workbook()
    sheet["N18"] = "值"
    with pytest.raises(ValueError, match="完整、唯一"):
        parse_excel(serialize(book))


def test_detail_upload_edit_review_and_export(client, headers):
    book, _ = detail_workbook()
    first = 20
    h = headers()
    response = client.post("/api/reports", headers=h,
                           files={"file": ("detail.xlsx", serialize(book))})
    assert response.status_code == 201, response.text
    report = response.json()
    items = client.get(f'/api/reports/{report["id"]}/items', headers=h).json()
    item = next(i for i in items if i["row_number"] == first)
    edit = {"version": 1, "fields": item["current"], "reason": "修订文字项"}
    assert client.patch(f'/api/items/{item["id"]}', headers=h, json=edit).status_code == 409
    edit["fields"] = {**edit["fields"], "值": " "}
    edit["version"] = 2
    assert client.patch(f'/api/items/{item["id"]}', headers=h, json=edit).status_code == 422
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    assert process_once()
    results = client.get(f'/api/runs/{run["id"]}/results', headers=h).json()
    assert len(results) == 3
    assert results[0]["explanation"] == "缺少附件"
    assert not results[0]["pending_review"]
    assert all(r["status"] == "missing_keyword" for r in results[1:])
    from openpyxl import load_workbook
    exported = client.get(f'/api/runs/{run["id"]}/export', headers=h)
    with BytesIO(exported.content) as stream:
        out = load_workbook(stream)
        assert out.sheetnames[:3] == ["封面", "目录", "领域1"]
        assert out["领域1"].cell(first, 10).value == 0
        assert out.sheetnames == ["封面", "目录", "领域1"]
        assert out["领域1"]["M16"].value == "AI审查"
        assert out["领域1"]["O20"].value == "缺少附件"
        assert out["领域1"]["O21"].value == "缺少关键词"
        out.close()
