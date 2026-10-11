"""Public bot links never carry authentication or account linking secrets."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.integrations.routes import public_bot_url
from app.main import create_app
from app.models import LinkCode
from app.security import digest

PASSWORD = "Synthetic-Messenger-URL-Only-2026"
TOKEN = "synthetic_max_token_not_a_real_credential"


@pytest.mark.parametrize("username", ["https://evil.example/bot", "bad/user", "bad?token=value", "two@@bot", "x\nHeader", "", "x" * 65, "кириллица"])
def test_invalid_or_missing_username_never_creates_public_url(username):
    settings = SimpleNamespace(max_bot_token=TOKEN, max_bot_username=username)
    assert public_bot_url("max", settings) is None


def test_public_bot_link_is_bare_and_requires_configuration():
    settings = SimpleNamespace(max_bot_token=TOKEN, max_bot_username="@synthetic_fsp_bot")
    assert public_bot_url("max", settings) == "https://max.ru/synthetic_fsp_bot"
    settings.max_bot_token = ""
    assert public_bot_url("max", settings) is None
    assert public_bot_url("unknown", settings) is None


@pytest.fixture
def environment(tmp_path):
    settings = Settings(database_url="sqlite:///" + str(tmp_path / "url.db"), seed_demo=True,
                        demo_password=PASSWORD, max_bot_token=TOKEN,
                        max_bot_username="synthetic_fsp_bot")
    app = create_app(settings)
    with TestClient(app) as client:
        yield app, client


def login(client, email):
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()["user"]["id"]


@pytest.mark.parametrize("email", ["candidate@demo.example.com", "employer@demo.example.com"])
def test_both_roles_get_url_with_private_link_code_owned_and_hashed(environment, email):
    app, client = environment
    user_id = login(client, email)
    state = client.get("/api/integrations/status").json()
    assert state["max"] == {"configured": True, "linked": False, "url": "https://max.ru/synthetic_fsp_bot"}
    assert "telegram" not in state
    assert TOKEN not in json.dumps(state)
    result = client.post("/api/integrations/messenger/link-code", json={"platform": "max"})
    assert result.status_code == 200
    payload = result.json()
    assert payload["url"] == state["max"]["url"]
    assert payload["code"] not in payload["url"] and TOKEN not in result.text
    with app.state.session_factory() as db:
        code = db.scalar(select(LinkCode).where(LinkCode.user_id == user_id))
        assert code.id == digest(payload["code"]) and code.id != payload["code"]
        assert code.platform == "max"
    assert client.post("/api/integrations/messenger/link-code", json={"platform": "telegram"}).status_code == 422


def test_public_url_and_link_code_do_not_bypass_auth_or_csrf(environment):
    app, client = environment
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    assert client.get("/api/integrations/status").status_code == 401
    assert client.post("/api/integrations/messenger/link-code", json={"platform": "max"}, headers={"X-CSRF-Token": csrf}).status_code == 401
    login(client, "candidate@demo.example.com")
    assert client.post("/api/integrations/messenger/link-code", json={"platform": "max"}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    app.state.settings.max_bot_username = "https://evil.example/private"
    assert client.get("/api/integrations/status").json()["max"]["url"] is None
    assert client.post("/api/integrations/messenger/link-code", json={"platform": "max"}).json()["url"] is None
