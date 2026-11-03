from io import BytesIO
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook
from openpyxl.styles import PatternFill
from sqlalchemy import select

from app.db import Session
from app.excel import parse_excel, export_excel
from app.models import Item, ItemSource
from app.schemas import COLUMNS, ReviewVerdict
from app.worker import process_once
from test_excel import detail_workbook, serialize


def two_group_workbook():
    book, sheet = detail_workbook(title_rows=1)
    sheet.unmerge_cells("I15:L16")
    sheet.merge_cells("I15:P16")
    for col, name in enumerate(COLUMNS[7:], 13):
        sheet.cell(17, col, name)
    sheet["I18"] = "A"
    sheet.merge_cells("M18:P18")
    sheet["M18"] = "B"
    sheet.merge_cells("I21:L21")
    sheet["I21"] = "K"
    sheet.merge_cells("M21:P21")
    sheet["M21"] = "J"
    for row, left, right in ((19, 0, 21), (20, "left-2", "right-2"), (22, "", 0), (23, "left-4", "")):
        sheet.cell(row, 9, "left-threshold")
        sheet.cell(row, 10, left)
        sheet.cell(row, 11, "left-note")
        sheet.cell(row, 13, "right-threshold")
        sheet.cell(row, 14, right)
        sheet.cell(row, 15, "right-note")
    sheet["P19"] = "右侧附件"
    sheet["P19"].hyperlink = "https://intranet/right.jpg"
    sheet["P20"] = '=HYPERLINK("https://intranet/second.gif","图片")'
    sheet["I19"].fill = PatternFill("solid", fgColor="DDEEFF")
    sheet.column_dimensions["I"].width = 19
    sheet.column_dimensions["M"].width = 23
    sheet.row_dimensions[19].height = 31
    sheet.print_area = "A1:P23"
    return book, sheet


def test_two_groups_are_independent_and_preserve_band_labels():
    book, _ = two_group_workbook()
    rows = parse_excel(serialize(book))
    assert [(r["row_number"], r["source_group"], r["source_label"]) for r in rows] == [
        (19, 0, "A"), (19, 1, "B"), (20, 0, "A"), (20, 1, "B"), (22, 1, "J"), (23, 0, "K")]
    assert rows[0]["fields"]["值"] == rows[4]["fields"]["值"] == "0"
    assert rows[0]["fields"]["门限"] == "left-threshold"
    assert rows[1]["fields"]["门限"] == "right-threshold"
    assert rows[0]["fields"]["备注"] == "left-note"
    assert rows[1]["fields"]["备注"] == "right-note"
    assert rows[1]["fields"]["附件链接"] == "https://intranet/right.jpg"
    assert rows[3]["fields"]["附件链接"] == "https://intranet/second.gif"
    assert rows[2]["fields"]["测试对象"] == "设备A"
    assert all(set(r["fields"]) == {"测试对象", *COLUMNS} for r in rows)


@pytest.mark.parametrize('title', ['USB功能测试', '实测数据', '第二阶段', '', None])
def test_group_title_text_is_unrestricted_in_upload_and_export(client, headers, title):
    h = headers()
    book, sheet = two_group_workbook()
    sheet.title = 'USB接口'
    sheet['I15'] = title
    # A later section literally called 第一轮 must not replace the leftmost input section.
    sheet.merge_cells('Q15:T16')
    sheet['Q15'] = '第一轮'
    for col, name in enumerate(COLUMNS[7:], 17):
        sheet.cell(17, col, name)
    sheet['R19'] = '不应读取右侧其他区'
    content = serialize(book)
    parsed = parse_excel(content)
    assert len(parsed) == 6
    assert [r['fields']['值'] for r in parsed[:2]] == ['0', '21']
    uploaded = client.post('/api/reports', headers=h, files={'file': ('usb.xlsx', content)})
    assert uploaded.status_code == 201, uploaded.text
    run = client.post(f'/api/reports/{uploaded.json()["id"]}/runs', headers=h).json()
    assert process_once()
    response = client.get(f'/api/runs/{run["id"]}/export', headers=h)
    assert response.status_code == 200, response.text
    output = load_workbook(BytesIO(response.content))
    assert output['USB接口']['I15'].value == (title or None)
    assert output['USB接口']['Q15'].value == 'AI审查'
    assert output['USB接口']['Y15'].value == '第一轮'
    assert output['USB接口']['Z19'].value == '不应读取右侧其他区'
    output.close()


def test_formula_in_second_group_is_not_silently_ignored():
    book, sheet = two_group_workbook()
    sheet["N22"] = "=1+1"
    with pytest.raises(ValueError, match="第 2 组.*公式转成值"):
        parse_excel(serialize(book))


