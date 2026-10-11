"""Saving or refreshing a demo profile must not invent FSP achievements."""
import pytest

from app.models import Profile, User
from test_api import candidate_data, env, login, registered


@pytest.mark.parametrize("account", ["seed_mock", "seed_not_linked", "manual_demo", "real"])
def test_fsp_achievements_require_existing_seed_provenance(env, account):
    app, client = env
    if account.startswith("seed_"):
        email = "candidate@demo.example.com" if account == "seed_mock" else "backend_python-middle@demo.example.com"
        candidate, identifier = login(app, email)
    else:
        email = account + "@example.com"
        identifier, _ = registered(app, client, email=email)
        candidate, _ = login(app, email)
        # These changes are confined to the fixture's temporary SQLite database.
        assert candidate.get("/api/candidate/profile").status_code == 200
        with app.state.session_factory() as db:
            user = db.get(User, identifier)
            user.is_demo = account == "manual_demo"
            row = db.get(Profile, identifier)
            row.fsp = {}  # A newly created manual account has no seed provenance.
            db.commit()

    expected = "synthetic_demo" if account == "seed_mock" else "not_linked"
    initial = candidate.get("/api/candidate/fsp")
    assert initial.status_code == 200, initial.text
    assert initial.json()["status"] == expected

    # Exercise both actions that previously manufactured a mock for all demo users.
    saved = candidate.put("/api/candidate/profile", json=candidate_data())
    assert saved.status_code == 200, saved.text
    assert saved.json()["fsp_status"] == expected
    refreshed = candidate.post("/api/candidate/fsp/sync")
    assert refreshed.status_code == 200, refreshed.text
    result = refreshed.json()
    assert result["status"] == expected
    assert result["verified"] is False
    if account == "seed_mock":
        assert result["achievements"]
        assert all(item["synthetic"] and item["source"] == "demo_registry" for item in result["achievements"])
    else:
        assert result["achievements"] == []

    # An explicitly supplied external ID remains unverified, never a mock achievement.
    if account != "seed_mock":
        saved = candidate.put("/api/candidate/profile", json={**candidate_data(), "fsp_id": "owner-provided-test-id"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["fsp_status"] == "unverified"
        refreshed = candidate.post("/api/candidate/fsp/sync")
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["status"] == "unverified"
        assert refreshed.json()["achievements"] == []
