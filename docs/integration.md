# 内网接入契约

登录和审查统一由被 Git 忽略的 backend/local_adapters 目录提供。

- LOCAL_AUTH_MODULE=local_adapters.login_adapter：login(username, password) 返回 {username, cookies}；fetchimage(url, cookies) 返回 [(base64_string, MIME), ...]。
- LOCAL_REVIEW_MODULE=local_adapters.review_rules：review_item(unit, keyword, attachment_url, criterion, threshold, value, fetchimage) 返回 (ai_threshold, ai_value, result) 三个字符串，并提供 RULE_VERSION。兼容旧版 (verdict, raw) 返回格式。
- attachment_url 是当前行附件入口的 str；一个入口可返回多张图片。回调类型为 Callable[[str], list[tuple[str, str]]]。
- 系统先按测试对象、用例编号、用例名称、测试小项匹配 a～d，将 e 列的单个关键词传入。无关键词直接返回“缺少关键词”。
- 附件主机名和 IP 不受白名单限制，无需配置 ATTACHMENT_HOSTS；旧配置保留也不影响访问。地址仍需为不含账号密码的有效 HTTP/HTTPS URL。图片支持 JPEG/PNG/GIF/BMP，每次最多 10 张，不限制单张图片字节大小；格式按原样传给本地审查函数。
- verdict 包含 status（passed/failed/needs_review）、explanation、evidence、suggestion、ai_threshold、ai_value；raw 为可 JSON 序列化的 dict。
- 规则变化时更新 RULE_VERSION，统一重启 API/Worker；整项执行时间应小于 LEASE_SECONDS（默认 300 秒）。
- AI 请求由本地 review_item 实现；本机 `.env` 可配置 `MODEL` 和 `APPLY_TOKEN`，在函数中通过 `from app.config import settings` 读取 `settings.model`、`settings.apply_token`。令牌由本地函数按实际接口要求使用，框架不会自动添加请求头。`AI_BASE_URL`、`AI_API_KEY`、`AI_MODEL` 继续兼容，审查记录优先使用 `MODEL`，为空时使用 `AI_MODEL`。修改配置后需重启 API 和 Worker。
- 旧适配器及其配置已移除，真实接入统一使用上述模块；原始模型结果、任务快照和至少一次调用语义保留。

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
测试对象列纳入解析、批次快照及结果导出；原始字段只读，纵向合并时沿用首格。
旧 11 字段模板兼容读取，但缺少测试对象时不会匹配五列关键词。
纵向合并字段沿用首格；横向合并说明不扩散到“值”列。
测试详情标题后 30 行内查找表头；重复字段或多测试详情区域会明确报错，避免选择错误轮次。
目前仅支持一组轮次字段，多轮次结构需提供样例后扩展。
原始工作表导出时保留，审查结果通过领域和原始 Excel 行号对应。

## 表格审查与用户备注

解析及审查使用相同列序：领域、全部原始字段、AI 门限/值/结果、用户备注、当前状态、操作。
原始行号仅用于后台追溯，不显示在工作台表格。
结果逐项提交，页面每秒刷新；pending 对应待审查，成功返回的结果对应已审查，执行异常明确显示失败。
用户备注按测试项保存，与原始 Excel 备注及 AI 结论分离，同一项在不同批次中展示当前备注。
PATCH /api/items/{id}/note：{note, version}，首次 version=0，后续使用 note_version，冲突返回 409。
仅报告本人可修改备注，管理员查看他人报告时只读；备注修改保留审计记录，不改变 AI 原始结果或报告版本。
每行均可进入关键词调整，匹配四字段后关联已有词条或新建申请；原有审批规则继续生效。

## 内网接入与自动刷新

