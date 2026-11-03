import pytest
from sqlalchemy import select

from app.config import settings
from app.db import Session
from app.models import ChangeRequest, Item, ItemNote, ItemReviewState, ItemSource, Report, Result, ResultMeasurements, ResultWork, Run, RunControl
from app.schemas import ReviewVerdict
from app.worker import process_once
from test_workflow import upload, change


def finished(client, h, report):
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    assert process_once()
    return run


def test_delete_old_batch_preserves_current_results_and_delete_current_restores_pending(client, headers, monkeypatch):
    owner = headers()
    report, other_report = upload(client, owner), upload(client, owner)
    monkeypatch.setattr('app.worker.review', lambda *args: (ReviewVerdict(status='passed', explanation='通过'), {}))
    old, current = finished(client, owner, report), finished(client, owner, report)
    other = finished(client, owner, other_report)
    endpoint = f'/api/reports/{report["id"]}'
    before = client.get(endpoint + '/items', headers=owner).json()
    assert all(not i['pending_review'] for i in before)
    item_id = before[0]['id']
    assert client.patch(f'/api/items/{item_id}/note', headers=owner, json={'version': 0, 'note': '保留备注'}).status_code == 200
    old_results = client.get(f'/api/runs/{old["id"]}/results', headers=owner).json()
    request_response = client.post('/api/keyword-requests', headers=owner,
        json={**change(domain=old_results[0]['domain']), 'result_id': old_results[0]['id']})
    assert request_response.status_code == 201, request_response.text
    request = request_response.json()
    assert client.delete(f'/api/runs/{old["id"]}', headers=owner).status_code == 200
    assert client.get(f'/api/runs/{old["id"]}', headers=owner).status_code == 404
    after_old = client.get(endpoint + '/items', headers=owner).json()
    assert all(not i['pending_review'] for i in after_old)
    assert [i['review']['id'] for i in after_old] == [i['review']['id'] for i in before]
    with Session() as db:
        assert db.get(ChangeRequest, request['id']).result_id is None
        assert db.get(RunControl, old['id']) is None
        for row in old_results:
            assert db.get(Result, row['id']) is None
            assert db.get(ResultWork, row['id']) is None
            assert db.get(ResultMeasurements, row['id']) is None
    current_results = client.get(f'/api/runs/{current["id"]}/results', headers=owner).json()
    assert client.delete(f'/api/runs/{current["id"]}', headers=owner).status_code == 200
    after = client.get(endpoint + '/items', headers=owner).json()
    assert all(i['pending_review'] and i['review'] is None for i in after)
    assert next(i for i in after if i['id'] == item_id)['note'] == '保留备注'
    assert client.get(endpoint + '/runs', headers=owner).json() == []
    assert client.get(f'/api/runs/{other["id"]}', headers=owner).status_code == 200
    assert (settings.data_dir / (report['id'] + '.xlsx')).exists()
    with Session() as db:
        assert db.get(Report, report['id'])
        assert db.get(Item, item_id) and db.get(ItemSource, item_id) and db.get(ItemNote, item_id)
        assert db.get(ItemReviewState, item_id).source_result_id is None
        assert db.get(RunControl, current['id']) is None
        for row in current_results:
            assert db.get(Result, row['id']) is None
            assert db.get(ResultWork, row['id']) is None
            assert db.get(ResultMeasurements, row['id']) is None
    outdated = before[0]
    assert client.patch(f'/api/items/{outdated["id"]}/review-state', headers=owner, json={
        'state': 'reviewed', 'revision': outdated['review_revision'], 'reviewed_revision': outdated['reviewed_revision']}).status_code == 409
    fresh = client.post(endpoint + '/runs', headers=owner, json={'selection_mode': 'missing'}).json()
    assert fresh['total'] == report['item_count']
    activities = client.get('/api/audit', headers=owner).json()
    assert len([a for a in activities if a['action'] == 'run.clear' and a['action_label'] == '删除审查批次']) == 2


@pytest.mark.parametrize('state', ['queued', 'running', 'pausing', 'paused'])
def test_batch_deletion_is_blocked_during_any_active_run(client, headers, state):
    h = headers()
    report = upload(client, h)
    old = finished(client, h, report)
    active = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    with Session() as db:
        db.get(Run, active['id']).state = state
        db.commit()
    for run in (old, active):
        response = client.delete(f'/api/runs/{run["id"]}', headers=h)
        assert response.status_code == 409
        assert client.get(f'/api/runs/{run["id"]}', headers=h).status_code == 200


def test_only_owner_can_delete_batch_even_when_admin_can_view(client, headers):
    owner, admin = headers(), headers('admin')
    report = upload(client, owner)
    run = finished(client, owner, report)
    endpoint = f'/api/runs/{run["id"]}'
    assert client.get(endpoint, headers=admin).status_code == 200
    assert client.delete(endpoint, headers=admin).status_code == 404
    assert client.delete(endpoint).status_code == 401
    assert client.delete(endpoint, headers=owner).status_code == 200
    assert client.delete(endpoint, headers=owner).status_code == 404
