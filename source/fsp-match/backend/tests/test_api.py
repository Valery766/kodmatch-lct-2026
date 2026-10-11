import hashlib
import hmac
import json
import time
from urllib.parse import urlencode, urlparse, parse_qs
from io import BytesIO
from types import SimpleNamespace
from contextlib import nullcontext
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from pypdf import PdfReader
from app.main import create_app
from app.config import Settings
from app.models import Attempt, AuthSession, AuthToken, Company, ContactGrant, Inbox, Invitation, LinkCode, MessengerLink, Notification, Outbox, Profile, User, VacancyResponse
from app.security import digest
from app.worker import process_once

PASSWORD = "Synthetic-Only-2026"


@pytest.fixture
def env(tmp_path):
    app = create_app(Settings(database_url="sqlite:///" + str(tmp_path / "test.db"), seed_demo=True, demo_password=PASSWORD, max_bot_token="test-bot-token", max_webhook_secret="test-webhook-secret-at-least-24"))
    with TestClient(app) as client:
        yield app, client


def login(app, email):
    client = TestClient(app)
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client, response.json()["user"]["id"]


def registered(app, client, email="new@example.com", role="candidate", confirm=True):
    token = client.get("/api/auth/me").json()["csrf_token"]
    client.headers["X-CSRF-Token"] = token
    response = client.post("/api/auth/register", json={"email": email, "password": PASSWORD, "role": role, "consent_version": "2026-10-06"})
    assert response.status_code == 201, response.text
    assert "token" not in response.text and PASSWORD not in response.text
    with app.state.session_factory() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user.password_hash != PASSWORD and user.password_hash.startswith("scrypt$")
        outbox = db.scalar(select(Outbox).where(Outbox.user_id == user.id, Outbox.kind == "confirm_email"))
        token = parse_qs(urlparse(outbox.payload["link"]).query)["token"][0]
        identifier = user.id
    if confirm:
        assert client.post("/api/auth/confirm-email", json={"token": token}).status_code == 200
    return identifier, token


def candidate_data():
    return {"full_name": "Анна Проверочная", "headline": "Python-разработчик", "bio": "Разрабатываю API. Связаться: private@example.com +7 900 123-45-67 @privateuser", "specialization": "backend_python", "declared_grade": "middle", "skills": ["python", "api", "testing"], "work_format": "remote", "location": "Санкт-Петербург", "salary_min": 80000, "salary_max": 160000, "contacts": {"phone": "+7 900 123-45-67", "telegram": "@privateuser", "website": "https://private.example.com"}, "visible": True, "consent_version": "2026-10-06"}


def need_and_candidate(app):
    employer, _ = login(app, "employer@demo.example.com")
    second, _ = login(app, "second@demo.example.com")
    candidate, cid = login(app, "candidate@demo.example.com")
    assert candidate.put("/api/candidate/profile", json=candidate_data()).status_code == 200
    need = employer.get("/api/employer/needs").json()["items"][0]
    return employer, second, candidate, cid, need


def invite(employer, cid, need):
    response = employer.post("/api/employer/invitations", json={"need_id": need["id"], "candidate_id": cid, "message": "Предлагаем обсудить разработку API", "contact_method": "Ответ в кабинете", "salary_min": 90000, "salary_max": 170000})
    assert response.status_code == 201, response.text
    return response.json()


def test_registration_confirmation_and_token_replay(env):
    app, client = env
    identifier, token = registered(app, client, confirm=False)
    response = client.post("/api/auth/login", json={"email": "new@example.com", "password": PASSWORD})
    assert response.status_code == 403
    assert client.post("/api/auth/confirm-email", json={"token": token}).status_code == 200
    assert client.post("/api/auth/confirm-email", json={"token": token}).status_code == 400
    account, user_id = login(app, "new@example.com")
    assert user_id == identifier and account.get("/api/auth/me").json()["user"]["email_verified"]


def test_batch_profile_projection_bounds_queries_and_preserves_company_grants(env):
    from app.services import profile_payloads
    app, _ = env
    employer, second, candidate, cid, need = need_and_candidate(app)
    offer = invite(employer, cid, need)
    assert candidate.post("/api/candidate/invitations/" + offer["id"] + "/respond", json={"decision":"accepted"}).status_code == 200
    with app.state.session_factory() as db:
        own_company = db.scalar(select(Company).where(Company.id == need["company_id"]))
        other_company = db.scalar(select(Company).where(Company.id != own_company.id))
        rows = list(db.scalars(select(Profile)))
        statements = []
        def count(_, __, statement, ___, ____, _____):
            statements.append(statement)
        event.listen(app.state.engine, "before_cursor_execute", count)
        try:
            projected = profile_payloads(db, rows, company_id=own_company.id)
            assert len(statements) == 2, "One User batch and one company-scoped active-grant batch"
            assert projected[cid]["contacts"]["phone"] == "+7 900 123-45-67"
            assert all("contacts" not in data for key,data in projected.items() if key != cid)
            statements.clear()
            other = profile_payloads(db, rows, company_id=other_company.id)
            assert len(statements) == 2 and "contacts" not in other[cid]
            assert "private@example.com" not in json.dumps(other)
            statements.clear()
            public = profile_payloads(db, rows)
            assert len(statements) == 1 and all("contacts" not in data and not data["contact_access"] for data in public.values())
        finally:
            event.remove(app.state.engine, "before_cursor_execute", count)
        grant = db.scalar(select(ContactGrant).where(ContactGrant.company_id == own_company.id, ContactGrant.candidate_id == cid))
        grant.revoked_at = time.time();db.commit()
        assert "contacts" not in profile_payloads(db, rows, company_id=own_company.id)[cid]


