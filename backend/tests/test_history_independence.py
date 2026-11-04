from io import BytesIO
import pytest
from openpyxl import load_workbook
from sqlalchemy import delete
from app.db import Session
from app.models import RunItemReviewState
from app.schemas import ReviewVerdict
from app.worker import process_once
from test_workflow import upload


def view(client, h, run):
    rows = client.get(f'/api/runs/{run["id"]}/results?include_reviewed=true', headers=h)
    assert rows.status_code == 200, rows.text
    return {r['item_id']: {key:r[key] for key in ('status','ai_threshold','ai_value','explanation','result_label','pending_review','stale')} for r in rows.json()}


@pytest.mark.parametrize('deleted', ['parent', 'child'])
@pytest.mark.parametrize('mode', ['full', 'missing'])
def test_each_history_survives_deleting_the_other(client, headers, monkeypatch, deleted, mode):
    h = headers()
    report = upload(client, h)
    monkeypatch.setattr('app.worker.review', lambda *args: (ReviewVerdict(status='passed', explanation='通过', ai_threshold='A门限', ai_value='A值'), {}))
    a = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    assert process_once()
    if mode == 'missing':
        item = client.get(f'/api/runs/{a["id"]}/results', headers=h).json()[0]
        changed = client.patch(f'/api/items/{item["item_id"]}/review-state', headers=h,
            json={'state':'pending','revision':item['review_revision'],'reviewed_revision':item['reviewed_revision'],'run_id':a['id']})
        assert changed.status_code == 200
    original_a = view(client, h, a)
    response = client.post(f'/api/runs/{a["id"]}/restart', headers=h, json={'selection_mode':mode})
    assert response.status_code == 200, response.text
    b = response.json()
    monkeypatch.setattr('app.worker.review', lambda *args: (ReviewVerdict(status='passed', explanation='通过', ai_threshold='B门限', ai_value='B值'), {}))
    assert process_once()
    original_b = view(client, h, b)
    assert view(client, h, a) == original_a
    victim, survivor, expected = (a, b, original_b) if deleted == 'parent' else (b, a, original_a)
    assert client.delete(f'/api/runs/{victim["id"]}', headers=h).status_code == 200
    assert view(client, h, survivor) == expected
    page = client.get(f'/api/reports/{report["id"]}/table-view', headers=h, params={'run_id':survivor['id']}).json()
    assert page['total'] == 3
    export = client.get(f'/api/runs/{survivor["id"]}/export?include_reviewed=true', headers=h)
    assert export.status_code == 200, export.text
    book = load_workbook(BytesIO(export.content))
    values = [c.value for s in book for row in s for c in row]
    assert ('A值' if survivor['id'] == a['id'] else 'B值') in values
    book.close()
    # The survivor remains a usable baseline even after its sibling is removed.
    row = client.get(f'/api/runs/{survivor["id"]}/results?include_reviewed=true', headers=h).json()[-1]
    assert client.patch(f'/api/items/{row["item_id"]}/review-state', headers=h,
        json={'state':'pending','revision':row['review_revision'],'reviewed_revision':row['reviewed_revision'],'run_id':survivor['id']}).status_code == 200
    assert client.post(f'/api/runs/{survivor["id"]}/restart', headers=h, json={'selection_mode':'missing'}).status_code == 200


def test_upgrade_restores_results_hidden_by_deleted_shared_state(client, headers, monkeypatch):
    h = headers()
    report = upload(client, h)
    monkeypatch.setattr('app.worker.review', lambda *args: (ReviewVerdict(status='passed', explanation='通过', ai_value='A保留值'), {}))
    a = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    assert process_once()
    b = client.post(f'/api/runs/{a["id"]}/restart', headers=h, json={'selection_mode':'full'}).json()
    assert process_once()
    assert client.delete(f'/api/runs/{b["id"]}', headers=h).status_code == 200
    with Session() as db:
        db.execute(delete(RunItemReviewState))
        db.commit()
    from app.migrate import main
    main(); main()
    assert all(not r['pending_review'] and r['ai_value'] == 'A保留值' for r in view(client, h, a).values())
