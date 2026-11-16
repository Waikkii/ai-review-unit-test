import copy
import json
from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import select, func

from app.config import settings
from app.db import Session
from app.models import Run, RunItemReviewState, ItemNote, Result, ResultWork
from app.schemas import ReviewVerdict
from app.worker import process_once
from test_excel import serialize
from test_excel_groups import multi_round_workbook
from test_workflow import upload


def export_data(client, token, run_id):
    response = client.get(f'/api/runs/{run_id}/archive', headers=token)
    assert response.status_code == 200, response.text
    return response.json()


def import_data(client, token, report_id, data):
    return client.post(f'/api/reports/{report_id}/import-results', headers=token,
                       files={'file': ('results.review.json', json.dumps(data, ensure_ascii=False).encode())})


def passed(*args):
    return ReviewVerdict(status='passed', explanation='通过', ai_threshold='', ai_value='0'), {'saved': ['原始返回', 0]}


def completed(client, token, monkeypatch):
    report = upload(client, token)
    monkeypatch.setattr('app.worker.review', passed)
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=token).json()
    assert process_once()
    return report, run, export_data(client, token, run['id'])


@pytest.mark.parametrize('new_report', [False, True])
def test_roundtrip_multiround_history_is_complete_independent_and_supplementable(client, headers, monkeypatch, new_report):
    token = headers()
    book, _ = multi_round_workbook()
    content = serialize(book)
    source = client.post('/api/reports', headers=token, files={'file': ('source.xlsx', content)}).json()
    monkeypatch.setattr('app.worker.review', passed)
    a = client.post(f'/api/reports/{source["id"]}/runs', headers=token).json()
    assert process_once()
    rows = client.get(f'/api/runs/{a["id"]}/results', headers=token).json()
    first = rows[0]
    assert client.patch(f'/api/items/{first["item_id"]}/note', headers=token,
                        json={'note': '=用户备注，独立数据组', 'version': 0}).status_code == 200
    assert client.patch(f'/api/items/{first["item_id"]}/review-state', headers=token,
        json={'state': 'pending', 'revision': first['review_revision'], 'reviewed_revision': first['reviewed_revision'], 'run_id': a['id']}).status_code == 200
    b = client.post(f'/api/reports/{source["id"]}/runs', headers=token,
                    json={'selection_mode': 'missing', 'base_run_id': a['id']}).json()
    assert b['total'] == 1
    monkeypatch.setattr('app.worker.review', lambda *args: (ReviewVerdict(status='missing_keyword', explanation='缺少关键词'), {'cause': 'missing'}))
    assert process_once()
    with Session() as db:
        state = db.get(RunItemReviewState, (b['id'], rows[1]['item_id']))
        state.pending, state.stale = True, True
        db.commit()
    data = export_data(client, token, b['id'])
    assert len(data['items']) == source['item_count'] == 14
    assert sum(item['pending'] for item in data['items']) == 2
    assert {item['source_group'] for item in data['items']} == set(range(6))
    target = source
    if new_report:
        target = client.post('/api/reports', headers=token, files={'file': ('renamed.xlsx', content)}).json()
    old_history = client.get(f'/api/runs/{b["id"]}/results?include_reviewed=true', headers=token).json()
    response = import_data(client, token, target['id'], data)
    assert response.status_code == 201, response.text
    imported = response.json()
    assert imported['mode'] == 'imported' and imported['pending_item_count'] == 2
    assert export_data(client, token, imported['id'])['items'] == data['items']
    def visible_history(values):
        return [{key: value for key, value in row.items() if key not in {'review_revision', 'reviewed_revision'}} for row in values]
    assert visible_history(client.get(f'/api/runs/{b["id"]}/results?include_reviewed=true', headers=token).json()) == visible_history(old_history)
    page = client.get(f'/api/reports/{target["id"]}/table-view', headers=token,
                      params={'run_id': imported['id'], 'filters': json.dumps({'state': ['待审查']})}).json()
    assert page['total'] == 2
    if new_report:
        assert client.delete(f'/api/reports/{source["id"]}', headers=token).status_code == 200
    else:
        for history in (a, b):
            assert client.delete(f'/api/runs/{history["id"]}', headers=token).status_code == 200
    assert export_data(client, token, imported['id'])['items'] == data['items']
    excel = client.get(f'/api/runs/{imported["id"]}/export?include_reviewed=true', headers=token)
    assert excel.status_code == 200, excel.text
    book = load_workbook(BytesIO(excel.content))
    assert book['领域3']['T19'].value == "'=用户备注，独立数据组"
    assert book['领域3']['AX20'].value == '0'  # 最后一个轮次的 AI 值同样保留。
    book.close()
    monkeypatch.setattr('app.worker.review', passed)
    child = client.post(f'/api/reports/{target["id"]}/runs', headers=token,
                        json={'selection_mode': 'missing', 'base_run_id': imported['id']})
    assert child.status_code == 201, child.text
    assert child.json()['total'] == 2
    assert process_once()
    assert export_data(client, token, imported['id'])['items'] == data['items']
    full = export_data(client, token, child.json()['id'])
    assert len(full['items']) == 14 and not any(item['pending'] for item in full['items'])


