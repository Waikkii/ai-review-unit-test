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
    [(b"not-a-gif", "image/gif")],
    [(b"not-a-bmp", "image/bmp")],
    [(b"not-a-bmp", "image/x-ms-bmp")],
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


@pytest.mark.parametrize("url", ["https://another.intranet.local/images", "http://10.20.30.40:8080/file",
                                 "http://localhost/image", "https://[::1]/image"])
@pytest.mark.parametrize("old_hosts", ["", "unrelated.example"])
def test_attachment_hosts_are_unrestricted_even_with_old_whitelist_config(client, monkeypatch, url, old_hosts):
    calls = []
    image = (b"\xff\xd8\xfftest", "image/jpeg")

    def download(address, username):
        calls.append((address, username))
        return [image]

    def rule(unit, keyword, attachment_url, criterion, threshold, value, fetchimage):
        assert fetchimage(attachment_url) == [image]
        return "10", "12", "通过"

    configure_rule(monkeypatch, rule)
    monkeypatch.setattr(settings, "attachment_hosts", old_hosts)
    monkeypatch.setattr("app.reviewer.download_with_session", download)
    verdict, _ = review({"附件链接": url}, [{"payload": {"e": "功率"}}], "current-user")
    assert verdict.status == "passed" and calls == [(url, "current-user")]


@pytest.mark.parametrize("url", ["file:///tmp/a.jpg", "ftp://files.intranet/a.jpg", "http:///a.jpg",
                                 "https://name:password@host/image", "https://host/a b.jpg"])
def test_invalid_attachment_urls_are_still_rejected_before_download(client, monkeypatch, url):
    def download(*args):
        raise AssertionError("invalid URL must not be downloaded")

    def rule(unit, keyword, attachment_url, criterion, threshold, value, fetchimage):
        fetchimage(attachment_url)

    configure_rule(monkeypatch, rule)
    monkeypatch.setattr("app.reviewer.download_with_session", download)
    with pytest.raises(ValueError):
        review({"附件链接": url}, [{"payload": {"e": "功率"}}], "current-user")


@pytest.mark.parametrize("header,mime,large", [
    (b"GIF87a", "image/gif", False), (b"GIF89a", "image/gif", False),
    (b"BM", "image/bmp", False), (b"BM", "image/x-ms-bmp", False),
    (b"BM", "image/bmp", True), (b"\xff\xd8\xff", "image/jpeg", True),
])
def test_gif_bmp_and_images_over_previous_size_limit_reach_local_review(client, monkeypatch, header, mime, large):
    content = header + (b"x" * (10 * 1024 * 1024 + 1) if large else b"test-image")
    expected = [(content, mime)]
    monkeypatch.setattr("app.reviewer.download_with_session", lambda url, user: expected)

    def rule(unit, keyword, attachment_url, criterion, threshold, value, fetchimage):
        assert fetchimage(attachment_url) == expected
        return "10", "12", "通过"

    configure_rule(monkeypatch, rule)
    verdict, _ = review({"附件链接": "https://files.intranet/image"}, [{"payload": {"e": "功率"}}], "user")
    assert verdict.status == "passed"
