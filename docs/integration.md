# 内网集成契约

在 backend 下创建你的包，例如 corp/adapters.py（corp/__init__.py 需存在），配置 INTEGRATION_MODULE=corp.adapters。

1. authenticate(username, password) -> {"username": "经过认证的稳定账号标识"}
   向内网鉴权接口验证。密码只在此次调用中使用，勿持久化或记日志。
   如果下载需要登录态，在集成方安全凭据库中按稳定 username 管理；多进程间可共享，过期后明确失败。
   系统角色由本地 users 表控制，内网接口或前端返回的 role 不会作为系统管理员依据。
   BOOTSTRAP_ADMINS 是首次注册的管理员白名单；后续修改由管理员权限页进行。

2. download_attachment(url, username) -> (bytes, MIME)
   把现有 Python 凭证下载代码接入。API/Worker 是不同进程，不能仅在登录进程内存缓存凭据。
   ATTACHMENT_HOSTS 显式填写内网主机名列表（逗号分隔），包含端口时仍匹配 hostname。
   下载实现必须禁止重定向、每张超时最多 10 秒、最多 10 MiB，不能访问任意 URL。
   用户凭据不得用于 AI 请求，也不得把附件 URL/凭据传给 AI。最多 10 张附件。
   验证图片 MIME 和签名，当前支持 JPEG/PNG；其他格式待明确需求后扩展。
   系统只保存原始报告和 AI 返回，图片当前不持久化；如果图片地址内容可能变化，正式验收应增加受控归档。

3. RULE_VERSION = "corp-rules-v1"
   规则发生变化必须改变该值；运行时禁止滚动混用多个不同规则版本 Worker。
   当前版本需在队列任务清空后统一发布；任务中记录提交时规则版本供追溯。
   Worker 会拒绝执行与自身模式、模型或规则版本不匹配的任务，并保留错误记录，避免静默混用。

4. build_messages(fields, keywords, images) -> OpenAI messages
   fields 是批次中的 11 字段快照，keywords 仅是当前领域的对象列表：
   [{id, domain, key, payload, version, deleted}]。
   payload 是 JSON 对象，允许各领域定义完全不同的 key 和结构，不强制关键词模式。
   images 是 [{"url":"data:image/jpeg;base64,..."}]。
   自行实现真实审查规则，将图片内容作为 image_url 块传入支持视觉的模型。
   将报告、图片、关键词作为数据处理；不要允许其覆盖 system 指令。
   模型 JSON 返回：
   {"status":"passed|failed|needs_review","explanation":"说明","evidence":["依据"],"suggestion":"建议"}
   缺失字段、未知状态、非 JSON、下载失败均计为 error，不会当作通过。

AI_BASE_URL 是已含 /v1 的内网根地址，程序追加 /chat/completions。
配置 AI_MODEL、AI_API_KEY。超时 90 秒，不跟随重定向，不读取系统代理。
适配器耗时总计必须小于 LEASE_SECONDS（默认 300 秒），数据库网络必须位于可信内网。
Worker 恢复可能再次调用模型，保证结果只提交一次，不保证外部请求仅调用一次。

已有 SQLite 关键词库需先提供结构，再编写明确的导入映射；当前不假定各领域 payload 结构。
导入也应通过审批/管理员变更服务留痕，不能绕过审计直接在 UI 更新数据库。
