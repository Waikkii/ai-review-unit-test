import base64
import importlib
import json
from urllib.parse import urlparse
import httpx
from .config import settings
from .excel import attachment_urls
from .schemas import ReviewVerdict
from .auth_sessions import download_with_session, SessionAuthFailure

class InvalidVerdict(ValueError):
    def __init__(self, raw):
        super().__init__('模型返回不符合审查契约')
        self.raw = raw

def rule_version():
    if settings.app_mode == "demo":
        return "demo-no-verdict-v1"
    return str(importlib.import_module(settings.local_review_module or settings.integration_module).RULE_VERSION)

def review(fields, keywords, username):
    payload = keywords[0].get("payload", {}) if len(keywords) == 1 else {}
    keyword_value = payload.get("e")
    if not keywords or (len(keywords) == 1 and (settings.local_review_module or "e" in payload)
                        and (not isinstance(keyword_value, str) or not keyword_value.strip())):
        return ReviewVerdict(status="missing_keyword", explanation="缺少关键词",
                             suggestion="请补充关键词并生效后执行查漏补缺。"), {
                                 "system": True, "reason": "missing_keyword", "demo": settings.app_mode == "demo"}
    if settings.app_mode == "demo":
        verdict = ReviewVerdict(status="needs_review", explanation="演示模式：尚未接入内网图片、AI 和审查规则，不作通过判定。",
                                evidence=[], suggestion="接入真实规则后创建新的审查批次。")
        return verdict, {"demo": True, "verdict": verdict.model_dump()}
    module = importlib.import_module(settings.integration_module)
    allowed = {host.strip().lower() for host in settings.attachment_hosts.split(",") if host.strip()}
    urls = attachment_urls(fields["附件链接"])
    if (fields["附件链接"].strip() and not urls) or len(urls) > 10:
        raise ValueError("附件链接数量无效")
    auth_failure = None

    def fetchimage(url):
        """Bound to the current user; the local rule downloads only when needed."""
        nonlocal auth_failure
        if auth_failure:
            raise auth_failure
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in allowed or parsed.username or parsed.password:
            raise ValueError("附件地址未列入允许列表")
        try:
            content, mime = (download_with_session(url, username) if settings.local_auth_module
                             else module.download_attachment(url, username))
        except SessionAuthFailure as exc:
            auth_failure = exc
            raise
        if mime not in {"image/jpeg", "image/png"} or len(content) > 10 * 1024 * 1024:
            raise ValueError("附件格式或大小不符合要求")
        if mime == "image/jpeg" and not content.startswith(b"\xff\xd8\xff"):
            raise ValueError("无效的 JPEG 图片")
        if mime == "image/png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("无效的 PNG 图片")
        return content, mime

    if settings.local_review_module:
        local = importlib.import_module(settings.local_review_module)
        if len(keywords) > 1 or (keywords and not isinstance(keywords[0]["payload"].get("e"), str)):
            raise ValueError("本地审查需要唯一的五字段关键词记录")
        keyword = keywords[0]["payload"]["e"] if keywords else None
        try:
            verdict, raw = local.review_item(fields.get("单位", ""), keyword, urls, fetchimage)
        except Exception:
            if auth_failure:
                raise auth_failure
            raise
        if auth_failure:
            raise auth_failure
        if not isinstance(raw, dict):
            raise ValueError("本地审查原始返回必须为对象")
        json.dumps(raw, ensure_ascii=False, allow_nan=False)
        return ReviewVerdict.model_validate(verdict), raw
    images = []
    for url in urls:
        content, mime = fetchimage(url)
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
