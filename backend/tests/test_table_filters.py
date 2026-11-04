import json
from app.worker import process_once
from app.schemas import ReviewVerdict
from test_workflow import upload
from test_missing_display import prepare


def test_column_filters_apply_before_pagination_and_offer_facets(client, headers):
    h = headers()
    report = upload(client, h)
    url = f'/api/reports/{report["id"]}/table-view'
    page = client.get(url, headers=h, params={'limit':1}).json()
    assert page['total'] == 3 and len(page['items']) == 1
    assert set(page['options']['domain']) == {'射频', '协议'}
    assert page['options']['result'] == ['待审查']
    assert set(page['options']['field:测试小项']) >= {'小项 A', '小项 B'}
    response = client.get(url, headers=h, params={'filters':json.dumps({'domain':['射频'], 'field:测试小项':['小项 B']})})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['total'] == 1 and data['items'][0]['current']['测试小项'] == '小项 B'
    assert data['options']['field:测试小项'] == ['小项 A', '小项 B']
    assert client.get(url, headers=h, params={'filters':'{"domain":[]}'}).json()['total'] == 0
    assert client.get(url, headers=h, params={'filters':'{"ai_value":[""]}'}).json()['total'] == 3
    for invalid in ('[]', '{', '{"unknown":["x"]}', '{"domain":"射频"}'):
        assert client.get(url, headers=h, params={'filters':invalid}).status_code == 422
    assert client.get(url, headers=headers('admin')).status_code == 200


def test_review_filter_includes_inherited_and_new_measurements(client, headers, monkeypatch):
    h = headers()
    report, _, run = prepare(client, h, monkeypatch)
    monkeypatch.setattr('app.worker.review', lambda *args: (ReviewVerdict(status='passed', explanation='通过', ai_threshold='新门限', ai_value='新值'), {}))
    assert process_once()
    url = f'/api/reports/{report["id"]}/table-view'
    data = client.get(url, headers=h, params={'run_id':run['id'], 'filters':'{"ai_value":["新值"]}'}).json()
    assert data['total'] == 1 and data['items'][0]['ai_value'] == '新值'
    assert set(data['options']['ai_value']) == {'旧值', '新值'}
    assert data['options']['state'] == ['已审查']
    assert data['options']['result'] == ['通过']
    other = upload(client, headers('admin'))
    assert client.get(f'/api/reports/{other["id"]}/table-view', headers=h).status_code == 404
    assert client.get(f'/api/reports/{other["id"]}/table-view', headers=headers('admin'), params={'run_id':run['id']}).status_code == 409
