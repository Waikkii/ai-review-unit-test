import importlib
import json
import re
from urllib.parse import urlparse
from .config import settings
from .schemas import ReviewVerdict
from .keywords import ENTRY_DOMAIN, keyword_text
from .auth_sessions import download_with_session, SessionAuthFailure

class InvalidVerdict(ValueError):
    def __init__(self, raw):
        super().__init__('模型返回不符合审查契约')
        self.raw = raw

def rule_version():
    if settings.app_mode == "demo":
        return "demo-no-verdict-v1"
    return str(importlib.import_module(settings.local_review_module).RULE_VERSION)

def review(fields, keywords, username):
    if not fields.get("附件链接", "").strip():
        return ReviewVerdict(status="needs_review", explanation="缺少附件"), {
            "system": True, "reason": "missing_attachment"}
    if re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", fields.get("判据门限", "")):
        return ReviewVerdict(status="needs_review", explanation="主观类"), {
            "system": True, "reason": "subjective"}
    payload = keywords[0].get("payload", {}) if len(keywords) == 1 else {}
    keyword_value = keyword_text(keywords[0]) if len(keywords) == 1 else None
    if not keywords or (len(keywords) == 1 and (settings.local_review_module or "e" in payload
                        or keywords[0].get("domain") == ENTRY_DOMAIN)
                        and (not isinstance(keyword_value, str) or not keyword_value.strip())):
        return ReviewVerdict(status="missing_keyword", explanation="缺少关键词",
                             suggestion="请补充关键词并生效后执行查漏补缺。"), {
                                 "system": True, "reason": "missing_keyword", "demo": settings.app_mode == "demo"}
    if settings.app_mode == "demo":
        verdict = ReviewVerdict(status="needs_review", explanation="演示模式：尚未接入内网图片、AI 和审查规则，不作通过判定。",
                                evidence=[], suggestion="接入真实规则后创建新的审查批次。")
        return verdict, {"demo": True, "verdict": verdict.model_dump()}
    attachment_url = fields.get("附件链接", "").strip()
    auth_failure = None

    def fetchimage(url: str) -> list[tuple[str, str]]:
        """Bound to the current user; the local rule downloads only when needed."""
        nonlocal auth_failure
        if auth_failure:
            raise auth_failure
        if not isinstance(url, str) or any(c.isspace() for c in url):
            raise ValueError("fetchimage 需要单个 URL 字符串")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("附件地址必须为不含账号密码的有效 HTTP/HTTPS URL")
        try:
            images = download_with_session(url, username)
        except SessionAuthFailure as exc:
            auth_failure = exc
            raise
        if not isinstance(images, list) or len(images) > 10:
            raise ValueError("fetchimage 必须返回最多 10 张图片的 list")
        validated = []
        for item in images:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError("每张图片必须为 (Base64字符串, MIME)")
            content, mime = item
            if not isinstance(content, str) or mime not in {"image/jpeg", "image/png", "image/gif", "image/bmp", "image/x-ms-bmp"}:
                raise ValueError("附件格式不符合要求，支持 JPEG、PNG、GIF、BMP")
            # 本地下载函数已转换为 Base64；原样透传，不解码或重复编码。
            validated.append((content, mime))
        return validated

    local = importlib.import_module(settings.local_review_module)
    if len(keywords) > 1:
        raise ValueError("本地审查需要唯一的关键词记录")
    keyword = keyword_value
    try:
        output = local.review_item(fields.get("单位", ""), keyword, attachment_url,
                                   fields.get("判据门限", ""), fields.get("门限", ""),
                                   fields.get("值", ""), fetchimage)
    except Exception:
        if auth_failure:
            raise auth_failure
        raise
    if auth_failure:
        raise auth_failure
    if isinstance(output, (tuple, list)) and len(output) == 3:
        threshold, value, result = output
        if not all(isinstance(part, str) for part in output):
            raise ValueError("本地审查的门限、值、结果必须为三个字符串")
        raw = {"ai_threshold": threshold, "ai_value": value, "result": result}
        text = result.strip()
        status = "passed" if text == "通过" else "failed" if "判断错误" in text else "needs_review"
        verdict = {"status": status, "explanation": result if text else "待人工确认",
                   "ai_threshold": threshold, "ai_value": value}
    elif isinstance(output, (tuple, list)) and len(output) == 2:
        # 兼容原有 (verdict, raw) 返回格式。
        verdict, raw = output
    else:
        raise ValueError("本地审查应返回 (AI门限, AI值, 结果)")
    if not isinstance(raw, dict):
        raise ValueError("本地审查原始返回必须为对象")
    json.dumps(raw, ensure_ascii=False, allow_nan=False)
    try:
        verdict = ReviewVerdict.model_validate(verdict)
    except ValueError as exc:
        raise InvalidVerdict(raw) from exc
    return verdict, raw
