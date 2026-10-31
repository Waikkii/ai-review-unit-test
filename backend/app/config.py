from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    app_mode: str = "production"
    database_url: str = "postgresql+psycopg://review:review@127.0.0.1/review"
    secret_key: str = ""
    data_dir: Path = ROOT / "data"
    demo_password: str = ""
    bootstrap_admins: str = ""
    integration_module: str = "app.integrations"
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    attachment_hosts: str = ""
    max_upload_mb: int = 20
    max_items: int = 20000
    token_minutes: int = 60
    lease_seconds: int = 300

    def validate_runtime(self):
        if len(self.secret_key) < 32:
            raise RuntimeError("SECRET_KEY 必须至少 32 个字符，请运行 scripts/setup.py")
        if self.lease_seconds < 300:
            raise RuntimeError("LEASE_SECONDS 不能小于 300 秒")
        if self.app_mode not in {"demo", "production"}:
            raise RuntimeError("APP_MODE 必须是 demo 或 production")
        if self.app_mode == "demo" and len(self.demo_password) < 12:
            raise RuntimeError("演示密码至少 12 个字符")
        if self.app_mode == "production" and self.integration_module == "app.integrations":
            raise RuntimeError("生产模式必须配置内网 INTEGRATION_MODULE")
        if self.app_mode == "production" and not self.database_url.startswith("postgresql"):
            raise RuntimeError("生产模式必须使用 PostgreSQL")

settings = Settings()
