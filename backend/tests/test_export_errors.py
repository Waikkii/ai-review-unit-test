from io import BytesIO
import logging

import pytest
from openpyxl import load_workbook

from app.config import settings
from app.worker import process_once
from test_excel import detail_workbook, serialize
from test_workflow import upload


@pytest.mark.parametrize('legacy_format', ['two_row_headers', 'wrong_round_merge'])
def test_export_rejects_old_or_nonstandard_headers(client, headers, legacy_format):
    h = headers()
    book, sheet = detail_workbook()
    sheet.title = "USB接口"
    original = serialize(book)
    report = client.post('/api/reports', headers=h, files={"file": ("usb.xlsx", original)}).json()
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    assert process_once()
    if legacy_format == 'two_row_headers':
        old, old_sheet = detail_workbook(header_rows=2, title_rows=1)
        old_sheet.title = 'USB接口'
    else:
        old = load_workbook(BytesIO(original))
        old["USB接口"].unmerge_cells("I16:L17")
        old["USB接口"].merge_cells("I16:L16")
    old.save(settings.data_dir / (report["id"] + '.xlsx'))
    old.close()
    response = client.get(f'/api/runs/{run["id"]}/export', headers=h)
    assert response.status_code == 422, response.text
    assert 'USB接口' in response.json()['detail'] and '三行表头' in response.json()['detail']


@pytest.mark.parametrize('failure,status,message', [
    (FileNotFoundError('source missing'), 404, '原始 Excel 文件不存在'),
    (ValueError('USB接口: 无法识别表头'), 422, 'USB接口: 无法识别表头'),
    (RuntimeError('renderer failure'), 500, '请联系管理员查看 API 日志'),
])
def test_export_failures_have_actionable_details_and_traceback(client, headers, monkeypatch, caplog, failure, status, message):
    h = headers()
    report = upload(client, h)
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    assert process_once()
    def fail(*args):
        raise failure
    monkeypatch.setattr('app.main.export_excel', fail)
    with caplog.at_level(logging.ERROR, logger='app.main'):
        response = client.get(f'/api/runs/{run["id"]}/export', headers=h)
    assert response.status_code == status
    assert message in response.json()['detail']
    assert any(run['id'] in record.message and record.exc_info for record in caplog.records)


def test_missing_original_reports_specific_error(client, headers):
    h = headers()
    report = upload(client, h)
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    assert process_once()
    (settings.data_dir / (report["id"] + '.xlsx')).unlink()
    response = client.get(f'/api/runs/{run["id"]}/export', headers=h)
    assert response.status_code == 404
    assert '恢复原文件' in response.json()['detail']


def test_export_preserves_unselected_sheet_without_requiring_detail_headers(client, headers):
    h = headers()
    book, _ = detail_workbook()
    book.create_sheet('USB接口')['A40'] = '本页不含测试项，仅留说明'
    report = client.post('/api/reports', headers=h, files={'file': ('notes.xlsx', serialize(book))}).json()
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    assert process_once()
    response = client.get(f'/api/runs/{run["id"]}/export', headers=h)
    assert response.status_code == 200, response.text
    exported = load_workbook(BytesIO(response.content))
    assert exported['USB接口']['A40'].value == '本页不含测试项，仅留说明'
    assert exported['USB接口'].max_column == 1
    assert exported['领域1']['O20'].value == '缺少附件'
    exported.close()
