"""在仓库根目录执行；不覆盖已有 .env。生产部署请人工填写 .env。"""
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
env = root / ".env"
if env.exists():
    raise SystemExit(".env 已存在，未覆盖。")
(root / "data").mkdir(exist_ok=True)
database = (root / "data" / "demo.db").as_posix()
env.write_text(
    "APP_MODE=demo\nSECRET_KEY=" + secrets.token_urlsafe(48) +
    "\nDEMO_PASSWORD=" + secrets.token_urlsafe(18) +
    "\nDATABASE_URL=sqlite:///" + database + "\nBOOTSTRAP_ADMINS=\n",
    encoding="utf-8")
try:
    env.chmod(0o600)
except OSError:
    pass
print("已生成 .env（含随机演示密码）；账号 demo / admin。请勿提交该文件。")
