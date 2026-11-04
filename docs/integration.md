# 内网接入契约

登录和审查统一由被 Git 忽略的 backend/local_adapters 目录提供。

- LOCAL_AUTH_MODULE=local_adapters.login_adapter：login(username, password) 返回 {username, cookies}；fetchimage(url, cookies) 返回 [(base64_string, MIME), ...]。
- LOCAL_REVIEW_MODULE=local_adapters.review_rules：review_item(unit, keyword, attachment_url, criterion, threshold, value, fetchimage) 返回 (ai_threshold, ai_value, result) 三个字符串，并提供 RULE_VERSION。兼容旧版 (verdict, raw) 返回格式。
- attachment_url 是当前行附件入口的 str；一个入口可返回多张图片。回调类型为 Callable[[str], list[tuple[str, str]]]。
- 自动判定优先级：无附件链接直接返回“缺少附件”；否则判据门限含中文汉字时返回“主观类”。两种情况均直接已审查，不调用本地模型/图片函数。其余测试项再按四字段匹配关键词，无关键词返回“缺少关键词”并保留待审查资格。
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

实际模板“测试详情”标题可占一行或合并多行；下方表头占三行，“测试对象”至“单位”各纵向合并三行，
数据分组标题占前两行，“门限、值、备注、附件链接”在第三行。分组标题文字不作约束，可以是“第一轮”、其他名称或空白；程序按公共字段右侧最左数据区的合并结构和第三行字段识别，不按标题名称选择区域，也不硬编码行列号。
表头标题和字段名识别时统一忽略排版空格、换行及零宽空白，但不改业务内容、原始字段值或导出原文。识别失败时返回具体缺失字段、错位坐标或实际合并范围，三行合并结构本身仍严格校验。
实际报告不再兼容前期错误样例中的两行表头；单行简化演示模板单独保留。
识别 11 个业务字段：上层用例编号至单位，下层门限、值、备注、附件链接。
测试对象列纳入解析、批次快照及结果导出；原始字段只读，纵向合并时沿用首格。
旧 11 字段模板兼容读取，但缺少测试对象时不会匹配五列关键词。
纵向合并字段沿用首格；横向合并说明不扩散到“值”列。
测试详情标题后 30 行内查找表头；不完整的重复字段或多测试详情区域会明确报错，避免选择错误轮次。
第一轮下支持多组完整的“门限、值、备注、附件链接”，每组“值”非空即生成一个独立审查项（数值 0 也保留），互不补用其他组的值或附件。同一行可对应多个项，按源行号和从左到右的组序排序。供应商等横向分组标签（如 A/B、K/J）随各组保存，网页在领域下方显示以区分同一行的各组；不把这些标签混入本地 review_item 业务入参。
新表 item_sources 保存 item_id、group_index（从 0 起）、group_label。通过现有 migrate 启动升级创建；已有三行表头的单组报告没有记录时按第 0 组处理，无需重置数据库。清除报告或历史时一并清理此表。
导出保留原 Sheet、原始单元格和超链接，在第一轮右侧追加“AI审查”，不再新增汇总 Sheet。每个原始四列组对应一个“门限、值、结果、备注”四列组；复制表头及横向分组的合并格式、样式、列宽和标签，按领域、源行号及组序写入结果。未参与当前批次的项结果留空；未执行项的 AI 门限、值和结果留空，用户备注独立保留。结果单元格支持缩小字体以适应列宽。单行简化演示模板使用 AI门限、AI值、AI结果、AI备注列。

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
该结果保留待审查资格，补齐关键词后可补审。无附件直接已审查并返回“缺少附件”；判据门限含中文时已审查并返回“主观类”，均不进入本地函数。有附件且为非主观类时，图片不预先下载，由审查函数内部调用系统传入的 fetchimage(url)。
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

