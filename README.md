# 单元测试报告 AI 审查

React + TypeScript / FastAPI / SQLAlchemy / PostgreSQL / 独立 Python Worker。
支持 Windows、Linux 原生部署，无 Docker；构建后的前端由后端提供，运行时不请求 CDN 或外网。

## 已实现

- 内网鉴权适配接口、短期 Bearer Token、用户启用状态及角色实时检查；内网接入凭据和 Cookie 临时加密保存，供后台自动重登。
- .xlsx 多 Sheet 解析，每 Sheet 一个领域；跳过封面、目录和空表，识别“测试详情”后的三行表头、纵向合并单元格、Excel 超链接、常量 HYPERLINK。
- 仅筛选“测试详情”中“值”非空且不为 NA 的行（忽略首尾空白、不区分大小写，保留数值 0），附件可为空；上方测试总结及横向合并说明行不计入。兼容旧单行表头模板。公式不猜测缓存值；其他公式需转成值。保留原始文件与 SHA-256。
- 用户只操作自己的报告；管理员可读全部，不能修改或控制他人报告。
- 原始解析字段只读，可单独添加用户备注及调整关键词；领域和审查结果支持勾选多项，清除选择显示全部，后端筛选覆盖整份报告。结果选项包含实际 AI 文案，支持不同错误、无有效图片、缺少关键词、执行失败和待审查。
- 全量审查、查漏补缺、暂停/继续及重新开始；重新开始按当前选定模式创建新批次并保留旧批次。
- 关键词变更生效后，五字段匹配的测试项重新待审查；补审只选择待审查项，旧批次结果不覆盖。
- 无附件直接显示“缺少附件”、判据门限含中文显示“主观类”，均直接已审查、不调用图片或模型；无匹配关键词或关键词为空时，AI 结果直接显示“缺少关键词”，不调用审查函数、图片接口或模型，保留待审查状态。
- 持久化任务、实时进度、单报告一个活动任务（含暂停）、Worker 租约恢复及旧 Worker 写入隔离。
- 每批次保存字段和关键词快照、模型、规则版本、运行模式及 AI 原始返回。实际执行时逐项读取最新生效的五字段业务关键词，历史结果保留。
- 解析和审查使用相同表格，显示全部原始字段与 AI 门限/值/结果、用户备注，逐项刷新状态；保留结果筛选及各列表分页。
- 表格按页面宽度分配全部列，原始字段自动换行；附件显示超链接，AI 依据点击结果查看。
- 操作记录默认只展示报告上传、发起 AI 审查、下载/导出及关键词申请，显示用户名和中文操作；完整审计数据保留。
- 关键词按 a～e（测试对象、用例编号、用例名称、测试小项、指标名称）五字段精确匹配，i 为单个关键词。普通用户新增/修改/删除需审批，管理员直接生效，版本冲突保护与软删除。
- 原始 Excel 下载；支持单轮多组和横向多轮（第一轮、第二轮……）独立审查，页面区分轮次与供应商，导出在各轮右侧按对应组追加 AI 门限、值、结果和备注，保留原始合并格式及超链接。
- 保留报告上传、审查发起及完成、审批、关键词生效、权限、下载与导出等关键操作记录，逐项结果不重复写操作日志。

## 正式环境首次初始化

从本机已扩充为 A/B/C/D/E 联合键的 keyword-expanded.db 初始化新业务库 `data/review.db`，不复制旧历史：

```powershell
.venv\Scripts\python scripts/setup.py --source "D:\你的目录\keyword-expanded.db"
# 确认预览正确，先停止服务，再执行：
.venv\Scripts\python scripts/setup.py --source "D:\你的目录\keyword-expanded.db" --admin-user "你的内网用户名" --apply
```

关键词列支持 I 或 valve，可通过 --table 和 --keyword-column 指定。脚本备份并更新 .env，设置 APP_MODE=production，保留模型和令牌配置；拒绝覆盖已有业务库。填写本机真实接入函数后重建启动，页面显示“正式环境”。日常启动不会重新初始化。

完整步骤见 [正式环境初始化](docs/正式环境初始化.md)。

## 显式演示环境

需要 Python 3.11+、Node.js 20.19+ 或 22.12+。演示和正式部署均支持 SQLite；也可配置 PostgreSQL。

在根目录创建环境并安装后端：

```text
python -m venv .venv
# Windows:
.venv\Scripts\python -m pip install -r backend/requirements-dev.txt
.venv\Scripts\python scripts/setup.py --demo
.venv\Scripts\python scripts/run.py init
.venv\Scripts\python scripts/sample_excel.py

# Linux 对应使用 .venv/bin/python
```

scripts/setup.py --demo 显式生成未纳入 Git 的 .env，演示账号 demo、admin，随机密码见 DEMO_PASSWORD。

### Windows 一键启动

