"""首次初始化：默认预览正式关键词导入；--apply 创建新库并切换配置。"""
import argparse
from datetime import datetime
from pathlib import Path
import re
import secrets
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def configure_environment(text, target, admin_user):
    from dotenv import dotenv_values
    from io import StringIO

    values = {key.upper(): value for key, value in dotenv_values(stream=StringIO(text)).items()}
    admins = admin_user or values.get("BOOTSTRAP_ADMINS", "")
    if not admins or not admins.strip():
        raise ValueError("请用 --admin-user 指定实际内网管理员用户名")
    updates = {
        "APP_MODE": "production",
        "DATABASE_URL": "sqlite:///" + Path(target).resolve().as_posix(),
        "DATA_DIR": (ROOT / "data" / "review_files").as_posix(),
        "SECRET_KEY": values.get("SECRET_KEY") or secrets.token_urlsafe(48),
        "BOOTSTRAP_ADMINS": admins,
        "LOCAL_AUTH_MODULE": values.get("LOCAL_AUTH_MODULE") or "local_adapters.login_adapter",
        "LOCAL_REVIEW_MODULE": values.get("LOCAL_REVIEW_MODULE") or "local_adapters.review_rules",
        "MODEL": values.get("MODEL") or values.get("AI_MODEL") or "",
        "APPLY_TOKEN": values.get("APPLY_TOKEN") or "",
        "ATTACHMENT_HOSTS": values.get("ATTACHMENT_HOSTS") or "",
    }
    if len(updates["SECRET_KEY"]) < 32:
        raise ValueError("已有 SECRET_KEY 不足 32 字符，请先修正配置")
    for key, value in updates.items():
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise ValueError(f"{key} 配置值含非法换行或控制字符")
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        line = key + "='" + escaped + "'"
        pattern = r"^\s*(?:export\s+)?" + re.escape(key) + r"\s*=.*$"
        if re.search(pattern, text, flags=re.M | re.I):
            text = re.sub(pattern, lambda match: line, text, flags=re.M | re.I)
        else:
            text = text.rstrip("\n") + "\n" + line + "\n"
    text = re.sub(r"^\s*(?:export\s+)?DEMO_PASSWORD\s*=.*\n?", "", text, flags=re.M | re.I)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "test.db")
    parser.add_argument("--table")
    parser.add_argument("--keyword-column", help="关键词列名；默认检测 I 或 valve")
    parser.add_argument("--target", type=Path, default=ROOT / "data" / "review.db")
    parser.add_argument("--admin-user", help="首次管理员的实际内网用户名")
    parser.add_argument("--apply", action="store_true", help="实际创建正式数据库并更新 .env")
    parser.add_argument("--demo", action="store_true", help="显式创建演示环境，不导入关键词")
    args = parser.parse_args()
    env = ROOT / ".env"
    if args.demo:
        if env.exists():
            parser.error(".env 已存在，未覆盖")
        (ROOT / "data").mkdir(exist_ok=True)
        with env.open("x", encoding="utf-8") as stream:
            stream.write("APP_MODE=demo\nSECRET_KEY=" + secrets.token_urlsafe(48) +
                         "\nDEMO_PASSWORD=" + secrets.token_urlsafe(18) +
                         "\nDATABASE_URL=sqlite:///" + (ROOT / "data" / "demo.db").as_posix() +
                         "\nBOOTSTRAP_ADMINS=\nMODEL=\nAPPLY_TOKEN=\n")
        env.chmod(0o600)
        print("已创建显式演示配置；未执行正式初始化。")
        return
    from app.bootstrap import create_business_database, read_seed

    try:
        source = args.source.resolve(strict=True)
        target = args.target.resolve()
        if source == target or target.exists():
            raise ValueError("目标必须是不存在的新数据库，不能覆盖源库或已有业务库")
        table, payloads = read_seed(source, args.table, args.keyword_column)
        print(f"源表：{table}；有效关键词：{len(payloads)}；目标：{target}")
        if not args.apply:
            print("仅预览；未创建数据库，未修改 .env。添加 --apply --admin-user 用户名 后执行。")
            return
        text = env.read_text(encoding="utf-8-sig") if env.exists() else ""
        new_text = configure_environment(text, target, args.admin_user)
        backup = None
        if env.exists():
            directory = ROOT / "data" / "config_backups"
            directory.mkdir(parents=True, exist_ok=True)
            backup = directory / ("env-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".bak")
            shutil.copy2(env, backup)
        create_business_database(target, payloads)
        temporary = env.with_name(".env.initializing")
        try:
            temporary.write_text(new_text, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(env)
        except Exception:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        print("已创建 review 正式业务库，并设置 APP_MODE=production。历史记录为空，源库及旧库未修改。")
        if backup:
            print("旧配置备份：", backup)
        print("启动前完成本地登录、图片下载和审查函数，并核对 MODEL、APPLY_TOKEN、ATTACHMENT_HOSTS。")
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
