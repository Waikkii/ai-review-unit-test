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
