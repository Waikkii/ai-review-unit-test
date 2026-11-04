from io import BytesIO
import pytest
from openpyxl import load_workbook
from sqlalchemy import select, func, delete
from app.db import Session
from app.models import Result, RunRetainedItem, ItemReviewState
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


def test_display_retains_reviewed_without_executing_or_copying_their_results(client, headers, monkeypatch):
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
    assert len(kept) == 2 and all(r['id'] in old_ids and r['ai_value']=='旧值' for r in kept)
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
        assert db.scalar(select(func.count()).select_from(Result).where(Result.run_id==run['id']))==1
        assert db.scalar(select(func.count()).select_from(RunRetainedItem).where(RunRetainedItem.run_id==run['id']))==2
    assert client.get(url,headers=h,params={'include_reviewed':True,'domain':'协议','include_total':True}).json()['total']==1
    output=client.get(f'/api/runs/{run["id"]}/export?include_reviewed=true',headers=h)
    assert output.status_code==200,output.text
    book=load_workbook(BytesIO(output.content))
    values=[c.value for s in book for row in s for c in row]
    assert '旧值' in values and '新值' in values
    book.close()
    assert client.delete(f'/api/runs/{first["id"]}',headers=h).status_code==200
    assert client.get(url+'?include_reviewed=true',headers=h).status_code==200
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
        db.execute(delete(RunRetainedItem))
        db.commit()
    main(); main()
    rows=client.get(f'/api/runs/{run["id"]}/results?include_reviewed=true',headers=h).json()
    assert len(rows)==3 and len([r for r in rows if r['ai_value']=='旧值'])==2
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(RunRetainedItem))==2