首次完成上面的环境和数据库初始化后，可直接双击根目录的 `start.cmd`：
自动构建前端（源码未变化时跳过），后台启动 API 和 Worker，然后访问 http://127.0.0.1:8000 。
关闭启动窗口不会停止系统；双击 `stop.cmd` 停止这两个进程。
`git pull` 更新代码后，双击 `rebuild-start.cmd` 会停止旧进程、重新构建并启动。
脚本自动定位项目目录，移动项目后无需改路径；日志和进程记录位于 `data/launcher/`，不上传 Git。
已有手动启动的 API/Worker 请先关闭，再使用一键脚本。
初次构建需要 Node.js、pnpm 和可用的依赖源/缓存；构建完成且源码未变化时，日常启动不需要构建工具或联网。
如果更新修改了后端依赖，先运行 `.venv\Scripts\python -m pip install -r backend/requirements.lock.txt`。
脚本不会重新生成 `.env` 或清空数据库；启动前自动升级辅助表、暂停约束及待审查状态。
可用 PowerShell 查看状态：`powershell -NoProfile -File scripts/manage.ps1 -Action status`。
首次启动默认仅本机访问；双击根目录的 `start-lan.cmd` 可切换为局域网访问。脚本会重建并重启本项目服务，监听 `0.0.0.0:8000`，启动后列出可访问的内网地址，其他电脑使用 `http://服务器内网IP:8000`。也可运行 `start-lan.cmd -Port 8001` 指定端口。Windows 防火墙需允许对应 TCP 端口的局域网入站访问；脚本不会自动修改防火墙。启动器会记住监听地址，后续普通启动沿用上次地址。仍可使用 `start.cmd -BindAddress 你的内网IP` 显式指定单个地址，重建时同样传给 `rebuild-start.cmd`。
成功启动后会在本地保存该地址，后续双击启动或重新构建会沿用；可再次传入 `-BindAddress` 更换地址。
可通过 `-Port 其他端口` 指定端口，停止/查看状态时需指定相同端口；非默认端口的日志放在 `data/launcher/端口/`。

分别打开两个终端启动 API 与 Worker：

```text
.venv\Scripts\python scripts/run.py api
.venv\Scripts\python scripts/run.py worker
```

前端（先安装 pnpm 11：npm install -g pnpm@11.25.0）：

```text
cd frontend
pnpm install --frozen-lockfile
pnpm run build
```

构建后重启 API，访问 http://127.0.0.1:8000 。开发时运行 pnpm run dev，访问终端显示的地址；/api 自动代理到后端。

上传 data/示例单元测试报告.xlsx，点击 AI 审查，观察进度、待人工确认结果、历史快照与导出。
**演示模式不会下载图片或调用模型，也不会产生真实“通过”判定。**

## 接入内网

实际内网接入文件放在本机 `backend/local_adapters/`，整个目录通过 `.gitignore` 排除，不上传 GitHub。
本机目录内的说明列出登录、Cookie 下载和审查函数接口；填写完成后在本机 `.env` 配置
`APP_MODE=local`、`LOCAL_AUTH_MODULE=local_adapters.login_adapter`、`LOCAL_REVIEW_MODULE=local_adapters.review_rules`
；附件主机名不受限制。正式接入推荐 APP_MODE=production，支持 SQLite review.db 或 PostgreSQL。
Cookie 失效后仅自动重登并重试一次；仍失败时停止整批审查，页面弹窗提示联系管理员。
凭据和 Cookie 使用 SECRET_KEY 派生密钥加密，默认 8 小时未更新后过期，退出登录/停用账号会移除。
不得将 SECRET_KEY 或本地接入目录强制加入 Git。

参考 [接入说明](docs/integration.md) 和 [部署说明](docs/deployment.md)。
正式接入需要脱敏 Excel 样例、关键词库结构、内网认证接口、现有凭证下载代码、审查规则及支持图片的 AI 模型。
APP_MODE=production 必须配置本地登录与审查模块，不回退到演示登录；正式 SQLite 数据库使用 review.db。

## 验证

```text
.venv\Scripts\python scripts/run.py test
cd frontend
pnpm run build
```

测试覆盖越权、管理员只读、修订冲突、快照、审批冲突、即时停用、任务租约恢复、并发领取、导出与附件地址格式校验。
SQLite 可用于正式单 Worker 部署和自动化测试；SQLite/PostgreSQL 并发、真实内网接口及实际 1500 行/30 人负载仍需在目标环境验证。

已有安装升级到表格审查版：Windows 使用 `rebuild-start.cmd` 自动补建新表；手动启动或 Linux 部署前执行
`python scripts/run.py migrate`（使用项目虚拟环境）。升级仅新增表，不重写历史报告或 AI 结果。

## 目录

- backend/app：API、模型、解析、权限、审查和 Worker
- backend/tests：业务与安全回归
- frontend/src：中文工作台
- scripts：初始化、启动和脱敏示例
- docs：接口契约、内网和离线部署

第一版数据库结构通过 python scripts/run.py init 显式创建，不会覆盖既有库；已有 SQLite 关键词不会自动导入。
原始文件、数据库、.env、真实报告及依赖安装产物不上传 GitHub。

状态支持报告所有者手动切换待审查/已审查，AI 原始结果保留；备注点击编辑，✅保存、❌清空。表格直接显示关键词并在当前页弹窗调整；关键词管理页采用上下结构及逐行列表。
