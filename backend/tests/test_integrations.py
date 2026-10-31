import json
import sys
import types
import pytest
import httpx
from app.config import settings
from app.reviewer import review, InvalidVerdict

@pytest.mark.parametrize("valid", [True, False])
def test_openai_compatible_adapter_and_raw_result(monkeypatch, valid):
    adapter = types.ModuleType("test_corp")
    downloaded = []
    def download(url, username):
        downloaded.append((url, username))
        return b"\xff\xd8\xfftest-jpeg", "image/jpeg"
    def messages(fields, keywords, images):
        assert images[0]["url"].startswith("data:image/jpeg;base64,")
        assert fields["用例编号"] == "TC-001"
        assert keywords[0]["payload"] == {"any_domain_key": ["custom"]}
        return [{"role":"user","content":[{"type":"text","text":"检查测试项"},
            {"type":"image_url","image_url":images[0]}]}]
    adapter.download_attachment = download
    adapter.build_messages = messages
    monkeypatch.setitem(sys.modules, "test_corp", adapter)
    for key, value in {"app_mode":"production","integration_module":"test_corp",
                       "attachment_hosts":"intranet.example","ai_base_url":"http://model.internal/v1",
                       "ai_model":"vision-test","ai_api_key":"private-key"}.items():
        monkeypatch.setattr(settings,key,value)
    verdict = {"status":"passed" if valid else "unknown", "explanation":"符合规则", "evidence":["图片指标一致"], "suggestion":""}
    raw = {"id":"response-123","choices":[{"message":{"content":json.dumps(verdict)}}]}
    def handler(request):
        assert str(request.url) == "http://model.internal/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer private-key"
        body = json.loads(request.content)
        assert body["model"] == "vision-test"
        assert "intranet.example" not in request.content.decode()
        return httpx.Response(200,json=raw)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    args=({"用例编号":"TC-001","附件链接":"https://intranet.example/a.jpg"},
          [{"payload":{"any_domain_key":["custom"]}}],"worker-user")
    if valid:
        result, original = review(*args)
        assert result.status == "passed"
        assert original == raw
    else:
        with pytest.raises(InvalidVerdict) as exc:
            review(*args)
        assert exc.value.raw == raw
    assert downloaded == [("https://intranet.example/a.jpg","worker-user")]

def test_adapter_rejects_fake_image(monkeypatch):
    adapter = types.ModuleType("test_corp")
    adapter.download_attachment = lambda url, username: (b"not-an-image", "image/jpeg")
    monkeypatch.setitem(sys.modules,"test_corp",adapter)
    monkeypatch.setattr(settings,"app_mode","production")
    monkeypatch.setattr(settings,"integration_module","test_corp")
    monkeypatch.setattr(settings,"attachment_hosts","intranet.example")
    with pytest.raises(ValueError,match="JPEG"):
        review({"附件链接":"https://intranet.example/a.jpg"},[],"demo")