def test_bank_filters_complete_dataset_before_stable_pagination(env):
    app, _ = env
    employer, _ = login(app, "employer@demo.example.com")
    with app.state.session_factory() as db:
        template = db.scalar(select(User).where(User.email == "candidate@demo.example.com"))
        users, profiles = [], []
        for index in range(1005):
            identifier = f"f0000000-0000-0000-0000-{index:012x}"
            users.append(User(id=identifier,email=f"bank-{index}@demo.example.com",password_hash=template.password_hash,role="candidate",email_verified=True,is_demo=True,consent_version="2026-10-06"))
            profiles.append(Profile(user_id=identifier,data={"specialization":"frontend_ts","declared_grade":"middle","skills":["query_bound_only"],"full_name":"Синтетический профиль"},visible=True,verification={"frontend_ts":{"grade":"junior","skills":["typescript"],"synthetic":True}},fsp={"achievements":[]}))
        final_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        users.append(User(id=final_id,email="bank-last@demo.example.com",password_hash=template.password_hash,role="candidate",email_verified=True,is_demo=True,consent_version="2026-10-06"))
        profiles.append(Profile(user_id=final_id,data={"specialization":"backend_python","declared_grade":"junior","skills":[],"full_name":"Синтетический профиль"},visible=True,verification={"backend_python":{"grade":"middle","skills":["query_bound_only"],"synthetic":True}},fsp={"achievements":[{"source":"demo_registry","synthetic":True}]}))
        db.add_all(users);db.flush();db.add_all(profiles);db.commit()
    response = employer.get("/api/employer/candidates",params={"specialization":"backend_python","grade":"middle","skill":"QUERY_BOUND_ONLY","fsp":True,"page_size":3})
    assert response.status_code == 200,response.text
    body = response.json()
    assert body["total"] == 1 and body["pages"] == 1 and body["page"] == 1 and body["page_size"] == 3
    assert [profile["id"] for profile in body["items"]] == [final_id]
    # Declared skill in 1005 frontend profiles is never treated as verified.
    assert employer.get("/api/employer/candidates",params={"specialization":"frontend_ts","skill":"query_bound_only"}).json()["total"] == 0
    all_bank = employer.get("/api/employer/candidates",params={"page_size":3}).json()
    next_bank = employer.get("/api/employer/candidates",params={"page":2,"page_size":3}).json()
    assert all_bank["total"] == 1012 and all_bank["pages"] == 338
    combined = [profile["id"] for profile in all_bank["items"] + next_bank["items"]]
    assert combined == sorted(combined) and len(set(combined)) == 6
    assert employer.get("/api/employer/candidates",params={"page":0}).status_code == 422
    assert employer.get("/api/employer/candidates",params={"page_size":101}).status_code == 422


def test_disabled_candidate_is_suppressed_in_saved_shortlist_even_with_grant(env):
    app, _ = env
    employer, _, candidate, cid, need = need_and_candidate(app)
    offer = invite(employer, cid, need)
    assert candidate.post("/api/candidate/invitations/" + offer["id"] + "/respond",json={"decision":"accepted"}).status_code == 200
    assert employer.post("/api/employer/shortlist",json={"need_id":need["id"],"candidate_id":cid}).status_code == 201
    assert employer.get("/api/employer/shortlist").json()["items"][0]["profile"]["contacts"]
    with app.state.session_factory() as db:
        db.get(User,cid).disabled = True;db.commit()
    assert employer.get("/api/employer/shortlist").json()["items"][0]["profile"] is None
    assert employer.get("/api/candidates/" + cid).status_code == 404


def test_confirmation_expiration_and_forgot_non_enumerating(env):
    app, client = env
    _, token = registered(app, client, confirm=False)
    with app.state.session_factory() as db:
        db.get(AuthToken, digest(token)).expires_at = time.time() - 1
        db.commit()
    assert client.post("/api/auth/confirm-email", json={"token": token}).status_code == 400
    first = client.post("/api/auth/forgot-password", json={"email": "new@example.com"})
    second = client.post("/api/auth/forgot-password", json={"email": "absent@example.com"})
    assert first.json() == second.json()


def test_password_reset_revokes_all_sessions(env):
    app, client = env
    registered(app, client)
    first, uid = login(app, "new@example.com")
    second, _ = login(app, "new@example.com")
    assert first.post("/api/auth/forgot-password", json={"email": "new@example.com"}).status_code == 200
    with app.state.session_factory() as db:
        row = db.scalar(select(Outbox).where(Outbox.user_id == uid, Outbox.kind == "reset_password"))
        token = parse_qs(urlparse(row.payload["link"]).query)["token"][0]
    assert first.post("/api/auth/reset-password", json={"token": token, "password": "A-New-Synthetic-Pass-2026"}).status_code == 200
    assert first.get("/api/candidate/profile").status_code == second.get("/api/candidate/profile").status_code == 401
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    assert client.post("/api/auth/reset-password", json={"token": token, "password": PASSWORD}, headers={"X-CSRF-Token": csrf}).status_code == 400


