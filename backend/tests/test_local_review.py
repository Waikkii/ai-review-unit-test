import sys
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


def test_review_receives_unit_single_keyword_and_links_and_downloads_inside_rule(client, monkeypatch):
    calls = adapter(monkeypatch)
    url = "https://intranet.example/a.jpg"
    entered = []

    def rule(unit, keyword, urls, fetchimage):
        assert unit == "dB" and keyword == "输出功率" and urls == [url]
        assert calls["fetch"] == [], "framework must not download before the rule is called"
        entered.append(True)
        image_bytes, mime = fetchimage(urls[0])
        assert image_bytes.startswith(b"\xff\xd8\xff") and mime == "image/jpeg"
        return {"status": "passed", "explanation": "图片符合门限", "ai_threshold": "10", "ai_value": "8.5"}, {"model": "local-test"}

    configure_rule(monkeypatch, rule)
    verdict, raw = review({"单位": "dB", "附件链接": url}, [{"payload": {"e": "输出功率"}}], "demo")
    assert entered == [True] and verdict.ai_value == "8.5" and raw == {"model": "local-test"}
    assert len(calls["login"]) == 1 and len(calls["fetch"]) == 2


@pytest.mark.parametrize("keywords", [[], [{"payload": {"e": " "}}], [{"payload": {"e": ""}}]])
def test_missing_keyword_short_circuits_before_rule_or_download(client, monkeypatch, keywords):
    calls = adapter(monkeypatch)

    def rule(unit, keyword, urls, fetchimage):
        raise AssertionError("missing keyword must not reach local rule")

    configure_rule(monkeypatch, rule)
    verdict, _ = review({"单位": "dB", "附件链接": "invalid-placeholder"}, keywords, "demo")
    assert verdict.status == "missing_keyword" and verdict.explanation == "缺少关键词"
    assert verdict.ai_threshold == verdict.ai_value == "" and calls["fetch"] == calls["login"] == []


def test_rule_cannot_swallow_auth_failure_or_issue_more_downloads_after_it(client, monkeypatch):
    calls = adapter(monkeypatch, fail_retry=True)
    url = "https://intranet.example/a.jpg"

    def rule(unit, keyword, urls, fetchimage):
        for _ in range(2):
            try:
                fetchimage(urls[0])
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
