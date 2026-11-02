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
   fields 是批次中的原始字段快照（新模板包含测试对象；旧 11 字段报告仍可读取），keywords 是匹配的对象列表：
   [{id, domain, key, payload, version, deleted}]。
   新关键词 payload 为 {a,b,c,d,e}，均为字符串；a=测试对象、b=用例编号、c=用例名称、d=测试小项、e=单个关键词。
   a～d 全库唯一，无领域列；Worker 仅将四字段逐字相等的记录传入，不 trim、不模糊匹配、不拆分 e。
   新关键词内部 domain 为保留标识，key 由 a～d 的 JSON 数组计算 SHA-256；集成规则不应依赖其值。
   未命中时 keywords=[]，由正式审查规则决定是否需要人工确认，不自动判为通过。
   兼容旧演示领域 JSON 关键词和历史快照；旧关键词仍按领域筛选，新数据应使用五列接口。
   images 是 [{"url":"data:image/jpeg;base64,..."}]。
   筛选依据为“测试详情”中的“值”非空，与附件是否存在无关。
   附件为空时 images=[]，build_messages 必须支持纯文字审查。
   附件有占位文本但无 HTTP(S) 地址时，上传保留原文，正式审查计为 error，不能当作无附件或通过。
   自行实现真实审查规则，将图片内容作为 image_url 块传入支持视觉的模型。
   将报告、图片、关键词作为数据处理；不要允许其覆盖 system 指令。
   模型 JSON 返回：
   {"status":"passed|failed|needs_review","explanation":"说明","evidence":["依据"],"suggestion":"建议"}
   缺失字段、未知状态、非 JSON、下载失败均计为 error，不会当作通过。

AI_BASE_URL 是已含 /v1 的内网根地址，程序追加 /chat/completions。
配置 AI_MODEL、AI_API_KEY。超时 90 秒，不跟随重定向，不读取系统代理。
适配器耗时总计必须小于 LEASE_SECONDS（默认 300 秒），数据库网络必须位于可信内网。
Worker 恢复可能再次调用模型，保证结果只提交一次，不保证外部请求仅调用一次。

已有 SQLite 关键词库结构已确认：a、b、c、d、e 均为 TEXT，PRIMARY KEY (a,b,c,d)，e 只有一个关键词。
尚未导入真实数据库；后续提供数据库文件或读取接口及表名后，通过以下接口提交映射记录。
POST /api/keyword-entry-requests：{action, entry:{a,b,c,d,e}, base_version, reason, result_id?}。
新增 base_version=0，修改/删除必须匹配当前版本；修改仅替换 e，调整联合主键需删除旧记录并新增。
现有 keywords/审批/审计表保留，五列数据由适配层存入 payload，无需重建已有数据库。
导入也应通过审批/管理员变更服务留痕，不能绕过审计直接在 UI 更新数据库。

## Excel 样例适配

实际模板“测试详情”标题合并两行；下方表头占三行，“测试对象”至“单位”各纵向合并三行，
“第一轮”占前两行，“门限、值、备注、附件链接”在第三行。程序按标签识别，不硬编码行列号。
实际报告不再兼容前期错误样例中的两行表头；单行简化演示模板单独保留。
识别 11 个业务字段：上层用例编号至单位，下层门限、值、备注、附件链接。
测试对象列纳入解析、修订、批次快照及结果导出；纵向合并时沿用首格。
旧 11 字段模板兼容读取，但缺少测试对象时不会匹配五列关键词。
纵向合并字段沿用首格；横向合并说明不扩散到“值”列。
测试详情标题后 30 行内查找表头；重复字段或多测试详情区域会明确报错，避免选择错误轮次。
目前仅支持一组轮次字段，多轮次结构需提供样例后扩展。
原始工作表导出时保留，审查结果通过领域和原始 Excel 行号对应。