def test_csrf_origin_roles_and_logout(env):
    app, _ = env
    account, _ = login(app, "candidate@demo.example.com")
    data = candidate_data()
    assert account.put("/api/candidate/profile", json=data, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert account.put("/api/candidate/profile", json=data, headers={"Origin": "https://hostile.example"}).status_code == 403
    assert account.get("/api/employer/needs").status_code == 403
    assert account.post("/api/auth/logout", json={}).status_code == 200
    assert account.get("/api/candidate/profile").status_code == 401


def test_contact_privacy_before_accept_other_company_and_revoke(env):
    app, _ = env
    employer, second, candidate, cid, need = need_and_candidate(app)
    search = employer.get(f"/api/employer/needs/{need['id']}/search")
    assert search.status_code == 200
    assert "private@example.com" not in search.text and "+7 900" not in search.text and "@privateuser" not in search.text
    item = employer.get(f"/api/candidates/{cid}").json()
    assert "contacts" not in item and "full_name" not in item and not item["contact_access"]
    offer = invite(employer, cid, need)
    assert "contacts" not in offer["candidate"]
    assert second.get(f"/api/employer/needs/{need['id']}").status_code == 404
    assert second.put(f"/api/employer/needs/{need['id']}", json={k: v for k, v in need.items() if k not in {"id", "company_id", "created_at", "updated_at"}}).status_code == 404
    assert candidate.post(f"/api/candidate/invitations/{offer['id']}/view", json={}).json()["status"] == "viewed"
    assert candidate.post(f"/api/candidate/invitations/{offer['id']}/respond", json={"decision": "accepted"}).status_code == 200
    assert employer.get(f"/api/candidates/{cid}").json()["contacts"]["email"] == "candidate@demo.example.com"
    assert "contacts" not in second.get(f"/api/candidates/{cid}").json()
    grant = candidate.get("/api/candidate/contact-grants").json()["items"][0]
    assert candidate.delete(f"/api/candidate/contact-grants/{grant['id']}").status_code == 200
    assert "contacts" not in employer.get(f"/api/candidates/{cid}").json()


def test_invitation_range_idempotency_owner_and_terminal_status(env):
    app, _ = env
    employer, second, candidate, cid, need = need_and_candidate(app)
    bad = {"need_id": need["id"], "candidate_id": cid, "message": "Приглашение обсудить задачу", "contact_method": "Кабинет", "salary_min": 100, "salary_max": 50}
    assert employer.post("/api/employer/invitations", json=bad).status_code == 422
    offer = invite(employer, cid, need)
    assert invite(employer, cid, need)["id"] == offer["id"]
    outsider, _ = login(app, "backend_python-middle@demo.example.com")
    assert outsider.post(f"/api/candidate/invitations/{offer['id']}/respond", json={"decision": "accepted"}).status_code == 404
    assert candidate.post(f"/api/candidate/invitations/{offer['id']}/respond", json={"decision": "rejected"}).json()["status"] == "rejected"
    assert candidate.post(f"/api/candidate/invitations/{offer['id']}/respond", json={"decision": "accepted"}).status_code == 409
    assert "contacts" not in employer.get(f"/api/candidates/{cid}").json()
    with app.state.session_factory() as db:
        assert len(list(db.scalars(select(Invitation)))) == 1
        assert len(list(db.scalars(select(ContactGrant)))) == 0


def test_visibility_and_self_report_cannot_change_grade(env):
    app, _ = env
    employer, _, candidate, cid, need = need_and_candidate(app)
    data = candidate_data()
    data["verified_grade"] = "middle"
    assert candidate.put("/api/candidate/profile", json=data).status_code == 422
    data.pop("verified_grade")
    data["visible"] = False
    assert candidate.put("/api/candidate/profile", json=data).json()["verified_grade"] == "junior"
    assert employer.get(f"/api/candidates/{cid}").status_code == 404
    assert cid not in [x["id"] for x in employer.get(f"/api/employer/needs/{need['id']}/search").json()["items"]]


def test_saved_survey_resumes_only_own_latest_candidate_context(env):
    app, client = env
    registered(app, client, email="survey-owner@example.com")
    account, _ = login(app, "survey-owner@example.com")
    assert account.get("/api/candidate/survey").json() == {"survey": None}
    first = {"specialization": "backend_python", "declared_grade": "junior", "answers": {"industry": "IT", "experience_context": "Учебные API", "preferred_tasks": "Backend"}}
    second = {"specialization": "frontend_ts", "declared_grade": "middle", "answers": {"industry": "Образование", "experience_context": "Веб-приложения", "preferred_tasks": "Интерфейсы"}}
    assert account.post("/api/candidate/survey", json=first).status_code == 200
    assert account.post("/api/candidate/survey", json=second).status_code == 200
    saved = account.get("/api/candidate/survey").json()["survey"]
    for key in ("specialization", "declared_grade", "answers"):
        assert saved[key] == second[key]
    assert saved["history_id"] and saved["created_at"]
    other, _ = login(app, "candidate@demo.example.com")
    assert other.get("/api/candidate/survey").json() == {"survey": None}
    employer, _ = login(app, "employer@demo.example.com")
    assert employer.get("/api/candidate/survey").status_code == 403


def test_employer_cannot_start_or_read_candidate_test_after_same_browser_role_change(env):
    app, _ = env
    account, _ = login(app, "candidate@demo.example.com")
    attempt = account.post("/api/candidate/attempts", json={"specialization": "frontend_ts", "grade": "junior"})
    assert attempt.status_code == 201
    identifier = attempt.json()["id"]
    assert account.post("/api/auth/logout", json={}).status_code == 200
    account.headers["X-CSRF-Token"] = account.get("/api/auth/me").json()["csrf_token"]
    switched = account.post("/api/auth/login", json={"email": "employer@demo.example.com", "password": PASSWORD})
    assert switched.status_code == 200 and switched.json()["user"]["role"] == "employer"
    account.headers["X-CSRF-Token"] = switched.json()["csrf_token"]
    for path in ("/api/candidate/attempts", f"/api/candidate/attempts/{identifier}", "/api/candidate/survey"):
        assert account.get(path).status_code == 403
    assert account.post("/api/candidate/attempts", json={"specialization": "frontend_ts", "grade": "junior"}).status_code == 403
    assert account.post(f"/api/candidate/attempts/{identifier}/submit", json={"answers": {}}).status_code == 403
    assert account.get("/api/employer/company").status_code == 200
    with app.state.session_factory() as db:
        assert db.get(Attempt, identifier).status == "in_progress"


def test_attempt_ownership_oracle_hidden_cooldown_and_voluntary_junior(env):
    app, client = env
    registered(app, client)
    account, uid = login(app, "new@example.com")
    body = {"specialization": "backend_python", "grade": "middle"}
    attempt = account.post("/api/candidate/attempts", json=body).json()
    assert "_oracle" not in json.dumps(attempt) and "expected" not in json.dumps(attempt["form"])
    assert account.post("/api/candidate/attempts", json=body).json()["id"] == attempt["id"]
    other, _ = login(app, "candidate@demo.example.com")
    assert other.get(f"/api/candidate/attempts/{attempt['id']}").status_code == 404
    assert other.post(f"/api/candidate/attempts/{attempt['id']}/submit", json={"answers": {}}).status_code == 404
    result = account.post(f"/api/candidate/attempts/{attempt['id']}/submit", json={"answers": {}}).json()
    assert result["status"] == "failed" and result["verified_grade"] is None
    assert account.post(f"/api/candidate/attempts/{attempt['id']}/submit", json={"answers": {"q1": "o1"}}).json()["result"] == result["result"]
    assert account.post("/api/candidate/attempts", json=body).status_code == 409
    junior = account.post("/api/candidate/attempts", json={**body, "grade": "junior"}).json()
    with app.state.session_factory() as db:
        trusted = db.get(Attempt, junior["id"]).form
        answers = {qid: oracle["expected"] for qid, oracle in trusted["_oracle"].items()}
    passed = account.post(f"/api/candidate/attempts/{junior['id']}/submit", json={"answers": answers})
    assert passed.status_code == 200, passed.text
    assert passed.json()["status"] == "passed" and passed.json()["verified_grade"] == "junior"
    assert account.get("/api/candidate/profile").json()["verified_skills"]


def test_failure_never_downgrades_existing_grade_and_expiry(env):
    app, _ = env
    account, uid = login(app, "backend_python-middle@demo.example.com")
    row = account.post("/api/candidate/attempts", json={"specialization": "backend_python", "grade": "junior"}).json()
    result = account.post(f"/api/candidate/attempts/{row['id']}/submit", json={"answers": {}}).json()
    assert result["verified_grade"] == "middle"
    other = account.post("/api/candidate/attempts", json={"specialization": "frontend_ts", "grade": "junior"}).json()
    with app.state.session_factory() as db:
        db.get(Attempt, other["id"]).expires_at = time.time() - 1
        db.commit()
    assert account.post(f"/api/candidate/attempts/{other['id']}/submit", json={"answers": {}}).status_code == 409
    assert account.post("/api/candidate/attempts", json={"specialization": "frontend_ts", "grade": "junior"}).status_code == 409


def test_vacancy_response_grants_only_owner_and_closed_rejected(env):
    app, _ = env
    employer, second, candidate, cid, need = need_and_candidate(app)
    vacancy = employer.get("/api/employer/vacancies").json()["items"][0]
    assert second.delete(f"/api/employer/vacancies/{vacancy['id']}").status_code == 404
    response = candidate.post(f"/api/candidate/vacancies/{vacancy['id']}/respond", json={"message": "Хочу обсудить вашу задачу"})
    assert response.status_code == 201
    assert candidate.post(f"/api/candidate/vacancies/{vacancy['id']}/respond", json={"message": "Повторный клик"}).json()["id"] == response.json()["id"]
    assert employer.get("/api/employer/responses").json()["items"][0]["candidate"]["contacts"]["email"] == "candidate@demo.example.com"
    assert "contacts" not in second.get(f"/api/candidates/{cid}").json()
    assert employer.delete(f"/api/employer/vacancies/{vacancy['id']}").status_code == 200
    assert candidate.post(f"/api/candidate/vacancies/{vacancy['id']}/respond", json={"message": "Снова хочу обсудить"}).status_code == 404


def test_microtasks_history_no_grade_mutation_and_oracle_hidden(env):
    app, _ = env
    employer, _, candidate, cid, _ = need_and_candidate(app)
    task = employer.post("/api/employer/microtasks", json={"title": "SQL-предикат", "description": "Введите SQL-проверку отсутствия значения в поле email", "specialization": "data_sql", "grade": "junior", "expected_answer": "email IS NULL"}).json()
    assert "expected" not in json.dumps(task) and "hash" not in json.dumps(task)
    tasks = candidate.get("/api/microtasks")
    assert "expected_hash" not in tasks.text
    before = candidate.get("/api/candidate/profile").json()["verified_grade"]
    first = candidate.post(f"/api/candidate/microtasks/{task['id']}/submit", json={"answer": "email = NULL"}).json()
    second = candidate.post(f"/api/candidate/microtasks/{task['id']}/submit", json={"answer": "  EMAIL is   null "}).json()
    assert first["score"] == 0 and second["score"] == 100 and first["id"] != second["id"]
    assert candidate.get("/api/candidate/profile").json()["verified_grade"] == before
    assert len(candidate.get("/api/candidate/microtask-history").json()["items"]) == 2


def test_pdf_export_privacy_and_import_preview(env):
    app, _ = env
    employer, second, candidate, cid, need = need_and_candidate(app)
    public = employer.get(f"/api/candidates/{cid}/pdf")
    assert public.status_code == 200
    text = "\n".join(x.extract_text() for x in PdfReader(BytesIO(public.content)).pages)
    assert "КОД.МЭТЧ" in text and "private@example.com" not in text and "@privateuser" not in text
    own = candidate.get("/api/candidate/profile/pdf")
    assert "candidate@demo.example.com" in "\n".join(x.extract_text() for x in PdfReader(BytesIO(own.content)).pages)
    preview = candidate.post("/api/candidate/profile/import", files={"file": ("resume.pdf", own.content, "application/pdf")})
    assert preview.status_code == 200, preview.text
    assert preview.json()["requires_confirmation"] and "verified_grade" not in preview.json()["fields"]
    assert candidate.post("/api/candidate/profile/import", files={"file": ("not.pdf", b"not a PDF", "application/pdf")}).status_code == 415
    assert candidate.post("/api/candidate/profile/import", files={"file": ("bad.pdf", b"%PDF-1.7 corrupt", "application/pdf")}).status_code == 422


def test_notifications_are_owned_and_delivery_failure_independent(env, monkeypatch):
    app, _ = env
    employer, _, candidate, cid, need = need_and_candidate(app)
    offer = invite(employer, cid, need)
    notice = candidate.get("/api/notifications").json()["items"][0]
    assert employer.post(f"/api/notifications/{notice['id']}/read", json={}).status_code == 404
    assert candidate.post(f"/api/notifications/{notice['id']}/read", json={}).status_code == 200
    assert candidate.post(f"/api/candidate/invitations/{offer['id']}/respond", json={"decision": "accepted"}).status_code == 200
    with app.state.session_factory() as db:
        db.add(MessengerLink(user_id=cid, platform="max", recipient_id="123"))
        outbox = Outbox(user_id=cid, channel="max", kind="invitation", payload={"recipient_id": "123", "invitation_id": offer["id"]})
        db.add(outbox)
        db.commit()
        oid = outbox.id
    monkeypatch.setattr("app.integrations.messengers.deliver", lambda *a: {"delivered": False, "error": "rate_limited", "retry_after": 120})
    assert process_once(app.state.session_factory, app.state.settings)
    with app.state.session_factory() as db:
        row = db.get(Outbox, oid)
        assert row.status == "retry" and row.attempts == 1 and row.next_attempt_at > time.time() + 100
        assert db.get(Invitation, offer["id"]).status == "accepted"
    assert employer.get(f"/api/candidates/{cid}").json()["contact_access"]


def test_worker_lease_recovery_and_confirmation_smtp(env, monkeypatch):
    app, client = env
    identifier, token = registered(app, client, confirm=False)
    delivered = []
    monkeypatch.setattr("app.worker.send_email", lambda payload, settings: delivered.append(payload["link"]) or {"delivered": True})
    with app.state.session_factory() as db:
        row = db.scalar(select(Outbox).where(Outbox.user_id == identifier))
        row.status, row.lease_until = "processing", time.time() - 1
        db.commit()
        oid = row.id
    assert process_once(app.state.session_factory, app.state.settings)
    with app.state.session_factory() as db:
        assert db.get(Outbox, oid).status == "delivered"
    assert token in delivered[0]  # Internal test mailbox capture, never API output.


def test_webhook_authentication_replay_link_identity_and_unlink(env):
    app, client = env
    account, uid = login(app, "candidate@demo.example.com")
    code = account.post("/api/integrations/messenger/link-code", json={"platform": "max"}).json()["code"]
    payload = {"update_type": "message_created", "message": {"sender": {"user_id": 123, "is_bot": False}, "recipient": {"chat_type": "dialog"}, "body": {"mid": "test-link-100", "text": "/start " + code}}}
    assert client.post("/api/integrations/max/webhook", json=payload).status_code == 401
    headers = {"X-Max-Bot-Api-Secret": app.state.settings.max_webhook_secret}
    assert client.post("/api/integrations/max/webhook", json=payload, headers=headers).status_code == 200
    assert client.post("/api/integrations/max/webhook", json=payload, headers=headers).status_code == 200
    with app.state.session_factory() as db:
        assert len(list(db.scalars(select(Inbox)))) == 1
        assert len(list(db.scalars(select(MessengerLink)))) == 1
        replies = list(db.scalars(select(Outbox).where(Outbox.kind == "bot_reply")))
        assert len(replies) == 1 and replies[0].payload["reply_key"] == "linked"
        assert code not in json.dumps(replies[0].payload) and "candidate@" not in json.dumps(replies[0].payload)
    # A second account cannot steal the same platform identity.
    other, _ = login(app, "backend_python-middle@demo.example.com")
    second_code = other.post("/api/integrations/messenger/link-code", json={"platform": "max"}).json()["code"]
    payload["message"]["body"]["mid"], payload["message"]["body"]["text"] = "test-link-101", "/start " + second_code
    assert client.post("/api/integrations/max/webhook", json=payload, headers=headers).status_code == 200
    with app.state.session_factory() as db:
        assert db.scalar(select(MessengerLink)).user_id == uid
    payload["message"]["body"]["mid"], payload["message"]["body"]["text"] = "test-link-102", "/unlink"
    client.post("/api/integrations/max/webhook", json=payload, headers=headers)
    assert not account.get("/api/integrations/status").json()["max"]["linked"]


def signed_launch(token, identity=123, timestamp=None):
    fields = {"auth_date": str(int(time.time()) if timestamp is None else timestamp), "user": json.dumps({"id": identity}, separators=(",", ":"))}
    data = "\n".join(k + "=" + fields[k] for k in sorted(fields))
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_signed_launch_requires_link_email_and_no_forged_identity(env):
    app, client = env
    account, _ = login(app, "candidate@demo.example.com")
    code = account.post("/api/integrations/messenger/link-code", json={"platform": "max"}).json()["code"]
    proof = signed_launch(app.state.settings.max_bot_token)
    assert client.post("/api/integrations/messenger/launch", json={"platform": "max", "init_data": proof}).status_code == 403
    assert client.post("/api/integrations/messenger/consume", json={"platform": "max", "init_data": proof, "code": code}).status_code == 200
    assert client.post("/api/integrations/messenger/launch", json={"platform": "max", "init_data": proof}).status_code == 200
    forged = signed_launch("wrong-token")
    assert client.post("/api/integrations/messenger/launch", json={"platform": "max", "init_data": forged}).status_code == 401
    expired = signed_launch(app.state.settings.max_bot_token, timestamp=int(time.time()) - 1000)
    assert client.post("/api/integrations/messenger/launch", json={"platform": "max", "init_data": expired}).status_code == 401


def test_llm_timeout_and_invalid_structure_fallback(env, monkeypatch):
    app, _ = env
    employer, _ = login(app, "employer@demo.example.com")
    app.state.settings.llm_base_url, app.state.settings.llm_model = "http://llm.invalid/v1", "synthetic-model"
    import httpx
    def timed_out(request):
        raise httpx.ReadTimeout("synthetic timeout", request=request)
    monkeypatch.setattr("app.integrations.parsing.LLMClient", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(timed_out), **kw))
    response = employer.post("/api/employer/needs/parse", json={"text": "Ищем Middle Python и FastAPI разработчика на удалённую работу"})
    assert response.status_code == 200 and response.json()["parser"] == "local_fallback"
    assert response.json()["fields"]["specialization"] == "backend_python" and response.json()["requires_confirmation"]
    invalid = json.dumps({"choices": [{"message": {"content": '{"verified_grade":"middle"}'}}]}).encode()
    monkeypatch.setattr("app.integrations.parsing.LLMClient", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"Content-Type": "application/json"}, stream=httpx.ByteStream(invalid))), **kw))
    assert employer.post("/api/employer/needs/parse", json={"text": "Ищем Python разработчика для API"}).json()["parser"] == "local_fallback"