criterion、threshold、value 分别取 Excel 当前测试项的“判据门限”“门限”“值”，均保持字符串，空字段为 ""，零值为 "0"。返回示例：`return "10", "8.5", "通过"`。结果文案去除首尾空白后，恰好等于“通过”则为通过（passed）；包含“判断错误”则为未通过（failed）；其余文案均为待人工确认（needs_review），包括无有效图片、其他错误说明和空结果。原文保留在 raw.result，非空原文同时保存在 explanation，供展示和筛选。AI 门限和值写入审查结果列，原始 Excel 字段保持不变。fetchimage 仍是绑定当前用户的多图下载回调，类型不变。

系统自身的缺少关键词、执行异常、尚未执行仍分别使用 missing_keyword、error、pending，保持原有流程；上述文案映射用于本地 review_item 返回的三值结果。旧版 (verdict, raw) 返回接口仍按其明确的内部 status 校验。

### 状态、备注和当前页关键词编辑

`PATCH /api/items/{id}/review-state` 请求示例：`{"state":"reviewed","revision":2,"reviewed_revision":0}`。
state 仅允许 pending/reviewed，对应待审查/已审查；revision 和 reviewed_revision 从 items 或 results 返回的 review_revision、reviewed_revision 读取。仅报告所有者可修改，管理员不能修改他人报告。活动审查期间禁止修改；版本冲突返回 409。手动状态只影响待审查资格，不改 AI 原始结果；设为待审查后会纳入查漏补缺。

备注默认以文本显示，点击后通过浮动编辑框修改，不撑高表格行；输入框下方的“确认”保存备注，“清除”清空并保存；Escape 或点击外部放弃本次未保存编辑。继续使用原 note_version 冲突校验。

测试项与审查结果支持每页 10、20、50、100 条，显示当前页、总页数及筛选后的总条数。切换条数或筛选条件时回到第一页。`GET /reports/{id}/items` 和 `GET /runs/{id}/results` 加 `include_total=true` 返回 `{ "items": [...], "total": 150 }`，不加此参数仍返回原数组；总数在分页前按完整筛选条件统计。审查历史在前端展示完整批次列表，不提供分页按钮。

领域关键词的“当前关键词”各列表头提供筛选菜单：搜索选项、复选多个值、确定后生效；同列选项取并集，多列条件取交集，选项根据其他列筛选联动。搜索后确定会仅保留搜索结果中勾选的值。支持清除此列及清除全部筛选，筛选覆盖接口返回的完整关键词库，不受表格滚动位置影响。

报告表格最后一列展示当前关键词并提供编辑按钮，在当前页面弹窗修改；四个匹配字段只读，可编辑关键词和原因。复用 keyword-entry-requests：普通用户提交审批，管理员直接生效，生效后相关测试项恢复待审查。关键词管理页为上方编辑表单、下方逐行关键词表格，审批记录在其后。

页面交互：待审查显示橙色，已审查显示绿色；关键词编辑图标位于关键词右侧。上传解析 Excel 时显示全屏加载蒙版，页面通过 inert 禁用操作，成功或失败后均解除。页面精简操作说明，详细规则保留在文档中。

历史批次列表显示发起时间（浏览器本地时区，精确到秒）、短批次号、状态与进度。导出只为本批次实际包含结果的 Sheet 添加 AI 区，其余 Sheet 原样保留；实际报告的解析与导出统一要求新版三行表头：“测试对象”至“单位”各纵向合并三行，数据分组标题占前两行（文字不限），四列明细在第三行。旧版两行合并表头及不符合新版合并结构的报告不做兼容，需使用新版原始文件重新上传审查。下载失败时网页显示接口具体原因；导出原始文件缺失返回 404，表头/分组等校验失败返回 422，其他异常返回含批次号的 500。上述导出异常均将批次号、报告号及堆栈写入 API 错误日志 `data/launcher/api.err.log`。

报告选择列表及各历史批次右侧显示“×”，仅所有者可删除，点击后确认再执行。报告删除复用 `DELETE /api/reports/{id}`，清除数据库中该报告的全部项、批次、结果和备注，并删除原始 Excel。`DELETE /api/runs/{id}` 仅删除一个批次及其 Result、ResultMeasurements、ResultWork、RunControl，保留报告、原始文件、备注和其他批次；当前引用被删结果的项清除引用、递增审查版本并恢复待审查，不自动套用旧批次结果。关键词申请解除到被删结果的关联，关键词及审批内容保留。报告存在排队、运行或暂停中的批次时两种删除均禁止；管理员不能删除他人报告或批次。批次删除保留一条中文操作记录。

