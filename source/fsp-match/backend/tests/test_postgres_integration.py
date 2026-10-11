"""Real PostgreSQL concurrency checks in a disposable, dedicated test schema.

Opt-in: FSP_TEST_DATABASE_URL=postgresql+psycopg://.../fsp_match_validation.
No SQLite fallback. Only our generated fsp_test_<32hex> schema can be dropped.
The API DSN search_path excludes public, so reflection/queries cannot fall back
to another application's tables. Credentials are never printed.
"""
import os
import re
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from app.config import Settings
from app.main import create_app
from app.models import Attempt, ContactGrant, Invitation, Notification, Profile

TEST_PASSWORD = "Synthetic-PG-Only-2026"
_URL = os.environ.get("FSP_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not _URL, reason="Dedicated PostgreSQL FSP_TEST_DATABASE_URL not configured")


@pytest.fixture
def pg_app():
    url = make_url(_URL)
    if url.get_backend_name() != "postgresql":
        pytest.fail("PostgreSQL integration fixture refuses non-PostgreSQL URL")
    if url.database != "fsp_match_validation":
        pytest.fail("PostgreSQL integration fixture requires dedicated fsp_match_validation database")
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    schema = "fsp_test_" + uuid.uuid4().hex
    assert re.fullmatch(r"fsp_test_[0-9a-f]{32}", schema)
    admin = create_engine(url, pool_pre_ping=True)
    quoted = admin.dialect.identifier_preparer.quote(schema)
    with admin.begin() as connection:
        connection.execute(text("CREATE SCHEMA " + quoted))
    # Exclude public completely: even pre-existing same-name tables must not be
    # considered visible by metadata reflection or unqualified foreign keys.
    test_url = url.update_query_dict({"options": "-csearch_path=" + schema})
    app = create_app(Settings(database_url=test_url.render_as_string(hide_password=False), environment="development", registration_enabled=True, seed_demo=True, demo_password=TEST_PASSWORD, telegram_bot_token="", max_bot_token="", llm_base_url="", llm_api_key="", llm_model=""))
    try:
        with TestClient(app) as lifespan_client:
            with app.state.engine.connect() as connection:
                assert connection.scalar(text("SELECT current_schema()")) == schema
                assert connection.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_schema = :schema"), {"schema": schema}) >= 20
            assert lifespan_client.get("/api/ready").status_code == 200
            yield app
    finally:
        # Engine pools close via lifespan before this exact-schema cleanup.
        # No DROP DATABASE, public table drop or wildcard target is permitted.
        assert re.fullmatch(r"fsp_test_[0-9a-f]{32}", schema)
        with admin.begin() as connection:
            connection.execute(text("DROP SCHEMA " + quoted + " CASCADE"))
        admin.dispose()


def account(app, email):
    client = TestClient(app)
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    response = client.post("/api/auth/login", json={"email": email, "password": TEST_PASSWORD}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.status_code
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client, response.json()["user"]["id"]


def clone_client(app, source):
    client = TestClient(app)
    client.cookies.set("fsp_session", source.cookies.get("fsp_session"))
    client.headers["X-CSRF-Token"] = source.headers["X-CSRF-Token"]
    return client


def parallel(count, operation):
    barrier = threading.Barrier(count)
    def call(index):
        barrier.wait(timeout=20)
        return operation(index)
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(call, range(count)))


def trusted_answers(app, identifier):
    with app.state.session_factory() as db:
        form = db.get(Attempt, identifier).form
        # Test-only oracle read. Production response never contains this field.
        return {qid: oracle["expected"] for qid, oracle in form["_oracle"].items()}


def test_postgres_eight_parallel_starts_keep_single_attempt_and_cooldown(pg_app):
    client, uid = account(pg_app, "candidate@demo.example.com")
    employer, _ = account(pg_app, "employer@demo.example.com")
    filtered = employer.get("/api/employer/candidates",params={"specialization":"backend_python","grade":"junior","skill":"PYTHON","fsp":True,"page_size":12})
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1 and filtered.json()["items"][0]["id"] == uid
    assert employer.get("/api/employer/candidates",params={"skill":"unknown_verified_skill"}).json()["total"] == 0
    body = {"specialization": "backend_python", "grade": "middle"}
    def start(_):
        response = clone_client(pg_app, client).post("/api/candidate/attempts", json=body)
        assert response.status_code == 201, response.status_code
        return response.json()["id"]
    ids = parallel(8, start)
    assert len(set(ids)) == 1
    with pg_app.state.session_factory() as db:
        rows = list(db.scalars(select(Attempt).where(Attempt.user_id == uid, Attempt.specialization == "backend_python", Attempt.grade == "middle")))
        assert len(rows) == 1 and rows[0].status == "in_progress"
    assert client.post(f"/api/candidate/attempts/{ids[0]}/submit", json={"answers": {}}).status_code == 200
    assert client.post("/api/candidate/attempts", json=body).status_code == 409
    with pg_app.state.session_factory() as db:
        assert len(list(db.scalars(select(Attempt).where(Attempt.user_id == uid)))) == 1


def test_postgres_parallel_submit_retains_categories_and_higher_grade_evidence(pg_app):
    client, uid = account(pg_app, "candidate@demo.example.com")
    # Remove synthetic verification for this test so only actual checker results
    # can produce the asserted categories.
    with pg_app.state.session_factory() as db:
        db.get(Profile, uid).verification = {}
        db.commit()
    first = client.post("/api/candidate/attempts", json={"specialization": "backend_python", "grade": "middle"}).json()
    second = client.post("/api/candidate/attempts", json={"specialization": "frontend_ts", "grade": "junior"}).json()
    attempts = [first, second]
    answers = [trusted_answers(pg_app, x["id"]) for x in attempts]
    def submit(index):
        response = clone_client(pg_app, client).post(f"/api/candidate/attempts/{attempts[index]['id']}/submit", json={"answers": answers[index]})
        assert response.status_code == 200, response.status_code
        assert response.json()["result"]["passed"]
        return response.json()
    parallel(2, submit)
    with pg_app.state.session_factory() as db:
        verification = db.get(Profile, uid).verification
        assert verification["backend_python"]["grade"] == "middle"
        assert verification["frontend_ts"]["grade"] == "junior"
        highest = dict(verification["backend_python"])
    # A fresh lower-level pass and a different-category upgrade are concurrent.
    # Neither may erase the other category or replace Middle with Junior evidence.
    third = client.post("/api/candidate/attempts", json={"specialization": "backend_python", "grade": "junior"}).json()
    fourth = client.post("/api/candidate/attempts", json={"specialization": "frontend_ts", "grade": "middle"}).json()
    attempts = [third, fourth]
    answers = [trusted_answers(pg_app, x["id"]) for x in attempts]
    parallel(2, submit)
    with pg_app.state.session_factory() as db:
        verification = db.get(Profile, uid).verification
        for key in ["grade", "score", "verified_at", "version", "attempt_id"]:
            assert verification["backend_python"][key] == highest[key]
        assert verification["backend_python"]["by_grade"]["junior"]["attempt_id"] == third["id"]
        assert verification["frontend_ts"]["grade"] == "middle"
        assert verification["frontend_ts"]["attempt_id"] == fourth["id"]


def test_postgres_parallel_invite_accept_contact_grant_and_company_privacy(pg_app):
    employer, _ = account(pg_app, "employer@demo.example.com")
    other_company, _ = account(pg_app, "second@demo.example.com")
    candidate, cid = account(pg_app, "candidate@demo.example.com")
    with pg_app.state.session_factory() as db:
        profile = db.get(Profile, cid)
        profile.data = {**profile.data, "contacts": {"phone": "+7 900 111-22-33", "telegram": "@private_pg_candidate", "website": "https://private-pg.example.com"}}
        db.commit()
    need = employer.get("/api/employer/needs").json()["items"][0]
    payload = {"need_id": need["id"], "candidate_id": cid, "message": "Предлагаем обсудить синтетическую задачу", "contact_method": "Личный кабинет", "salary_min": 90000, "salary_max": 180000}
    def send(_):
        response = clone_client(pg_app, employer).post("/api/employer/invitations", json=payload)
        assert response.status_code == 201, response.status_code
        assert "contacts" not in response.json()["candidate"]
        return response.json()["id"]
    invitation_ids = parallel(8, send)
    assert len(set(invitation_ids)) == 1
    with pg_app.state.session_factory() as db:
        assert len(list(db.scalars(select(Invitation)))) == 1
        assert len(list(db.scalars(select(Notification).where(Notification.kind == "invitation")))) == 1
        assert len(list(db.scalars(select(ContactGrant)))) == 0
    # Two distinct needs make two concurrent accepted events race to create the
    # first grant for the same company+candidate; one unique grant must survive.
    need_body = {k: v for k, v in need.items() if k not in {"id", "company_id", "created_at", "updated_at"}}
    need_body["title"] = "Вторая синтетическая потребность"
    second_need = employer.post("/api/employer/needs", json=need_body).json()
    second_invitation = employer.post("/api/employer/invitations", json={**payload, "need_id": second_need["id"]}).json()["id"]
    ids = [invitation_ids[0], second_invitation]
    def accept(index):
        response = clone_client(pg_app, candidate).post(f"/api/candidate/invitations/{ids[index]}/respond", json={"decision": "accepted"})
        assert response.status_code == 200, response.status_code
        assert response.json()["status"] == "accepted"
        return response.json()
    parallel(2, accept)
    with pg_app.state.session_factory() as db:
        grants = list(db.scalars(select(ContactGrant).where(ContactGrant.candidate_id == cid)))
        assert len(grants) == 1
        assert len(list(db.scalars(select(Notification).where(Notification.kind == "invitation_response")))) == 2
    granted = employer.get(f"/api/candidates/{cid}").json()
    assert granted["contacts"]["phone"] == "+7 900 111-22-33"
    private = other_company.get(f"/api/candidates/{cid}")
    assert "contacts" not in private.json() and "private_pg_candidate" not in private.text
    # Repeated concurrent terminal clicks cannot duplicate grants/notifications.
    parallel(2, accept)
    with pg_app.state.session_factory() as db:
        assert len(list(db.scalars(select(ContactGrant).where(ContactGrant.candidate_id == cid)))) == 1
        assert len(list(db.scalars(select(Notification).where(Notification.kind == "invitation_response")))) == 2