def test_production_cookies_and_closed_registration(tmp_path):
    settings = Settings(database_url="sqlite:///" + str(tmp_path / "prod.db"), environment="production", app_url="https://fsp.example.com", allowed_origins="https://fsp.example.com", registration_enabled=False, seed_demo=True, demo_password=PASSWORD)
    with TestClient(create_app(settings), base_url="https://fsp.example.com") as client:
        me = client.get("/api/auth/me")
        cookie = me.headers["set-cookie"]
        assert "__Host-fsp_preauth" in cookie and "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert "default-src 'none'" in me.headers["content-security-policy"]
        token = me.json()["csrf_token"]
        assert client.post("/api/auth/register", json={"email": "real@example.com", "password": PASSWORD, "role": "candidate", "consent_version": "2026-10-06"}, headers={"X-CSRF-Token": token}).status_code == 503
        logged = client.post("/api/auth/login", json={"email": "candidate@demo.example.com", "password": PASSWORD}, headers={"X-CSRF-Token": token})
        assert "__Host-fsp_session" in logged.headers["set-cookie"]
        assert client.get("/api/docs").status_code == 404
        assert client.get("/api/catalog").json()["registration_enabled"] is False


def test_invalid_payload_does_not_echo_password(env):
    app, client = env
    token = client.get("/api/auth/me").json()["csrf_token"]
    response = client.post("/api/auth/register", json={"email": "bad", "password": "secret-short", "role": "candidate", "consent_version": "2026-10-06"}, headers={"X-CSRF-Token": token})
    assert response.status_code == 422 and "secret-short" not in response.text


