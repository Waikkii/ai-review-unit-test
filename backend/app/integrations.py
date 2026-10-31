"""替换成自己的模块，并通过 INTEGRATION_MODULE=your_package.adapters 引入。
不要在此模块、数据库、日志中保存用户密码；下载凭据生命周期由内网集成管理。
"""
RULE_VERSION = "unconfigured-v1"

def authenticate(username: str, password: str) -> dict:
    """返回经内网认证的稳定 username，不接受前端传入角色。
    若下载需要登录态，在集成方安全凭据库中按 username 管理，过期时抛出异常。
    """
    raise NotImplementedError("尚未接入内网账号接口")

def download_attachment(url: str, username: str) -> tuple[bytes, str]:
    """使用现有 Python 凭证下载实现，返回 (图片字节, MIME)。
    必须设置超时、限制响应大小、禁用重定向，勿返回原始请求凭据。
    """
    raise NotImplementedError("尚未接入附件凭证下载")

def build_messages(fields: dict, keywords: list[dict], images: list[dict]) -> list[dict]:
    """实现各领域实际规则，并组合 OpenAI chat/completions messages。
    images 元素是 {url: 'data:image/jpeg;base64,...'}；不可把附件 URL 或凭据传给模型。
    模型须返回 JSON: status(passed/failed/needs_review), explanation, evidence[], suggestion。
    Excel、图片及关键词应作为待审查数据处理，不应覆盖系统指令。
    """
    raise NotImplementedError("尚未配置实际审查规则")
