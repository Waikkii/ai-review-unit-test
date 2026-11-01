# 单元测试报告 AI 审查

React + TypeScript / FastAPI / SQLAlchemy / PostgreSQL / 独立 Python Worker。
支持 Windows、Linux 原生部署，无 Docker；构建后的前端由后端提供，运行时不请求 CDN 或外网。

## 已实现

- 内网鉴权适配接口、短期 Bearer Token、用户启用状态及角色实时检查；不存用户密码。
- .xlsx 多 Sheet 解析，每 Sheet 一个领域；跳过封面、目录和空表，识别“测试详情”后的单行/双行表头、纵向合并单元格、Excel 超链接、常量 HYPERLINK。
- 仅筛选“测试详情”中“值”非空的行（包括数值 0），附件可为空；上方测试总结及横向合并说明行不计入。兼容旧单行表头模板。公式不猜测缓存值；其他公式需转成值。保留原始文件与 SHA-256。
- 用户只读写自己的报告；管理员可读全部、仅能修订/发起自己的报告。
- 原始字段修订与版本冲突保护；审查结论人工修改第一版不开放。
- 持久化任务、实时进度、单报告一个活动任务、Worker 租约及中断恢复、逐项失败记录。
- 每批次固定字段/关键词快照、模型、规则版本、运行模式及 AI 原始返回；历史不覆盖。
- 结果中通过项默认折叠，其他项展开；结果、报告、审查历史、审批、日志支持分页。
- 关键词业务结构为 a～e 五个 TEXT 字段，a～d（测试对象、用例编号、用例名称、测试小项）全库联合唯一，e 为单个关键词；按四字段精确匹配。普通用户新增/修改/删除需审批，管理员直接生效，版本冲突保护与软删除。
- 原始 Excel 下载；导出保留原始 Sheet 并新增该批次全部审查字段和结论 Sheet。
- 上传、登录、修订、发起审查、逐项结果、审批、关键词生效、权限、下载与导出均记录审计。

## 本地演示

需要 Python 3.11+、Node.js 20.19+ 或 22.12+。数据库演示可用 SQLite，正式部署使用 PostgreSQL。

在根目录创建环境并安装后端：

```text
python -m venv .venv
# Windows:
.venv\Scripts\python -m pip install -r backend/requirements-dev.txt
.venv\Scripts\python scripts/setup.py
.venv\Scripts\python scripts/run.py init
.venv\Scripts\python scripts/sample_excel.py

# Linux 对应使用 .venv/bin/python
```

scripts/setup.py 生成未纳入 Git 的 .env，演示账号 demo、admin，随机密码见 DEMO_PASSWORD。
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

参考 [接入说明](docs/integration.md) 和 [部署说明](docs/deployment.md)。
正式接入需要脱敏 Excel 样例、关键词库结构、内网认证接口、现有凭证下载代码、审查规则及支持图片的 AI 模型。
APP_MODE=production 启动必须使用 PostgreSQL 并配置自定义集成模块；不回退到演示登录。

## 验证

```text
.venv\Scripts\python scripts/run.py test
cd frontend
pnpm run build
```

测试覆盖越权、管理员只读、修订冲突、快照、审批冲突、即时停用、任务租约恢复、并发领取、导出与链接白名单。
SQLite 用于快速自动化测试；PostgreSQL 并发和真实内网接口、实际 1500 行/30 人负载仍需目标环境验证。

## 目录

- backend/app：API、模型、解析、权限、审查和 Worker
- backend/tests：业务与安全回归
- frontend/src：中文工作台
- scripts：初始化、启动和脱敏示例
- docs：接口契约、内网和离线部署

第一版数据库结构通过 python scripts/run.py init 显式创建，不会覆盖既有库；已有 SQLite 关键词不会自动导入。
原始文件、数据库、.env、真实报告及依赖安装产物不上传 GitHub。