def test_parallel_starts_keep_one_attempt_and_cooldown(env):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    app, _ = env
    account, uid = login(app, "candidate@demo.example.com")
    cookie = account.cookies.get("fsp_session")
    csrf_token = account.headers["X-CSRF-Token"]
    barrier = threading.Barrier(8)
    def start():
        client = TestClient(app)
        client.cookies.set("fsp_session", cookie)
        client.headers["X-CSRF-Token"] = csrf_token
        barrier.wait(timeout=10)
        result = client.post("/api/candidate/attempts", json={"specialization": "backend_python", "grade": "middle"})
        assert result.status_code == 201, result.text
        return result.json()["id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        identifiers = list(pool.map(lambda _: start(), range(8)))
    assert len(set(identifiers)) == 1
    with app.state.session_factory() as db:
        assert len(list(db.scalars(select(Attempt).where(Attempt.user_id == uid)))) == 1
    attempt_id = identifiers[0]
    assert account.post(f"/api/candidate/attempts/{attempt_id}/submit", json={"answers": {}}).status_code == 200
    assert account.post("/api/candidate/attempts", json={"specialization": "backend_python", "grade": "middle"}).status_code == 409


def test_rate_limit_does_not_commit_business_changes(env):
    from app.security import rate_limit
    app, _ = env
    with app.state.session_factory() as db:
        user = db.scalar(select(User).where(User.email == "candidate@demo.example.com"))
        user.disabled = True
        rate_limit(db, "isolated-limiter-test", user.id, 5, 60)
        db.rollback()
    with app.state.session_factory() as db:
        assert not db.scalar(select(User).where(User.email == "candidate@demo.example.com")).disabled


def test_queued_notification_cancelled_after_unlink(env, monkeypatch):
    from app.integrations.bot_commands import handle_bot_command
    app, _ = env
    account, uid = login(app, "candidate@demo.example.com")
    with app.state.session_factory() as db:
        db.add(MessengerLink(user_id=uid, platform="max", recipient_id="999"))
        row = Outbox(user_id=uid, channel="max", kind="invitation", payload={"recipient_id": "999", "status": "sent"})
        db.add(row)
        db.commit()
        oid = row.id
        assert handle_bot_command("max", "999", "/unlink", "unlink-privacy-test", app.state.settings, db) == "unlinked"
    with app.state.session_factory() as db:
        assert db.get(Outbox, oid).status == "cancelled"
        # Retry recovery for an older process must independently check live link.
        db.get(Outbox, oid).status = "retry"
        db.commit()
    calls = []
    monkeypatch.setattr("app.integrations.messengers.deliver", lambda *a: calls.append(a) or {"delivered": True})
    process_once(app.state.session_factory, app.state.settings)
    assert calls == []
    with app.state.session_factory() as db:
        assert db.get(Outbox, oid).status == "cancelled"


def test_contact_in_name_and_max_link_redacted(env):
    app, _ = env
    employer, _, candidate, cid, _ = need_and_candidate(app)
    data = candidate_data()
    data.update(full_name="private@example.com", bio="MAX: https://max.ru/u/private_contact")
    candidate.put("/api/candidate/profile", json=data)
    public = employer.get(f"/api/candidates/{cid}")
    assert "private@example.com" not in public.text and "private_contact" not in public.text
    data["full_name"] = "+79001234567"
    candidate.put("/api/candidate/profile", json=data)
    assert "+79001234567" not in employer.get(f"/api/candidates/{cid}").text


def test_llm_resume_personal_data_gate_and_need_redaction(env, monkeypatch):
    from app.integrations.parsing import parse
    app, _ = env
    settings = app.state.settings
    settings.llm_base_url, settings.llm_model = "https://llm.invalid/v1", "test-model"
    outgoing = []
    import httpx
    def post(request):
        outgoing.append(json.loads(request.content)["messages"][-1]["content"])
        body = json.dumps({"choices": [{"message": {"content": '{"specialization":"backend_python","grade":"junior","required_skills":["python"]}'}}]}).encode()
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=httpx.ByteStream(body))
    monkeypatch.setattr("app.integrations.parsing.LLMClient", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(post), **kw))
    assert parse("Иван Иванов private@example.com Python", "resume", settings)["parser"] == "local_fallback"
    assert outgoing == []
    assert parse("Нужен Python. private@example.com +79001234567 https://private.example.com", "need", settings)["parser"] == "llm_structured"
    assert "private@example.com" not in outgoing[0] and "+79001234567" not in outgoing[0] and "https://private" not in outgoing[0]