def test_export_restores_group_positions_styles_merges_and_blanks():
    book, _ = two_group_workbook()
    original = serialize(book)
    rows = parse_excel(original)
    results, measurements, sources, notes = [], {}, {}, {}
    for index, row in enumerate(rows):
        key = str(index)
        result = SimpleNamespace(id=key, item_id=key, domain=row["domain"], row_number=row["row_number"],
                                 status="needs_review", explanation=f"结果-{index}")
        results.append(result)
        measurements[key] = SimpleNamespace(ai_threshold=f"threshold-{index}", ai_value=f"value-{index}")
        notes[key] = "=备注" if index == 1 else f"备注-{index}"
        sources[key] = row["source_group"]
    # Export order must not determine where a same-row sibling lands.
    output = export_excel(original, SimpleNamespace(id="test"), list(reversed(results)), measurements, notes, sources)
    before, after = load_workbook(BytesIO(original)), load_workbook(BytesIO(output))
    sheet, source = after["领域1"], before["领域1"]
    assert after.sheetnames == before.sheetnames
    for row in source:
        for cell in row:
            target = sheet[cell.coordinate]
            assert target.value == cell.value
            assert list(target._style or [0] * 9) == list(cell._style or [0] * 9)
            assert (target.hyperlink.target if target.hyperlink else None) == (cell.hyperlink.target if cell.hyperlink else None)
    assert {str(a) for a in source.merged_cells.ranges} <= {str(a) for a in sheet.merged_cells.ranges}
    assert {"Q15:X16", "Q18:T18", "U18:X18", "Q21:T21", "U21:X21"} <= {str(a) for a in sheet.merged_cells.ranges}
    assert sheet["Q15"].value == "AI审查"
    assert [sheet.cell(17, col).value for col in range(17, 25)] == ["门限", "值", "结果", "备注"] * 2
    assert [sheet[cell].value for cell in ("Q18", "U18", "Q21", "U21")] == ["A", "B", "K", "J"]
    assert sheet["S19"].value == "结果-0" and sheet["W19"].value == "结果-1"
    assert sheet["W19"].alignment.shrinkToFit
    assert sheet["U19"].value == "threshold-1" and sheet["X19"].value == "'=备注"
    assert sheet["Q19"]._style == source["I19"]._style
    assert sheet.column_dimensions["Q"].width == 19
    assert sheet.column_dimensions["U"].width == 23
    assert sheet.row_dimensions[19].height == 31
    assert "$X$23" in sheet.print_area
    assert all(sheet.cell(22, col).value is None for col in range(17, 21))
    assert all(sheet.cell(23, col).value is None for col in range(21, 25))
    before.close(); after.close()


def test_upload_review_export_and_missing_mode_do_not_mix_siblings(client, headers, monkeypatch):
    h = headers()
    book, _ = two_group_workbook()
    uploaded = client.post('/api/reports', headers=h, files={"file": ("groups.xlsx", serialize(book))})
    assert uploaded.status_code == 201, uploaded.text
    report = uploaded.json()
    endpoint = f'/api/reports/{report["id"]}'
    items = client.get(endpoint + '/items', headers=h).json()
    assert report["item_count"] == len(items) == 6
    assert [(i["row_number"], i["source_group"]) for i in items] == [(19, 0), (19, 1), (20, 0), (20, 1), (22, 1), (23, 0)]
    assert [i["source_label"] for i in items] == ["A", "B", "A", "B", "J", "K"]
    first, sibling = items[:2]
    note = f'/api/items/{sibling["id"]}/note'
    assert client.patch(note, headers=h, json={"note": "只给B组的备注", "version": 0}).status_code == 200
    calls = []
    def review(fields, keywords, username):
        calls.append((fields["门限"], fields["值"], fields["附件链接"]))
        return ReviewVerdict(status="passed", explanation="通过", ai_threshold=fields["门限"], ai_value=fields["值"]), {}
    monkeypatch.setattr("app.worker.review", review)
    run = client.post(endpoint + '/runs', headers=h).json()
    assert process_once()
    rows = client.get(f'/api/runs/{run["id"]}/results', headers=h).json()
    assert len(calls) == 6
    assert calls[0][:2] == ("left-threshold", "0") and calls[1][:2] == ("right-threshold", "21")
    assert [r["source_label"] for r in rows] == ["A", "B", "A", "B", "J", "K"]
    output = client.get(f'/api/runs/{run["id"]}/export', headers=h)
    exported = load_workbook(BytesIO(output.content))
    assert exported["领域1"]["Q19"].value == "left-threshold"
    assert exported["领域1"]["U19"].value == "right-threshold"
    assert exported["领域1"]["X19"].value == "只给B组的备注"
    assert exported["领域1"]["T19"].value is None
    exported.close()
    current = client.get(endpoint + '/items', headers=h).json()
    state = next(i for i in current if i["id"] == sibling["id"])
    assert client.patch(f'/api/items/{sibling["id"]}/review-state', headers=h, json={"state": "pending",
        "revision": state["review_revision"], "reviewed_revision": state["reviewed_revision"]}).status_code == 200
    supplement = client.post(endpoint + '/runs', headers=h, json={"selection_mode": "missing"}).json()
    assert supplement["total"] == 1
    assert process_once()
    single = client.get(f'/api/runs/{supplement["id"]}/results', headers=h).json()
    assert len(single) == 1 and single[0]["item_id"] == sibling["id"] != first["id"]
    assert client.delete(endpoint, headers=h).status_code == 200
    with Session() as db:
        assert db.scalar(select(ItemSource.item_id)) is None
        assert db.scalar(select(Item.id)) is None
