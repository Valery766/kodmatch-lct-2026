"""Trusted event bursts cannot amplify the shared delivery queue without bound."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.database import build_database, dispose_database
from app.integrations.bot_commands import BOT_REPLY_LIMIT, handle_bot_command
from app.models import Base, Inbox, LinkCode, MessengerLink, Outbox, RateBucket, User
from app.security import digest


@pytest.fixture
def store(tmp_path):
    engine, factory = build_database("sqlite:///" + str(tmp_path / "quota.db"))
    Base.metadata.create_all(engine)
    with factory() as db:
        user = User(email="quota@demo.example.com", password_hash="not-loginable-synthetic-fixture",
                    role="candidate", email_verified=True, is_demo=True, consent_version="synthetic-test")
        db.add(user)
        db.commit()
        user_id = user.id
    yield factory, user_id, SimpleNamespace()
    dispose_database(engine)


def counts(factory):
    with factory() as db:
        return {"inbox": db.scalar(select(func.count()).select_from(Inbox)),
                "replies": db.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == "bot_reply"))}


@pytest.mark.parametrize("text", ["/help", "/status", "/start", "/unknown", "/start invalid_synthetic_code"])
def test_100_unique_events_are_acknowledged_with_at_most12_queued_replies(store, text):
    factory, _, settings = store
    with factory() as db:
        for index in range(100):
            handle_bot_command("max", "77701", text, f"burst-{index}", settings, db)
    assert counts(factory) == {"inbox": 100, "replies": BOT_REPLY_LIMIT}
    with factory() as db:
        payloads = list(db.scalars(select(Outbox.payload)))
    assert "invalid_synthetic_code" not in json.dumps(payloads)
    if text.startswith("/start "):
        assert sum(x["reply_key"] == "limited" for x in payloads) == BOT_REPLY_LIMIT - 6


def test_100_help_then100_limited_code_attempts_share_one_reply_quota(store):
    factory, _, settings = store
    with factory() as db:
        for index in range(100):
            handle_bot_command("max", "77701", "/help", f"help-{index}", settings, db)
        for index in range(100):
            handle_bot_command("max", "77701", "/start invalid", f"code-{index}", settings, db)
    assert counts(factory) == {"inbox": 200, "replies": BOT_REPLY_LIMIT}


def test_duplicate_does_not_consume_quota_and_recipients_are_independent(store):
    factory, _, settings = store
    with factory() as db:
        assert handle_bot_command("max", "77701", "/help", "same-event", settings, db) == "help"
        for _ in range(100):
            assert handle_bot_command("max", "77701", "/help", "same-event", settings, db) is None
        for index in range(BOT_REPLY_LIMIT - 1):
            handle_bot_command("max", "77701", "/help", f"first-{index}", settings, db)
        for index in range(BOT_REPLY_LIMIT):
            handle_bot_command("max", "77702", "/help", f"second-{index}", settings, db)
    assert counts(factory) == {"inbox": BOT_REPLY_LIMIT * 2, "replies": BOT_REPLY_LIMIT * 2}


def test_valid_link_and_unlink_still_execute_when_reply_quota_exhausted(store):
    factory, user_id, settings = store
    raw = "synthetic_link_code_valid_after_help_quota"
    with factory() as db:
        db.add(LinkCode(id=digest(raw), user_id=user_id, platform="max", expires_at=time.time() + 600))
        db.commit()
        for index in range(BOT_REPLY_LIMIT):
            handle_bot_command("max", "77701", "/help", f"help-{index}", settings, db)
        assert handle_bot_command("max", "77701", "/start " + raw, "link-valid", settings, db) == "linked"
        assert db.scalar(select(MessengerLink)).user_id == user_id
        assert db.get(LinkCode, digest(raw)).used_at is not None
        notification = Outbox(user_id=user_id, channel="max", kind="invitation",
                              payload={"recipient_id": "77701", "status": "pending"})
        db.add(notification)
        db.commit()
        notification_id = notification.id
        assert handle_bot_command("max", "77701", "/unlink", "unlink-always", settings, db) == "unlinked"
        assert db.scalar(select(MessengerLink)) is None
        assert db.get(Outbox, notification_id).status == "cancelled"
    assert counts(factory) == {"inbox": BOT_REPLY_LIMIT + 2, "replies": BOT_REPLY_LIMIT}


def test_reply_quota_recovers_after_window_reset(store):
    factory, _, settings = store
    with factory() as db:
        for index in range(BOT_REPLY_LIMIT + 1):
            handle_bot_command("max", "77701", "/help", f"before-{index}", settings, db)
        bucket = db.get(RateBucket, "bot_reply:max:" + digest("77701"))
        bucket.reset_at = time.time() - 1
        db.commit()
        handle_bot_command("max", "77701", "/status", "after-reset", settings, db)
    assert counts(factory)["replies"] == BOT_REPLY_LIMIT + 1


def test_parallel100_event_burst_never_exceeds_shared_recipient_quota(store):
    factory, _, settings = store
    def event(index):
        with factory() as db:
            return handle_bot_command("max", "77701", "/help", f"parallel-{index}", settings, db)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(event, range(100)))
    assert counts(factory) == {"inbox": 100, "replies": BOT_REPLY_LIMIT}