def test_hidden_profile_not_exposed_through_existing_invitation(env):
    app, _ = env
    employer, _, candidate, cid, need = need_and_candidate(app)
    offer = invite(employer, cid, need)
    hidden = candidate_data()
    hidden.update(visible=False, bio="Hidden current profile content")
    candidate.put("/api/candidate/profile", json=hidden)
    result = employer.get("/api/employer/invitations")
    assert "Hidden current profile content" not in result.text
    assert result.json()["items"][0]["candidate"] is None
    assert employer.get(f"/api/candidates/{cid}").status_code == 404


def test_invitation_snapshot_survives_need_edit(env):
    app, _ = env
    employer, _, candidate, cid, need = need_and_candidate(app)
    offer = invite(employer, cid, need)
    updated = {k: v for k, v in need.items() if k not in {"id", "company_id", "created_at", "updated_at"}}
    updated["description"] = "Другое описание после отправки приглашения"
    employer.put(f"/api/employer/needs/{need['id']}", json=updated)
    historical = candidate.get("/api/candidate/invitations").json()["items"][0]
    assert historical["need"]["description"] == offer["need"]["description"]
    assert "_need_snapshot" not in historical


def test_production_messenger_cookie_none_only_validated_launch(tmp_path):
    settings = Settings(database_url="sqlite:///" + str(tmp_path / "mini.db"), environment="production", app_url="https://fsp.example.com", allowed_origins="https://fsp.example.com", registration_enabled=False, seed_demo=True, demo_password=PASSWORD, max_bot_token="test-token")
    app = create_app(settings)
    with TestClient(app, base_url="https://fsp.example.com") as client:
        csrf_token = client.get("/api/auth/me").json()["csrf_token"]
        normal = client.post("/api/auth/login", json={"email": "candidate@demo.example.com", "password": PASSWORD}, headers={"X-CSRF-Token": csrf_token})
        assert "SameSite=lax" in normal.headers["set-cookie"]
        uid = normal.json()["user"]["id"]
        with app.state.session_factory() as db:
            db.add(MessengerLink(user_id=uid, platform="max", recipient_id="123"))
            db.commit()
        launch = client.post("/api/integrations/messenger/launch", json={"platform": "max", "init_data": signed_launch(settings.max_bot_token)})
        assert launch.status_code == 200
        assert "SameSite=none" in launch.headers["set-cookie"] and "Secure" in launch.headers["set-cookie"]


