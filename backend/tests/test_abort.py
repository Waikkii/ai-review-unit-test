import pytest
from sqlalchemy import select, func

from app.auth_sessions import SessionAuthFailure
from app.db import Session
from app.models import Run, Result, ResultMeasurements, RunControl, ItemReviewState
from app.schemas import ReviewVerdict
from app.worker import process_once
from test_workflow import upload


@pytest.mark.parametrize('state', ['queued', 'running', 'pausing', 'paused', 'blocked'])
def test_abort_fences_batch_and_allows_immediate_deletion(client, headers, state):
    h = headers()
    report = upload(client, h)
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    with Session() as db:
        stored = db.get(Run, run['id'])
        stored.state, stored.lease_owner = state, 'old-worker'
        db.commit()
    endpoint = f'/api/runs/{run["id"]}'
    response = client.post(endpoint + '/abort', headers=h)
    assert response.status_code == 200, response.text
    assert response.json()['state'] == 'aborted'
    assert not process_once()
    assert client.post(endpoint + '/resume', headers=h).status_code == 409
    assert client.post(endpoint + '/abort', headers=h).status_code == 200
    with Session() as db:
        stored = db.get(Run, run['id'])
        assert stored.lease_owner is None and stored.lease_until is None
    assert client.delete(endpoint, headers=h).status_code == 200
    assert client.get(endpoint, headers=h).status_code == 404


@pytest.mark.parametrize('delete_target', [None, 'batch', 'report'])
@pytest.mark.parametrize('outcome', ['passed', 'auth_error', 'error'])
def test_inflight_call_cannot_write_or_recreate_aborted_deleted_data(client, headers, monkeypatch, delete_target, outcome):
    h = headers()
    report = upload(client, h)
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    endpoint = f'/api/runs/{run["id"]}'
    calls = []
    def review(*args):
        calls.append(args)
        assert client.post(endpoint + '/abort', headers=h).json()['state'] == 'aborted'
        if delete_target:
            path = endpoint if delete_target == 'batch' else f'/api/reports/{report["id"]}'
            assert client.delete(path, headers=h).status_code == 200
        if outcome == 'auth_error':
            raise SessionAuthFailure()
        if outcome == 'error':
            raise RuntimeError('inflight error')
        return ReviewVerdict(status='passed', explanation='should be discarded'), {}
    monkeypatch.setattr('app.worker.review', review)
    assert process_once()
    assert len(calls) == 1 and not process_once()
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(ResultMeasurements)) == 0
        if delete_target:
            assert db.get(Run, run['id']) is None
            assert db.get(RunControl, run['id']) is None
            assert not db.scalars(select(Result).where(Result.run_id == run['id'])).all()
        else:
            assert db.get(Run, run['id']).state == 'aborted'
            assert db.get(Run, run['id']).completed == 0
            assert all(r.status == 'pending' for r in db.scalars(select(Result)))
        assert all(s.reviewed_revision == 0 and s.source_result_id is None for s in db.scalars(select(ItemReviewState)))


def test_abort_preserves_completed_items_and_restart_keeps_selected_mode(client, headers, monkeypatch):
    h = headers()
    report = upload(client, h)
    endpoint = f'/api/reports/{report["id"]}'
    run = client.post(endpoint + '/runs', headers=h, json={'selection_mode': 'missing'}).json()
    calls = []
    def review(*args):
        calls.append(args)
        if len(calls) == 2:
            assert client.post(f'/api/runs/{run["id"]}/abort', headers=h).status_code == 200
        return ReviewVerdict(status='passed', explanation='通过'), {}
    monkeypatch.setattr('app.worker.review', review)
    assert process_once()
    detail = client.get(f'/api/runs/{run["id"]}', headers=h).json()
    assert detail['state'] == 'aborted' and detail['completed'] == 1
    rows = client.get(f'/api/runs/{run["id"]}/results', headers=h).json()
    assert [r['status'] for r in rows] == ['passed', 'pending', 'pending']
    restarted = client.post(f'/api/runs/{run["id"]}/restart', headers=h).json()
    assert restarted['selection_mode'] == 'missing' and restarted['total'] == 2
    assert client.get(f'/api/runs/{run["id"]}', headers=h).json()['state'] == 'aborted'


def test_abort_is_owner_only_and_rejects_finished_batch(client, headers):
    owner, admin = headers(), headers('admin')
    report = upload(client, owner)
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=owner).json()
    endpoint = f'/api/runs/{run["id"]}/abort'
    assert client.post(endpoint, headers=admin).status_code == 404
    assert client.post(endpoint).status_code == 401
    assert process_once()
    assert client.post(endpoint, headers=owner).status_code == 409
