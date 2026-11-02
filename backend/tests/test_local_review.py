import sys
import base64
import types
import pytest
from app.config import settings
from app.reviewer import review
from app.auth_sessions import SessionAuthFailure
from test_auth_sessions import adapter


def configure_rule(monkeypatch, function):
    module = types.ModuleType("local_rule_contract_test")
    module.RULE_VERSION = "rule-contract-v1"
    module.review_item = function
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(settings, "local_review_module", module.__name__)
    monkeypatch.setattr(settings, "app_mode", "local")
    monkeypatch.setattr(settings, "attachment_hosts", "intranet.example")


def test_review_receives_unit_keyword_and_url_string_and_downloads_multiple_images_inside_rule(client, monkeypatch):
    calls = adapter(monkeypatch)
    url = "https://intranet.example/a.jpg"
    entered = []
    original = sys.modules[settings.local_auth_module].fetchimage

    def fetch(url, cookies):
        return original(url, cookies) + [(base64.b64encode(b"\x89PNG\r\n\x1a\ntest-png").decode("ascii"), "image/png")]

    sys.modules[settings.local_auth_module].fetchimage = fetch

    def rule(unit, keyword, attachment_url, criterion, threshold, value, fetchimage):
        assert unit == "dB" and keyword == "输出功率" and attachment_url == url
        assert isinstance(attachment_url, str)
        assert calls["fetch"] == [], "framework must not download before the rule is called"
        entered.append(True)
        images = fetchimage(attachment_url)
        assert len(images) == 2
        assert base64.b64decode(images[0][0]).startswith(b"\xff\xd8\xff") and images[0][1] == "image/jpeg"
        assert base64.b64decode(images[1][0]).startswith(b"\x89PNG\r\n\x1a\n") and images[1][1] == "image/png"
        return {"status": "passed", "explanation": "图片符合门限", "ai_threshold": "10", "ai_value": "8.5"}, {"model": "local-test"}

    configure_rule(monkeypatch, rule)
    verdict, raw = review({"单位": "dB", "附件链接": url}, [{"payload": {"e": "输出功率"}}], "demo")
    assert entered == [True] and verdict.ai_value == "8.5" and raw == {"model": "local-test"}
    assert len(calls["login"]) == 1 and len(calls["fetch"]) == 2


@pytest.mark.parametrize("keywords", [[], [{"payload": {"e": " "}}], [{"payload": {"e": ""}}]])
def test_missing_keyword_short_circuits_before_rule_or_download(client, monkeypatch, keywords):
    calls = adapter(monkeypatch)

    def rule(unit, keyword, attachment_url, criterion, threshold, value, fetchimage):
        raise AssertionError("missing keyword must not reach local rule")

    configure_rule(monkeypatch, rule)
    verdict, _ = review({"单位": "dB", "附件链接": "invalid-placeholder"}, keywords, "demo")
    assert verdict.status == "missing_keyword" and verdict.explanation == "缺少关键词"
    assert verdict.ai_threshold == verdict.ai_value == "" and calls["fetch"] == calls["login"] == []


def test_rule_cannot_swallow_auth_failure_or_issue_more_downloads_after_it(client, monkeypatch):
    calls = adapter(monkeypatch, fail_retry=True)
    url = "https://intranet.example/a.jpg"

    def rule(unit, keyword, attachment_url, criterion, threshold, value, fetchimage):
        for _ in range(2):
            try:
                fetchimage(attachment_url)
            except Exception:
                pass
        return {"status": "passed", "explanation": "must not be accepted"}, {}

    configure_rule(monkeypatch, rule)
    with pytest.raises(SessionAuthFailure):
        review({"单位": "dB", "附件链接": url}, [{"payload": {"e": "功率"}}], "demo")
    assert len(calls["fetch"]) == 2 and len(calls["login"]) == 1