def test_junior_pass_preserves_higher_grade_score_date_and_source(env):
    app, _ = env
    candidate, uid = login(app, "backend_python-middle@demo.example.com")
    with app.state.session_factory() as db:
        profile = db.get(Profile, uid)
        previous = {"grade": "middle", "skills": ["api", "testing"], "score": 75, "verified_at": 1700000000.0, "version": "assessment-historical", "attempt_id": "historical-middle-id"}
        profile.verification = {"backend_python": previous}
        db.commit()
    junior = candidate.post("/api/candidate/attempts", json={"specialization": "backend_python", "grade": "junior"}).json()
    with app.state.session_factory() as db:
        form = db.get(Attempt, junior["id"]).form
        answers = {qid: oracle["expected"] for qid, oracle in form["_oracle"].items()}
    result = candidate.post(f"/api/candidate/attempts/{junior['id']}/submit", json={"answers": answers})
    assert result.json()["result"]["score"] == 100
    with app.state.session_factory() as db:
        highest = db.get(Profile, uid).verification["backend_python"]
        for key in ["grade", "score", "verified_at", "version", "attempt_id"]:
            assert highest[key] == previous[key]
        assert highest["by_grade"]["junior"]["score"] == 100
        assert highest["by_grade"]["junior"]["attempt_id"] == junior["id"]