@pytest.mark.parametrize('change', ['sha', 'missing', 'duplicate', 'group', 'fields', 'status', 'version', 'stale'])
def test_invalid_import_is_atomic(client, headers, monkeypatch, change):
    token = headers()
    report, run, original = completed(client, token, monkeypatch)
    data = copy.deepcopy(original)
    if change == 'sha': data['excel_sha256'] = '0' * 64
    elif change == 'missing': data['items'].pop()
    elif change == 'duplicate': data['items'][1] = data['items'][0]
    elif change == 'group': data['items'][0]['source_group'] = 999
    elif change == 'fields': data['items'][0]['original_fields']['值'] = 'changed'
    elif change == 'status': data['items'][0]['result']['status'] = 'invented'
    elif change == 'version': data['version'] = 999
    elif change == 'stale': data['items'][0]['stale'] = True
    response = import_data(client, token, report['id'], data)
    assert response.status_code == 422, response.text
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 1
    assert export_data(client, token, run['id'])['items'] == original['items']


def test_import_preserves_reviewed_without_result_and_applies_note_versions(client, headers, monkeypatch):
    token = headers()
    report, run, data = completed(client, token, monkeypatch)
    first = client.get(f'/api/runs/{run["id"]}/results', headers=token).json()[0]
    assert client.patch(f'/api/items/{first["item_id"]}/note', headers=token,
                        json={'note': 'old note', 'version': 0}).status_code == 200
    data['items'][0]['result'] = None
    data['items'][0]['pending'] = False
    data['items'][1]['pending'] = True
    response = import_data(client, token, report['id'], data)
    assert response.status_code == 201, response.text
    imported = response.json()
    assert export_data(client, token, imported['id'])['items'] == data['items']
    view = client.get(f'/api/runs/{imported["id"]}/results?include_reviewed=true', headers=token).json()
    assert len(view) == 3 and not view[0]['pending_review'] and view[0]['ai_value'] == ''
    with Session() as db:
        note = db.get(ItemNote, first['item_id'])
        assert note.note == '' and note.version == 2
    child = client.post(f'/api/reports/{report["id"]}/runs', headers=token,
                        json={'selection_mode': 'missing', 'base_run_id': imported['id']})
    assert child.status_code == 201 and child.json()['total'] == 1


def test_archive_access_and_active_task_restrictions(client, headers, monkeypatch):
    token, admin = headers(), headers('admin')
    report, run, data = completed(client, token, monkeypatch)
    assert client.get(f'/api/runs/{run["id"]}/archive', headers=admin).status_code == 200
    assert import_data(client, admin, report['id'], data).status_code == 404
    active = client.post(f'/api/reports/{report["id"]}/runs', headers=token).json()
    assert client.get(f'/api/runs/{active["id"]}/archive', headers=token).status_code == 409
    assert import_data(client, token, report['id'], data).status_code == 409
    assert client.get(f'/api/runs/{run["id"]}/archive', headers=token).status_code == 200
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 2


@pytest.mark.parametrize('content', [b'not json', b'{"format":1,"format":2}', b'{"value":NaN}'])
def test_malformed_json_is_rejected(client, headers, content):
    token = headers()
    report = upload(client, token)
    response = client.post(f'/api/reports/{report["id"]}/import-results', headers=token,
                           files={'file': ('results.json', content)})
    assert response.status_code == 422


def test_archive_file_size_limit(client, headers, monkeypatch):
    token = headers()
    report, run, data = completed(client, token, monkeypatch)
    monkeypatch.setattr(settings, 'max_upload_mb', 0)
    assert import_data(client, token, report['id'], data).status_code == 413
    assert client.get(f'/api/runs/{run["id"]}/archive', headers=token).status_code == 413
