"""Expired links and a refused SMTP account cannot amplify the email queue."""
import smtplib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import issue_token
from app.config import Settings
from app.database import build_database, dispose_database
from app.models import AuthToken, Base, Outbox, User
from app.main import create_app
from app.security import digest
from app.worker import process_once


NOW = 10000.0


@pytest.fixture
def store():
    engine, factory = build_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with factory() as db:
        user = User(email="email-recovery@example.com", password_hash="not-loginable-fixture",
                    role="candidate", consent_version="test")
        other = User(email="other-recovery@example.com", password_hash="not-loginable-fixture",
                     role="candidate", consent_version="test")
        db.add_all([user, other]); db.commit()
        identifiers = user.id, other.id
    yield factory, Settings(), identifiers
    dispose_database(engine)


def queue_auth(store, *, kind="confirm_email", user_index=0):
    factory, settings, identifiers = store
    with factory() as db:
        user = db.get(User, identifiers[user_index])
        existing = set(db.scalars(select(Outbox.id)))
        issue_token(db, user, kind, settings)
        db.flush()
        row = next(row for row in db.scalars(select(Outbox)) if row.id not in existing)
        row.next_attempt_at = NOW
        row.created_at = NOW + len(existing)
        token_raw = parse_qs(urlsplit(row.payload["link"]).query)["token"][0]
        token = db.get(AuthToken, digest(token_raw))
        token.expires_at = NOW + (86400 if kind == "confirm_email" else 3600)
        db.commit()
        return row.id, token.id


@pytest.mark.parametrize("old_status", ["pending", "retry", "failed"])
def test_resend_cancels_old_link_and_sends_only_latest(store, monkeypatch, old_status):
    factory, settings, _ = store
    old_id, old_token = queue_auth(store)
    with factory() as db:
        db.get(Outbox, old_id).status = old_status
        db.commit()
    new_id, new_token = queue_auth(store)
    sent = []
    monkeypatch.setattr("app.worker.send_email", lambda payload, _: sent.append(
        digest(parse_qs(urlsplit(payload["link"]).query)["token"][0])) or {"delivered": True})
    assert process_once(factory, settings, now=NOW)
    with factory() as db:
        assert db.get(AuthToken, old_token) is None
        assert db.get(Outbox, old_id).status == "cancelled"
        assert db.get(Outbox, new_id).status == "delivered"
    assert sent == [new_token]


def test_reclaimed_processing_link_is_cancelled_after_resend(store, monkeypatch):
    factory, settings, _ = store
    old_id, _ = queue_auth(store)
    with factory() as db:
        old = db.get(Outbox, old_id)
        old.status, old.lease_until = "processing", NOW - 1
        db.commit()
    new_id, _ = queue_auth(store)
    monkeypatch.setattr("app.worker.send_email", lambda *_: pytest.fail("Obsolete leased link reached SMTP"))
    assert process_once(factory, settings, now=NOW)
    with factory() as db:
        assert db.get(Outbox, old_id).status == "cancelled"
        assert db.get(Outbox, new_id).status == "pending"


def test_registration_resend_route_keeps_latest_token_and_non_enumerating_response(tmp_path):
    app = create_app(Settings(database_url="sqlite:///" + str(tmp_path / "resend.db")))
    with TestClient(app) as client:
        csrf = client.get("/api/auth/me").json()["csrf_token"]
        client.headers["X-CSRF-Token"] = csrf
        assert client.post("/api/auth/register", json={"email": "new-recovery@example.com",
            "password": "Synthetic-Recovery-2026", "role": "candidate",
            "consent_version": "2026-10-06"}).status_code == 201
        with app.state.session_factory() as db:
            old = db.scalar(select(Outbox))
            old_id = old.id
            old_raw = parse_qs(urlsplit(old.payload["link"]).query)["token"][0]
        resend = client.post("/api/auth/resend-confirmation", json={"email": "new-recovery@example.com"})
        absent = client.post("/api/auth/resend-confirmation", json={"email": "absent-recovery@example.com"})
        assert resend.status_code == absent.status_code == 200 and resend.json() == absent.json()
        with app.state.session_factory() as db:
            assert db.get(Outbox, old_id).status == "cancelled"
            current = db.scalar(select(Outbox).where(Outbox.status == "pending"))
            current_raw = parse_qs(urlsplit(current.payload["link"]).query)["token"][0]
        assert client.post("/api/auth/confirm-email", json={"token": old_raw}).status_code == 400
        assert client.post("/api/auth/confirm-email", json={"token": current_raw}).status_code == 200
        assert client.post("/api/auth/confirm-email", json={"token": current_raw}).status_code == 400


@pytest.mark.parametrize("invalid", ["missing", "expired", "used", "wrong_user", "wrong_kind"])
def test_invalid_legacy_auth_link_is_cancelled_before_smtp(store, monkeypatch, invalid):
    factory, settings, identifiers = store
    row_id, token_id = queue_auth(store, kind="reset_password")
    with factory() as db:
        token = db.get(AuthToken, token_id)
        if invalid == "missing":
            db.delete(token)
        elif invalid == "expired":
            token.expires_at = NOW
        elif invalid == "used":
            token.used_at = NOW - 1
        elif invalid == "wrong_user":
            token.user_id = identifiers[1]
        else:
            token.kind = "confirm_email"
        db.commit()
    monkeypatch.setattr("app.worker.send_email", lambda *_: pytest.fail("Invalid link reached SMTP"))
    assert process_once(factory, settings, now=NOW)
    with factory() as db:
        row = db.get(Outbox, row_id)
        assert row.status == "cancelled" and row.last_error == "invalid_auth_token"
        assert row.attempts == 0 and row.lease_until is None