def test_smtp_tls_configuration_blank_port_and_legal_gate(monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "")
    monkeypatch.setenv("SMTP_TLS_MODE", "starttls")
    settings = Settings(environment="development")
    assert settings.smtp_port == 587 and settings.smtp_starttls and not settings.smtp_ssl
    monkeypatch.setenv("SMTP_TLS_MODE", "ssl")
    settings = Settings(environment="development")
    assert settings.smtp_port == 465 and settings.smtp_ssl and not settings.smtp_starttls
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(environment="production", app_url="https://fsp.example.com", registration_enabled=True, legal_operator_name="Учебный оператор", legal_operator_contact="operator@example.com", smtp_host="smtp.example.com", smtp_from="approved@example.com", smtp_tls_mode="none", smtp_password="never-print-this-secret")
    try:
        Settings(environment="production", app_url="http://not-https.example.com", registration_enabled=False, smtp_password="never-print-this-secret")
    except ValidationError as error:
        assert "never-print-this-secret" not in str(error)


def test_retired_telegram_credentials_and_saved_link_cannot_enable_routes(env, monkeypatch):
    from app.integrations.routes import public_bot_url
    from app.integrations.bot_commands import handle_bot_command
    app, client = env
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-retired-token")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "retired_bot")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic-retired-webhook-secret")
    # Old environment keys are ignored and cannot restore an active transport.
    app.state.settings = Settings(**app.state.settings.model_dump())
    assert not hasattr(app.state.settings, "telegram_bot_token")
    account, uid = login(app, "candidate@demo.example.com")
    with app.state.session_factory() as db:
        db.add(MessengerLink(user_id=uid, platform="telegram", recipient_id="123"))
        db.commit()
    state = account.get("/api/integrations/status").json()
    assert "telegram" not in state and state["max"]["configured"]
    assert public_bot_url("telegram", SimpleNamespace(telegram_bot_token="synthetic", telegram_bot_username="retired_bot")) is None
    assert account.post("/api/integrations/messenger/link-code", json={"platform": "telegram"}).status_code == 422
    proof = signed_launch("synthetic-retired-token")
    assert client.post("/api/integrations/messenger/launch", json={"platform": "telegram", "init_data": proof}).status_code == 422
    assert client.post("/api/integrations/messenger/consume", json={"platform": "telegram", "init_data": proof, "code": "synthetic-unused-code-123456"}).status_code == 422
    assert client.post("/api/integrations/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": "synthetic-retired-webhook-secret"}, json={}).status_code == 404
    with app.state.session_factory() as db:
        assert handle_bot_command("telegram", "123", "/unlink", "retired-channel-callback", app.state.settings, db) == "error"
        assert db.scalar(select(MessengerLink).where(MessengerLink.platform == "telegram")) is not None
        assert not db.scalar(select(Inbox.id)) and not db.scalar(select(LinkCode.id))
    assert account.post("/api/integrations/messenger/link-code", json={"platform": "max"}).status_code == 200


@pytest.mark.parametrize("kind", ["invitation", "bot_reply"])
@pytest.mark.parametrize("old_status", ["pending", "retry", "processing"])
def test_retired_queued_telegram_is_cancelled_without_network_and_max_still_delivers(env, monkeypatch, kind, old_status):
    from app.services import notify
    app, _ = env
    _, uid = login(app, "candidate@demo.example.com")
    with app.state.session_factory() as db:
        db.add(MessengerLink(user_id=uid, platform="telegram", recipient_id="123"))
        db.add(MessengerLink(user_id=uid, platform="max", recipient_id="456"))
        legacy = Outbox(user_id=uid, channel="telegram", kind=kind,
                        payload={"recipient_id": "123", "reply_key": "help"}, status=old_status,
                        created_at=time.time() - 30, next_attempt_at=0, lease_until=time.time() - 1)
        db.add(legacy)
        notify(db, uid, "invitation", {"status": "sent", "title": "Проверка MAX"})
        db.commit()
        oid = legacy.id
        # The stored retired link is preserved, but business events use MAX only.
        assert len(list(db.scalars(select(Outbox).where(Outbox.channel == "telegram")))) == 1
        max_id = db.scalar(select(Outbox.id).where(Outbox.channel == "max"))
        assert max_id is not None
    calls = []
    monkeypatch.setattr("app.integrations.messengers.deliver", lambda *args: calls.append(args[0]) or {"delivered": True})
    assert process_once(app.state.session_factory, app.state.settings)
    assert calls == []
    with app.state.session_factory() as db:
        legacy = db.get(Outbox, oid)
        assert legacy.status == "cancelled" and legacy.last_error == "unsupported_platform"
        assert legacy.attempts == 0 and legacy.lease_until is None
        assert db.scalar(select(MessengerLink).where(MessengerLink.platform == "telegram")) is not None
    assert process_once(app.state.session_factory, app.state.settings)
    assert calls == ["max"]
    with app.state.session_factory() as db:
        assert db.get(Outbox, max_id).status == "delivered"


def test_retired_contact_remains_private_on_profile_save_and_is_absent_from_granted_contact_and_pdf(env):
    app, _ = env
    employer, _, candidate, cid, need = need_and_candidate(app)
    data = candidate_data()
    data["contacts"]["telegram"] = "@retired_channel_only"
    assert candidate.put("/api/candidate/profile", json=data).status_code == 200
    del data["contacts"]["telegram"]
    assert candidate.put("/api/candidate/profile", json=data).status_code == 200
    assert candidate.get("/api/candidate/profile").json()["contacts"]["telegram"] == "@retired_channel_only"
    offer = invite(employer, cid, need)
    assert candidate.post(f"/api/candidate/invitations/{offer['id']}/respond", json={"decision": "accepted"}).status_code == 200
    granted = employer.get(f"/api/candidates/{cid}").json()
    assert "telegram" not in granted["contacts"] and granted["contacts"]["phone"] == data["contacts"]["phone"]
    for response in [candidate.get("/api/candidate/profile/pdf"), employer.get(f"/api/candidates/{cid}/pdf")]:
        assert response.status_code == 200
        text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
        assert "@retired_channel_only" not in text and "candidate@demo.example.com" in text
