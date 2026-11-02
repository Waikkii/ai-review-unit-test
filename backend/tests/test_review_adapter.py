import sys
import pytest
import httpx
from app.config import settings
from app.auth_sessions import CookieExpired, download_with_session
from app.reviewer import review, InvalidVerdict
from test_auth_sessions import adapter
from test_local_review import configure_rule


@pytest.mark.parametrize("valid", [True, False])
def test_local_review_verdict_contract_and_original_response(client, monkeypatch, valid):
    adapter(monkeypatch)
    raw = {"model": "local-model", "response": {"value": "12"}}

    def rule(unit, keyword, attachment_url, criterion, threshold, value, fetchimage):
        assert attachment_url == ""  # No attachment is an empty string.
        return {"status": "passed" if valid else "unknown", "explanation": "审查说明"}, raw

    configure_rule(monkeypatch, rule)
    if valid:
        verdict, returned = review({"单位": "dB", "附件链接": ""}, [{"payload": {"e": "功率"}}], "demo")
        assert verdict.status == "passed" and returned == raw
    else:
        with pytest.raises(InvalidVerdict) as error:
            review({"单位": "dB", "附件链接": ""}, [{"payload": {"e": "功率"}}], "demo")
        assert error.value.raw == raw


@pytest.mark.parametrize("images", [
    (b"\xff\xd8\xffone-image", "image/jpeg"),  # Old tuple format is rejected.
    [(b"\xff\xd8\xffvalid", "image/jpeg"), (b"not-a-png", "image/png")],
    [(b"\xff\xd8\xffvalid", "image/jpeg")] * 11,
])
def test_multi_image_return_format_and_each_image_are_validated(client, monkeypatch, images):
    adapter(monkeypatch)
    sys.modules[settings.local_auth_module].fetchimage = lambda url, cookies: images

    def rule(unit, keyword, attachment_url, criterion, threshold, value, fetchimage):
        fetchimage(attachment_url)
        return {"status": "passed", "explanation": "must not accept invalid images"}, {}

    configure_rule(monkeypatch, rule)
    with pytest.raises(ValueError):
        review({"单位": "dB", "附件链接": "https://intranet.example/entry"}, [{"payload": {"e": "功率"}}], "demo")


def test_empty_image_list_is_passed_to_business_review(client, monkeypatch):
    adapter(monkeypatch)
    sys.modules[settings.local_auth_module].fetchimage = lambda url, cookies: []

    def rule(unit, keyword, attachment_url, criterion, threshold, value, fetchimage):
        assert fetchimage(attachment_url) == []
        return {"status": "needs_review", "explanation": "附件入口没有图片"}, {}

    configure_rule(monkeypatch, rule)
    verdict, _ = review({"单位": "dB", "附件链接": "https://intranet.example/entry"}, [{"payload": {"e": "功率"}}], "demo")
    assert verdict.status == "needs_review"


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_status_triggers_one_refresh_without_custom_error(client, monkeypatch, status):
    calls = adapter(monkeypatch)
    original = sys.modules[settings.local_auth_module].fetchimage

    def fetch(url, cookies):
        if cookies["session"] == "old-cookie-secret":
            response = httpx.Response(status, request=httpx.Request("GET", url))
            response.raise_for_status()
        return original(url, cookies)

    sys.modules[settings.local_auth_module].fetchimage = fetch
    images = download_with_session("https://intranet.example/entry", "demo")
    assert len(images) == 1 and len(calls["login"]) == 1


def test_cookie_expired_is_a_signal_exception_not_a_network_stub():
    assert str(CookieExpired()) == "登录状态已失效"
