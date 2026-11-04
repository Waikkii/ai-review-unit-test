from io import BytesIO
import pytest
from openpyxl import load_workbook
from sqlalchemy import select, func, delete
from app.db import Session
from app.models import Result, ResultMeasurements, ResultWork, RunRetainedItem, ItemReviewState
from app.schemas import ReviewVerdict
from app.worker import process_once
from test_workflow import upload


def prepare(client, h, monkeypatch):
    report = upload(client, h)
    monkeypatch.setattr('app.worker.review', lambda *args: (ReviewVerdict(status='passed', explanation='通过', ai_threshold='旧门限', ai_value='旧值'), {'original':True}))
    first = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()
    assert process_once()
    items = client.get(f'/api/reports/{report["id"]}/items', headers=h).json()
    item = items[-1]
    client.patch(f'/api/items/{item["id"]}/review-state', headers=h, json={'state':'pending','revision':item['review_revision'],'reviewed_revision':item['reviewed_revision']})
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=h, json={'selection_mode':'missing'}).json()
    return report, first, run


def test_display_owns_inherited_results_without_executing_them(client, headers, monkeypatch):
    h = headers()
    report, first, run = prepare(client, h, monkeypatch)
    url = f'/api/runs/{run["id"]}/results'
    assert run['total'] == 1
    assert len(client.get(url, headers=h).json()) == 1
    view = client.get(url, headers=h, params={'include_reviewed':True,'include_total':True,'limit':2}).json()
    assert view['total'] == 3 and len(view['items']) == 2
    assert len(client.get(f'/api/runs/{run["id"]}/result-options?include_reviewed=true', headers=h).json()) == 2
    old_ids = {r['id'] for r in client.get(f'/api/runs/{first["id"]}/results',headers=h).json()}
    before = client.get(url+'?include_reviewed=true',headers=h).json()
    kept = [r for r in before if not r['pending_review']]
    assert len(kept) == 2 and all(r['id'] not in old_ids and r['ai_value']=='旧值' for r in kept)
    calls=[]
    def review(fields,*args):
        calls.append(fields)
        return ReviewVerdict(status='passed',explanation='通过',ai_threshold='新门限',ai_value='新值'), {}
    monkeypatch.setattr('app.worker.review',review)
    assert process_once() and len(calls)==1
    after=client.get(url+'?include_reviewed=true',headers=h).json()
    assert len(after)==3 and all(not r['pending_review'] for r in after)
    assert len([r for r in after if r['ai_value']=='旧值'])==2
    assert len([r for r in after if r['ai_value']=='新值'])==1
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(Result).where(Result.run_id==run['id']))==3
        assert db.scalar(select(func.count()).select_from(RunRetainedItem).where(RunRetainedItem.run_id==run['id']))==2
    assert client.get(url,headers=h,params={'include_reviewed':True,'domain':'协议','include_total':True}).json()['total']==1
    output=client.get(f'/api/runs/{run["id"]}/export?include_reviewed=true',headers=h)
    assert output.status_code==200,output.text
    book=load_workbook(BytesIO(output.content))
    values=[c.value for s in book for row in s for c in row]
    assert '旧值' in values and '新值' in values
    book.close()
    assert client.delete(f'/api/runs/{first["id"]}',headers=h).status_code==200
    surviving = client.get(url+'?include_reviewed=true',headers=h).json()
    assert sorted(r['ai_value'] for r in surviving) == ['新值', '旧值', '旧值']
    assert all(not r['pending_review'] for r in surviving)
    output = client.get(f'/api/runs/{run["id"]}/export?include_reviewed=true', headers=h)
    book = load_workbook(BytesIO(output.content))
    assert '旧值' in [c.value for s in book for row in s for c in row]
    book.close()
    detail = client.get(f'/api/runs/{run["id"]}', headers=h).json()
    assert detail['total'] == detail['completed'] == 1
    assert client.delete(f'/api/reports/{report["id"]}',headers=h).status_code==200


