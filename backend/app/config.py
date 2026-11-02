from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator

ROOT = Path(__file__).resolve().parents[2]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    app_mode: str = "production"
    database_url: str = "sqlite:///./data/review.db"
    secret_key: str = ""
    data_dir: Path = ROOT / "data"
    demo_password: str = ""
    bootstrap_admins: str = ""
    local_auth_module: str = ""
    local_review_module: str = ""
    credential_ttl_seconds: int = 28800
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    model: str = ""
    apply_token: str = ""
    attachment_hosts: str = ""  # 兼容旧配置，不再限制附件主机名。
    max_upload_mb: int = 20
    max_items: int = 20000
    token_minutes: int = 60
    lease_seconds: int = 300

    @field_validator("database_url")
    @classmethod
    def absolute_sqlite_path(cls, value):
        from sqlalchemy.engine import make_url
        url = make_url(value)
        if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
            path = Path(url.database)
            if not path.is_absolute():
                url = url.set(database=(ROOT / path).resolve().as_posix())
                return url.render_as_string(hide_password=False)
        return value

    @field_validator("data_dir")
    @classmethod
    def absolute_data_dir(cls, value):
        return value if value.is_absolute() else (ROOT / value).resolve()

    @property
    def review_model(self) -> str:
        return self.model or self.ai_model

    def validate_runtime(self):
        if len(self.secret_key) < 32:
            raise RuntimeError("SECRET_KEY 必须至少 32 个字符，请运行 scripts/setup.py")
        if self.lease_seconds < 300:
            raise RuntimeError("LEASE_SECONDS 不能小于 300 秒")
        if self.app_mode not in {"demo", "local", "production"}:
            raise RuntimeError("APP_MODE 必须是 demo、local 或 production")
        if self.app_mode in {"local", "production"} and not (self.local_auth_module and self.local_review_module):
            raise RuntimeError("必须配置 LOCAL_AUTH_MODULE 和 LOCAL_REVIEW_MODULE")
        if self.app_mode == "demo" and len(self.demo_password) < 12:
            raise RuntimeError("演示密码至少 12 个字符")
        if self.app_mode == "production" and not self.database_url.startswith(("postgresql", "sqlite")):
            raise RuntimeError("正式模式支持 SQLite 或 PostgreSQL")

settings = Settings()