`POST /api/runs/{id}/abort` 立即将排队中、审查中、暂停中、已暂停或认证阻塞的批次标记为 `aborted`（已中止），清除 Worker 租约，不创建新批次。中止前已写入的结果保留，未完成项继续待审查；该历史随后可删除，原始报告也可删除（前提是没有其他活动批次）。已发出的外部请求可能自行返回，但 Worker 在领取下一项和写回结果前均通过租约及状态条件更新隔离旧调用，批次或报告被删除后也不会补写结果或恢复记录。中止后不能继续原批次，可按当前选择模式重新开始；重复中止不重复记录操作。页面保留原暂停/继续/重新开始，并在可中止状态下显示“中止”按钮。

查漏补缺页面继续显示整份报告：开始时已审查的项保留原结论、AI 门限和值；只把待审查项加入本批次执行队列。每项每批次执行一次，缺少关键词、执行异常等仍待审查的项在修复原因后再发起查漏补缺，不自动循环重试。进度的 total/completed 仅统计本批次执行项，表格分页总数统计全部展示项。

查漏补缺以当前选择的历史为基础。`POST /api/reports/{id}/runs` 可传 `base_run_id`；`POST /api/runs/{id}/restart` 直接以路径中的历史为基础。未选择历史时使用报告当前结果。所选历史仍未完成或失败的项，以及手动改成待审查、关键词变化失效的项，进入本轮队列；重审项清空旧值并写入本轮新结果。

继承的已审查结果在新批次中保存独立的 Result、ResultMeasurements、ResultWork 副本，由 run_retained_items 标记为继承项，不加入 Worker 队列或进度统计。删除来源历史不再影响新批次的展示、状态和导出。启动升级会把仍可读取的旧引用转换为独立副本；已经删除来源而丢失的结果无法恢复。删除新批次或报告时清理其副本。

网页对 `/runs/{id}/results`、`/result-options` 和 `/export` 使用 `include_reviewed=true`；仅在查漏补缺模式下合并当前批次结果与保留的已审查结果，筛选、分页和导出一致。默认未带参数时仍返回原本批次工作项，保持接口兼容。查漏补缺页面导出包含保留的已审查结果；用户手动标记已审查但没有 AI 结果的项也显示，AI 列保持空白。

测试项和审查结果表格的每个明细列均提供搜索、多选、空白值和清除此列筛选；领域、审查结果筛选移入对应表头。`GET /api/reports/{id}/table-view` 接收可选的 `run_id`、JSON 格式 `filters`、`offset`、`limit`，返回 `items/total/options`。筛选对整份报告生效后再分页，各列选项按其他列的筛选条件联动（自身筛选不缩小自身选项），多列取交集，同列多选取并集，空数组表示不选任何值。键为 `domain`、`field:原始列名`、`ai_threshold`、`ai_value`、`result`、`note`、`state`、`keyword`；空白值使用空字符串。审查页面读取状态后再请求此完整表格视图，导出仍导出完整批次。

审查函数抛出异常时，Worker 以 ERROR 级别记录 `Review failed`，包含 run（批次号）、result（结果号）、domain（领域）、type（异常类型）、exc（异常内容）及完整异常堆栈。堆栈中的 `File "路径", line 行号, in 函数名` 可定位本地审查函数的出错位置，并保留异常原因链。Windows 启动器写入 `data/launcher/worker.err.log`（自定义端口为 `data/launcher/<端口>/worker.err.log`）；使用 `Get-Content .\data\launcher\worker.err.log -Tail 100 -Wait` 实时查看。网页仍显示统一失败提示，不把这些诊断内容额外存入数据库。更新后需要重启 Worker；历史异常不会补打。