def test_local_rule_raw_output_must_be_json_serializable(client, monkeypatch):
    adapter(monkeypatch)
    configure_rule(monkeypatch, lambda *args: ({"status": "needs_review", "explanation": "格式检查"}, {"bytes": b"not-json"}))
    with pytest.raises(TypeError):
        review({"单位": "", "附件链接": ""}, [{"payload": {"e": "功率"}}], "demo")


@pytest.mark.parametrize("result,status", [("通过", "passed"), (" 通过\n", "passed"),
    ("功率判断错误。", "failed"), ("判断错误，请人工排查", "failed"),
    ("通过，但电压判断错误", "failed"),
    ("无有效图片。", "needs_review"), ("功率错误，请人工排查。", "needs_review"),
    ("待人工确认", "needs_review"), ("不通过", "needs_review"), ("通过。", "needs_review"),
    ("passed", "needs_review"), ("failed", "needs_review"), ("unknown", "needs_review"),
    ("", "needs_review"), ("  ", "needs_review")])
def test_extended_inputs_and_three_string_outputs(client, monkeypatch, result, status):
    fields = {"单位": "dB", "附件链接": "", "判据门限": "大于等于", "门限": "0", "值": "0.00"}
    original = fields.copy()

    def rule(unit, keyword, attachment_url, criterion, threshold, value, fetchimage):
        assert (unit, keyword, attachment_url, criterion, threshold, value) == (
            "dB", "功率", "", "大于等于", "0", "0.00")
        assert callable(fetchimage)
        return "-10", "0.01", result

    configure_rule(monkeypatch, rule)
    verdict, raw = review(fields, [{"payload": {"e": "功率"}}], "demo")
    assert verdict.status == status
    assert (verdict.ai_threshold, verdict.ai_value) == ("-10", "0.01")
    assert raw == {"ai_threshold": "-10", "ai_value": "0.01", "result": result}
    assert verdict.explanation == (result if result.strip() else "待人工确认")
    assert fields == original


@pytest.mark.parametrize("output", [(10, "12", "通过"),
                                   ("10", None, "通过"), ("10", "12", "通过", {})])
def test_invalid_three_value_output_is_rejected(client, monkeypatch, output):
    configure_rule(monkeypatch, lambda *args: output)
    with pytest.raises(ValueError):
        review({}, [{"payload": {"e": "功率"}}], "demo")


def test_empty_original_fields_and_unreadable_ai_measurements(client, monkeypatch):
    def rule(unit, keyword, attachment_url, criterion, threshold, value, fetchimage):
        assert (criterion, threshold, value) == ("", "", "")
        return "", "", "待人工确认"

    configure_rule(monkeypatch, rule)
    verdict, _ = review({}, [{"payload": {"e": "功率"}}], "demo")
    assert verdict.ai_threshold == verdict.ai_value == ""
    assert verdict.status == "needs_review"


def test_worker_persists_three_value_response_and_preserves_original_fields(client, headers, monkeypatch):
    from app.worker import process_once
    from test_workflow import upload, change

    owner, admin = headers(), headers("admin")
    report = upload(client, owner)
    request = {**change(domain="射频"), "payload": {"e": "功率"}}
    assert client.post('/api/keyword-requests', headers=admin, json=request).status_code == 201
    configure_rule(monkeypatch, lambda *args: ("AI门限", "AI值", "功率判断错误。"))
    run = client.post(f'/api/reports/{report["id"]}/runs', headers=owner).json()
    assert process_once()
    rows = client.get(f'/api/runs/{run["id"]}/results', headers=owner).json()
    reviewed = [row for row in rows if row["domain"] == "射频"]
    assert len(reviewed) == 2
    assert all((row["ai_threshold"], row["ai_value"], row["status"]) == ("AI门限", "AI值", "failed") for row in reviewed)
    assert all(row["result_label"] == "功率判断错误。" for row in reviewed)
    assert {row["fields"]["值"] for row in reviewed} == {"12", "8"}
    assert all(row["fields"]["门限"] == "10" for row in reviewed)
