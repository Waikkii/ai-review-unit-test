from app.config import Settings


def test_review_model_prefers_model():
    settings = Settings(_env_file=None, model="preferred-model", ai_model="legacy-model", apply_token="test-token")
    assert settings.review_model == "preferred-model"
    assert settings.apply_token == "test-token"


def test_review_model_falls_back_to_ai_model():
    settings = Settings(_env_file=None, model="", ai_model="legacy-model")
    assert settings.review_model == "legacy-model"


def test_model_and_apply_token_load_from_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MODEL=local-model\nAPPLY_TOKEN=local-token\n", encoding="utf-8")
    settings = Settings(_env_file=env)
    assert settings.model == "local-model"
    assert settings.apply_token == "local-token"


def test_relative_sqlite_and_upload_paths_are_resolved_from_project_root():
    from app.config import ROOT
    settings = Settings(_env_file=None, database_url="sqlite:///./data/review.db", data_dir="data/review_files")
    assert settings.database_url == "sqlite:///" + (ROOT / "data" / "review.db").as_posix()
    assert settings.data_dir == ROOT / "data" / "review_files"