def test_manual_reviewed_without_ai_result_is_still_visible(client,headers):
    h=headers()
    report=upload(client,h)
    item=client.get(f'/api/reports/{report["id"]}/items',headers=h).json()[0]
    client.patch(f'/api/items/{item["id"]}/review-state',headers=h,json={'state':'reviewed','revision':item['review_revision'],'reviewed_revision':item['reviewed_revision']})
    run=client.post(f'/api/reports/{report["id"]}/runs',headers=h,json={'selection_mode':'missing'}).json()
    rows=client.get(f'/api/runs/{run["id"]}/results?include_reviewed=true',headers=h).json()
    assert len(rows)==3
    manual=next(r for r in rows if r['item_id']==item['id'])
    assert not manual['pending_review'] and manual['id']=='' and manual['ai_value']==''


@pytest.mark.parametrize('restart', [True, False])
def test_selected_history_is_independent_of_newer_history_and_deletion(client, headers, monkeypatch, restart):
    h = headers()
    report = upload(client, h)
    base = f'/api/reports/{report["id"]}'
    monkeypatch.setattr('app.worker.review', lambda *args: (ReviewVerdict(status='passed', explanation='通过', ai_threshold='A门限', ai_value='A值'), {}))
    first = client.post(base + '/runs', headers=h).json()
    assert process_once()
    monkeypatch.setattr('app.worker.review', lambda *args: (ReviewVerdict(status='passed', explanation='通过', ai_value='B值'), {}))
    second = client.post(base + '/runs', headers=h).json()
    assert process_once()
    item = client.get(base + '/items', headers=h).json()[-1]
    client.patch(f'/api/items/{item["id"]}/review-state', headers=h,
        json={'state':'pending', 'revision':item['review_revision'], 'reviewed_revision':item['reviewed_revision']})
    url = f'/api/runs/{first["id"]}/restart' if restart else base + '/runs'
    response = client.post(url, headers=h, json={'selection_mode':'missing', 'base_run_id':first['id']})
    assert response.status_code in (200, 201), response.text
    new = response.json()
    monkeypatch.setattr('app.worker.review', lambda *args: (ReviewVerdict(status='passed', explanation='通过', ai_threshold='C门限', ai_value='C值'), {}))
    assert process_once()
    for history in (first, second):
        assert client.delete(f'/api/runs/{history["id"]}', headers=h).status_code == 200
    rows = client.get(f'/api/runs/{new["id"]}/results?include_reviewed=true', headers=h).json()
    assert sorted(r['ai_value'] for r in rows) == ['A值', 'A值', 'C值']
    assert all(not r['pending_review'] for r in rows)
    assert client.get(f'/api/runs/{new["id"]}', headers=h).json()['completed'] == 1


def test_display_permissions(client,headers,monkeypatch):
    h,admin=headers(),headers('admin')
    report,_,run=prepare(client,admin,monkeypatch)
    path=f'/api/runs/{run["id"]}'
    assert client.get(path+'/results?include_reviewed=true',headers=h).status_code==404
    assert client.get(path+'/result-options?include_reviewed=true',headers=h).status_code==404
    assert client.get(path+'/export?include_reviewed=true',headers=h).status_code==404


def test_missing_keyword_runs_once_and_retained_history_survives_future_review(client,headers,monkeypatch):
    h=headers()
    report,first,run=prepare(client,h,monkeypatch)
    path=f'/api/runs/{run["id"]}'
    calls=[]
    def review(*args):
        calls.append(args)
        return ReviewVerdict(status='missing_keyword',explanation='缺少关键词'), {}
    monkeypatch.setattr('app.worker.review',review)
    assert process_once() and not process_once() and len(calls)==1
    rows=client.get(path+'/results?include_reviewed=true',headers=h).json()
    old_ids={r['id'] for r in rows if not r['pending_review']}
    assert len(rows)==3 and len(old_ids)==2
    assert len([r for r in rows if r['pending_review']])==1
    monkeypatch.setattr('app.worker.review',lambda *args:(ReviewVerdict(status='passed',explanation='通过',ai_value='未来结果'),{}))
    client.post(f'/api/reports/{report["id"]}/runs',headers=h)
    assert process_once()
    historical=client.get(path+'/results?include_reviewed=true',headers=h).json()
    assert old_ids <= {r['id'] for r in historical}
    assert all(r['ai_value']!='未来结果' for r in historical)