@pytest.mark.parametrize("link", ["", "https://example.com/confirm?token=one&token=two", "https://example.com/confirm?missing", None])
def test_malformed_legacy_link_is_cancelled_without_error_details(store, monkeypatch, link):
    factory, settings, _ = store
    row_id, _ = queue_auth(store)
    with factory() as db:
        row = db.get(Outbox, row_id)
        row.payload = {**row.payload, "link": link}
        db.commit()
    monkeypatch.setattr("app.worker.send_email", lambda *_: pytest.fail("Malformed link reached SMTP"))
    assert process_once(factory, settings, now=NOW)
    with factory() as db:
        row = db.get(Outbox, row_id)
        assert row.status == "cancelled" and row.last_error == "invalid_auth_token"


def test_auth_error_uses_only_numeric_code_and_keeps_valid_mail_after_eight_attempts(store, monkeypatch):
    factory, settings, _ = store
    row_id, _ = queue_auth(store)
    secret_text = b"535 secret-password mailbox@example.com token=raw-sensitive-value"
    with factory() as db:
        db.get(Outbox, row_id).attempts = 7
        db.commit()
    def refused(*_):
        raise smtplib.SMTPAuthenticationError(535, secret_text)
    monkeypatch.setattr("app.worker.send_email", refused)
    assert process_once(factory, settings, now=NOW)
    with factory() as db:
        row = db.get(Outbox, row_id)
        assert row.status == "retry" and row.attempts == 8
        assert row.last_error == "SMTPAuthenticationError:535"
        assert row.next_attempt_at == NOW + 900 and row.lease_until is None
        assert secret_text.decode() not in row.last_error


@pytest.mark.parametrize("code", ["secret-code", True, 999])
def test_non_numeric_or_invalid_smtp_code_is_not_stored(store, monkeypatch, code):
    factory, settings, _ = store
    row_id, _ = queue_auth(store)
    def refused(*_):
        raise smtplib.SMTPAuthenticationError(code, b"private provider text")
    monkeypatch.setattr("app.worker.send_email", refused)
    assert process_once(factory, settings, now=NOW)
    with factory() as db:
        assert db.get(Outbox, row_id).last_error == "SMTPAuthenticationError"


@pytest.mark.parametrize("legacy_error", ["SMTPAuthenticationError", "SMTPAuthenticationError:535"])
def test_common_email_cooldown_leaves_other_email_queued_and_max_working(store, monkeypatch, legacy_error):
    factory, settings, _ = store
    blocked_id, _ = queue_auth(store)
    other_id, _ = queue_auth(store, user_index=1)
    with factory() as db:
        blocked = db.get(Outbox, blocked_id)
        blocked.status, blocked.last_error, blocked.next_attempt_at = "retry", legacy_error, NOW + 900
        reply = Outbox(channel="max", kind="bot_reply", payload={"recipient_id": "123", "reply_key": "help"},
                       next_attempt_at=NOW, created_at=NOW + 2)
        db.add(reply); db.commit(); reply_id = reply.id
    monkeypatch.setattr("app.worker.send_email", lambda *_: pytest.fail("SMTP cooldown bypassed"))
    monkeypatch.setattr("app.integrations.messengers.deliver", lambda *_: {"delivered": True})
    assert process_once(factory, settings, now=NOW)
    assert not process_once(factory, settings, now=NOW)
    with factory() as db:
        assert db.get(Outbox, reply_id).status == "delivered"
        assert db.get(Outbox, other_id).status == "pending" and db.get(Outbox, other_id).attempts == 0


def test_resend_cannot_bypass_provider_cooldown_and_recovers_after_expiry(store, monkeypatch):
    factory, settings, _ = store
    old_id, _ = queue_auth(store)
    with factory() as db:
        old = db.get(Outbox, old_id)
        old.status, old.last_error, old.next_attempt_at = "retry", "SMTPAuthenticationError:535", NOW + 900
        db.commit()
    new_id, _ = queue_auth(store)
    sent = []
    monkeypatch.setattr("app.worker.send_email", lambda payload, _: sent.append(payload) or {"delivered": True})
    assert not process_once(factory, settings, now=NOW + 899)
    with factory() as db:
        old = db.get(Outbox, old_id)
        assert old.status == "cancelled" and old.last_error == "SMTPAuthenticationError:535"
        assert db.get(Outbox, new_id).status == "pending"
    assert process_once(factory, settings, now=NOW + 900)
    assert len(sent) == 1
    with factory() as db:
        assert db.get(Outbox, new_id).status == "delivered"


def test_generic_failure_keeps_bounded_retries_and_deterministic_schedule(store, monkeypatch):
    factory, settings, _ = store
    row_id, _ = queue_auth(store)
    def unavailable(*_):
        raise TimeoutError("private provider message")
    monkeypatch.setattr("app.worker.send_email", unavailable)
    assert process_once(factory, settings, now=NOW)
    with factory() as db:
        row = db.get(Outbox, row_id)
        assert row.status == "retry" and row.last_error == "TimeoutError"
        assert row.next_attempt_at == NOW + 60
        row.attempts, row.next_attempt_at = 7, NOW + 60
        db.commit()
    assert process_once(factory, settings, now=NOW + 60)
    with factory() as db:
        assert db.get(Outbox, row_id).status == "failed"