本地文件不纳入 Git：login_adapter.py 提供 login(username, password) 和 fetchimage(url, cookies)；
review_rules.py 提供 RULE_VERSION 和 review_item(unit, keyword, attachment_url, criterion, threshold, value, fetchimage)。
每条测试项先由系统读取单位、按 a～d 匹配关键词并取 e 字符串、读取附件入口字符串，再调用该函数。
未匹配到关键词或 e 为空时，系统直接返回 missing_keyword（缺少关键词），不调用审查函数、不请求图片/模型。
该结果保留待审查资格，补齐关键词后可补审。无附件传空字符串；图片不预先下载，由审查函数内部调用系统传入的 fetchimage(url)。
该回调已绑定当前登录用户，内部调用 login_adapter.fetchimage(url, cookies)，并负责认证重试和图片校验。
两层 fetchimage 均返回 list[tuple[str, str]]，每项是一张图片的 Base64 字符串和 MIME；允许空列表，最多 10 张，不限制单张图片字节大小。MIME 支持 image/jpeg、image/png、image/gif、image/bmp 和 image/x-ms-bmp。content 由本地下载函数转换为 Base64 字符串，框架不解码、不检查二进制文件头、不重复编码，原样交给本地审查函数。
对应 Callable[[str], list[tuple[str, str]]]。一个附件入口可返回多张图片，系统不拆分 attachment_url 字符串。
login 返回 {username, cookies}，cookies 为可序列化的 dict/list；fetchimage 在 Cookie 失效时抛 CookieExpired。
公共层用当前用户的 Cookie 获取图片，遇 CookieExpired 自动重新登录一次并重试一次。
CookieExpired 是会话失效信号，不负责 HTTP 请求；带 response.status_code=401/403 的 HTTP 异常也会识别为失效。
仍失败则批次进入 blocked，保留后续 pending，不再领取该批次；API 返回 auth_session_failed，前端显示联系管理员弹窗。
不将密码、Cookie、内网 API 错误响应或原始异常文本发往浏览器、模型或日志。
重新登录后用户可继续；继续会重试认证失败的当前项，而非跳过它。
内网凭据由 Fernet 加密，密钥从本机 SECRET_KEY 派生，默认 8 小时未更新失效；API/Worker 共用数据库和密钥。
退出登录或管理员停用用户会删除其暂存凭据。普通 Bearer Token 的有效期仍单独受 TOKEN_MINUTES 控制。

## 审查模式与控制

POST /api/reports/{id}/runs：{selection_mode:"full|missing"}，不传默认 full。
full 选择全部测试项；missing 只选择当前待审查项，无待审查项返回 409。
POST /api/runs/{id}/pause、resume、restart 控制报告本人的批次；管理员不能控制别人的任务。
暂停等待当前项结束；paused 批次仍占用该报告的活动任务位置。
restart 接受 {selection_mode:"full|missing"}，按界面当前选定模式创建新批次并保留旧批次；不传时沿用该批次模式。
旧 Worker 被隔离，不能回写已经重启的批次。missing 模式若没有待审查项，拒绝重启并保留现状。
关键词申请待审批时不影响状态；管理员修改/审批生效后，匹配的项增加规则修订号并变为待审查。
批次冻结关键词与项修订号；审查过程中生效的新关键词不会被旧结果误标成已完成，需下一次补审处理。
原始批次结果保留，pending_review 表示当前关键词下仍需审查。
领域筛选只影响展示，在服务端过滤后分页，不改变全量/补审选择范围。
GET /api/reports/{id}/domains 返回整份报告的领域；items 和 run results 接受重复 domain 参数，例如 `?domain=领域一&domain=领域二`。领域之间取并集，后端筛选后分页。

审查结果支持重复 result 参数按网页实际结果文案精确筛选，例如 `?result=缺少关键词&result=执行失败`。结果之间取并集，与领域筛选取交集；没有选择时显示全部。`GET /api/runs/{id}/result-options` 返回整批数据中去重的有效结果文案，不受当前分页和领域选择限制。重新待审查的旧结论归入“待审查”，未失效的缺少关键词仍单独显示。旧 status 参数继续兼容，也支持重复 status 参数。

## 操作记录摘要

GET /api/audit 默认只返回 report.upload、run.start、report.download、run.export、keyword.request，过滤在分页之前执行。
返回 actor_username（用户名）、action_label（中文操作）、category 和 target_label（报告名/关键词对象），工作台不展示技术 ID 或原始 JSON。
登录、逐项审查等详细留痕仍在数据库保留；排障可使用 summary=false，普通用户仍只能访问本人记录。

### 扩展审查参数与三值返回

criterion、threshold、value 分别取 Excel 当前测试项的“判据门限”“门限”“值”，均保持字符串，空字段为 ""，零值为 "0"。返回示例：`return "10", "8.5", "通过"`。结果支持“通过”“不通过”“待人工确认”或 passed/failed/needs_review；未知或空结果按接口格式错误处理，不自动判为通过。AI 门限和值写入审查结果列，原始 Excel 字段保持不变。fetchimage 仍是绑定当前用户的多图下载回调，类型不变。