def test_existing_missing_history_is_backfilled_idempotently(client,headers,monkeypatch):
    from app.migrate import main
    h=headers()
    _,first,run=prepare(client,h,monkeypatch)
    with Session() as db:
        for retained in db.scalars(select(RunRetainedItem)).all():
            clone_id = retained.result_id
            original = db.scalar(select(Result).where(Result.run_id == first['id'], Result.item_id == retained.item_id))
            db.get(ItemReviewState, retained.item_id).source_result_id = original.id
            retained.result_id = original.id
            db.flush()
            db.execute(delete(ResultMeasurements).where(ResultMeasurements.result_id == clone_id))
            db.execute(delete(ResultWork).where(ResultWork.result_id == clone_id))
            db.execute(delete(Result).where(Result.id == clone_id))
        db.commit()
    main(); main()
    rows=client.get(f'/api/runs/{run["id"]}/results?include_reviewed=true',headers=h).json()
    assert len(rows)==3 and len([r for r in rows if r['ai_value']=='旧值'])==2
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(RunRetainedItem))==2
        assert db.scalar(select(func.count()).select_from(Result).where(Result.run_id == run['id'])) == 3


def test_supplemental_real_adapter_three_strings_reach_display(client, headers, monkeypatch):
    from test_local_review import configure_rule
    from test_workflow import change
    owner, admin = headers(), headers('admin')
    report = upload(client, owner)
    response = client.post('/api/keyword-requests', headers=admin,
        json={**change(domain='射频'), 'payload': {'e': '功率'}})
    assert response.status_code == 201
    configure_rule(monkeypatch, lambda *args: ('旧门限', '旧值', '通过'))
    base = f'/api/reports/{report["id"]}'
    first = client.post(base + '/runs', headers=owner).json()
    assert process_once()
    item = next(i for i in client.get(base + '/items', headers=owner).json() if i['domain'] == '射频')
    client.patch(f'/api/items/{item["id"]}/review-state', headers=owner,
        json={'state':'pending', 'revision':item['review_revision'], 'reviewed_revision':item['reviewed_revision']})
    calls = []
    def local_rule(*args):
        calls.append(args)
        return '25', '21', '通过'
    configure_rule(monkeypatch, local_rule)
    response = client.post(f'/api/runs/{first["id"]}/restart', headers=owner, json={'selection_mode':'missing'})
    assert response.status_code == 200, response.text
    run = response.json()
    queued = client.get(f'/api/runs/{run["id"]}/results?include_reviewed=true', headers=owner).json()
    pending = next(r for r in queued if r['item_id'] == item['id'])
    assert pending['pending_review']
    assert (pending['ai_threshold'], pending['ai_value'], pending['explanation']) == ('', '', '')
    assert process_once() and len(calls) == 1
    rows = client.get(f'/api/runs/{run["id"]}/results?include_reviewed=true', headers=owner).json()
    updated = next(r for r in rows if r['item_id'] == item['id'])
    assert not updated['pending_review'] and updated['status'] == 'passed'
    assert (updated['ai_threshold'], updated['ai_value']) == ('25', '21')
    assert updated['id'] == pending['id']
    assert updated['raw'] == {'ai_threshold':'25', 'ai_value':'21', 'result':'通过'}
    assert any(r['ai_value'] == '旧值' for r in rows)
