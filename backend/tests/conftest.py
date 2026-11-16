import os
from pathlib import Path
import pytest

root = Path(__file__).resolve().parents[2]
test_dir = root / "data" / "tests"
test_dir.mkdir(parents=True, exist_ok=True)
os.environ.update(APP_MODE="demo", SECRET_KEY="test-secret-" * 5, DEMO_PASSWORD="test-password-123",
                  DATABASE_URL="sqlite:///" + (test_dir / "test.db").as_posix(), DATA_DIR=str(test_dir))
from fastapi.testclient import TestClient
from app.main import app
from app.db import Base, engine
from app import models

@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(app) as c:
        yield c

@pytest.fixture
def headers(client):
    def login(name="demo"):
        response = client.post("/api/auth/login", json={"username": name, "password": "test-password-123"})
        assert response.status_code == 200, response.text
        # 现有业务用例显式准备已授权账号；首次登录权限规则由专门用例验证。
        from app.db import Session
        from app.models import User
        with Session() as db:
            user = db.get(User, response.json()['user']['id'])
            if user.role == 'none':
                user.role = 'user'
                db.commit()
        return {"Authorization": "Bearer " + response.json()["access_token"]}
    return login
