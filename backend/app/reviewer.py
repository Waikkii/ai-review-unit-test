import base64
import importlib
import json
from urllib.parse import urlparse
import httpx
from .config import settings
from .excel import attachment_urls
from .schemas import ReviewVerdict

class InvalidVerdict(ValueError):
    def __init__(self, raw):
        super().__init__('模型返回不符合审查契约')
        self.raw = raw

def rule_version():
    if settings.app_mode == "demo":
        return "demo-no-verdict-v1"
    return str(importlib.import_module(settings.integration_module).RULE_VERSION)

def review(fields, keywords, username):
    if settings.app_mode == "demo":
        verdict = ReviewVerdict(status="needs_review", explanation="演示模式：尚未接入内网图片、AI 和审查规则，不作通过判定。",
                                evidence=[], suggestion="接入真实规则后创建新的审查批次。")
        return verdict, {"demo": True, "verdict": verdict.model_dump()}
    module = importlib.import_module(settings.integration_module)
    allowed = {host.strip().lower() for host in settings.attachment_hosts.split(",") if host.strip()}
    urls = attachment_urls(fields["附件链接"])
    if not urls or len(urls) > 10:
        raise ValueError("附件链接数量无效")
    images = []
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in allowed or parsed.username or parsed.password:
            raise ValueError("附件地址未列入允许列表")
        content, mime = module.download_attachment(url, username)
        if mime not in {"image/jpeg", "image/png"} or len(content) > 10 * 1024 * 1024:
            raise ValueError("附件格式或大小不符合要求")
        if mime == "image/jpeg" and not content.startswith(b"\xff\xd8\xff"):
            raise ValueError("无效的 JPEG 图片")
        if mime == "image/png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("无效的 PNG 图片")
        images.append({"url": "data:" + mime + ";base64," + base64.b64encode(content).decode()})
    if not settings.ai_base_url or not settings.ai_model:
        raise ValueError("AI 配置不完整")
    messages = module.build_messages(fields, keywords, images)
    with httpx.Client(timeout=90, follow_redirects=False, trust_env=False) as client:
        response = client.post(settings.ai_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + settings.ai_api_key},
            json={"model": settings.ai_model, "messages": messages,
                  "response_format": {"type": "json_object"}, "temperature": 0})
        response.raise_for_status()
        raw = response.json()
    try:
        verdict = ReviewVerdict.model_validate(json.loads(raw["choices"][0]["message"]["content"]))
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise InvalidVerdict(raw) from exc
    return verdict, raw
