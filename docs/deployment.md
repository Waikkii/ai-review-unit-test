# 原生与离线部署

## 演示 / 生产

演示使用随机密码账号与 SQLite；生产必须设置：
APP_MODE=production
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST/DATABASE
SECRET_KEY=至少32个随机字符
INTEGRATION_MODULE=corp.adapters
BOOTSTRAP_ADMINS=首次管理员的内网账号
AI_BASE_URL=http://内网模型地址/v1
AI_MODEL=视觉模型名
AI_API_KEY=内网密钥
ATTACHMENT_HOSTS=内网图片主机名

在 PostgreSQL 创建数据库和专用账号，授予首次建表权限。执行一次 init 后，API/Worker 使用仅允许业务表所需读写的运行账号。
审计表 audit 应在数据库权限层禁止运行账号 UPDATE/DELETE，以加强追加记录约束。
第一版通过 create_all 初始化 v1；未来结构变更需配套迁移，create_all 不用于升级旧表。

生产环境同目录共享 .env、data、数据库配置。原始 Excel 文件留在服务端磁盘，不随意清理。
Windows 通过 NTFS ACL 仅向服务账号开放 .env、data；Linux .env 权限 600，data 目录 700。
数据备份同时包含 PostgreSQL 与 data 原始文件，定期验证恢复。磁盘或数据库备份可包含敏感报告，遵循内网管理要求。
所有 Worker 使用同一发布版本与集成配置。默认一个 Worker，可增加进程以同时审查不同报告；同一报告串行审查。
任务租约过期后恢复，外部 AI 为至少一次调用；租约必须长于集成单项处理时间。
一次任务存在多个项目错误时其余项目继续，最终 partial_failed；修复接口后用户可新建批次，旧批次不变。

## Windows

安装 Python、PostgreSQL；在有网络的构建机准备前端。
使用 .venv/Scripts/python.exe scripts/run.py init，然后分别通过受管服务启动：
.venv/Scripts/python.exe scripts/run.py api
.venv/Scripts/python.exe scripts/run.py worker
使用组织认可的 Windows 服务管理方式配置自动恢复。工作目录设为仓库根目录。

## Linux

对应使用 .venv/bin/python。用 systemd 创建两个独立 service：
WorkingDirectory=/opt/ai-review
ExecStart=/opt/ai-review/.venv/bin/python /opt/ai-review/scripts/run.py api
Worker 服务把 api 改为 worker。配置 User 为专用非 root 账号，Restart=on-failure。
API 默认仅绑定 127.0.0.1:8000，由内网反向代理提供 HTTPS、请求大小限制和登录速率限制。
如需绑定内网网卡，直接在 backend 目录运行 uvicorn app.main:app --host 内网地址 --port 8000。
禁止使用 --host 0.0.0.0 把服务无意暴露到不受控网段。

## 离线依赖

在与目标机器相同 OS、CPU 和 Python 小版本的联网构建机：

python -m pip download -r backend/requirements.lock.txt -d wheelhouse
cd frontend
pnpm install --frozen-lockfile
pnpm run build

将源码、wheelhouse、frontend/dist、Python 安装包、PostgreSQL 安装包拷入内网。
运行：
python -m venv .venv
.venv/Scripts/python -m pip install --no-index --find-links wheelhouse -r backend/requirements.lock.txt

Linux 替换 Python 路径。目标机运行已构建 frontend/dist 不需要 Node 或 npm。
不要用 Windows wheelhouse 安装 Linux。依赖锁文件来源于开发验证环境；不同平台先在同平台构建机验证。
不要把 .env、data、凭据或实际报告加入代码仓库/构建产物。
前端未使用远程字体、脚本或 CDN；运行时仅访问配置的内网服务。
不要在共享计算机暴露 FastAPI /docs；反向代理可禁用 /docs /redoc /openapi.json。

## 验收待办

实际 Excel 模板的标题/合并/链接规则、原有 SQLite 库导入、内网认证与凭据生命周期、视觉模型返回均需在提供接口后联调。
本地测试未代替 PostgreSQL 多进程故障恢复及 30 用户/1500 项负载验收。
当前无结论人工覆盖入口、无取消任务、无图片归档；并非所有候选功能均已开放。
