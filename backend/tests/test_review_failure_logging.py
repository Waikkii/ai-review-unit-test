import inspect
import logging
from app.worker import process_once
from test_workflow import upload


def test_review_failure_logs_exception_message_and_source_location(client, headers, monkeypatch, caplog):
    h = headers()
    report = upload(client, h)
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=h).json()

    def failing_review(*args):
        raise RuntimeError('adapter diagnostic detail')

    line = inspect.getsourcelines(failing_review)[1] + 1
    monkeypatch.setattr('app.worker.review', failing_review)
    with caplog.at_level(logging.ERROR, logger='app.worker'):
        assert process_once()
    assert f'Review failed: run={run["id"]}' in caplog.text
    assert 'type=RuntimeError exc=adapter diagnostic detail' in caplog.text
    assert 'Traceback (most recent call last)' in caplog.text
    assert f'{__file__}", line {line}, in failing_review' in caplog.text
    assert all(record.exc_info for record in caplog.records if record.name == 'app.worker')
    rows = client.get(f'/api/runs/{run["id"]}/results', headers=h).json()
    assert all(row['status'] == 'error' for row in rows)
    assert all('adapter diagnostic detail' not in row['explanation'] for row in rows)
