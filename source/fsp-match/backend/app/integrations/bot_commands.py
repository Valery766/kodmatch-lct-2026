import time
from sqlalchemy import select, update
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from ..database import build_database, dispose_database
from ..models import Inbox, LinkCode, MessengerLink, Outbox, User
from ..security import digest, rate_limit

BOT_REPLY_LIMIT = 12
BOT_REPLY_WINDOW_SECONDS = 60


def handle_bot_command(platform, user_id, text, event_id, settings, db=None):
    """Trusted platform callback only. Returns fixed reply key or None for replay.

    Persists inbox, link action and reply outbox atomically. Never sends network
    messages here and never returns email, contact details or raw link code.
    """
    if platform != "max" or not user_id or len(str(user_id)) > 80 or len(event_id) > 120:
        return "error"
    if db is None:
        engine, factory = build_database(settings.database_url)
        try:
            with factory() as session:
                return handle_bot_command(platform, user_id, text, event_id, settings, session)
        finally:
            dispose_database(engine)
    event_key = platform + ":" + event_id
    if db.get(Inbox, event_key):
        return None
    # Every new trusted event is acknowledged and deduplicated, but responses
    # share one recipient quota, including "limited" replies. Suppression is
    # independent from linking/unlinking so users can always revoke a link.
    reply_allowed = True
    try:
        rate_limit(db, "bot_reply:" + platform, str(user_id), BOT_REPLY_LIMIT, BOT_REPLY_WINDOW_SECONDS)
    except HTTPException as exc:
        if exc.status_code != 429:
            raise
        reply_allowed = False
    link = db.scalar(select(MessengerLink).where(MessengerLink.platform == platform, MessengerLink.recipient_id == str(user_id)).with_for_update())
    owner = db.get(User, link.user_id) if link else None
    command = str(text or "").strip()[:500]
    reply = "help"
    if command.startswith("/start "):
        try:
            rate_limit(db, "bot_link:" + platform, str(user_id), 6, 600)
        except HTTPException as exc:
            if exc.status_code != 429:
                raise
            db.add(Inbox(id=event_key, platform=platform))
            if reply_allowed:
                db.add(Outbox(user_id=owner.id if owner else None, channel=platform, kind="bot_reply", payload={"recipient_id": str(user_id), "reply_key": "limited"}))
            db.commit()
            return "limited"
        raw = command.split(maxsplit=1)[1].strip()
        token = db.scalar(select(LinkCode).where(LinkCode.id == digest(raw), LinkCode.platform == platform).with_for_update())
        if not token or token.used_at or token.expires_at <= time.time():
            reply = "expired"
        else:
            token_owner = db.get(User, token.user_id)
            existing = db.scalar(select(MessengerLink).where(MessengerLink.platform == platform, MessengerLink.user_id == token.user_id))
            if link and link.user_id != token.user_id or existing and existing.recipient_id != str(user_id):
                reply = "already_linked"
            elif not token_owner or not token_owner.email_verified or token_owner.disabled:
                reply = "error"
            else:
                if not link:
                    db.add(MessengerLink(user_id=token.user_id, platform=platform, recipient_id=str(user_id)))
                token.used_at = time.time()
                owner = token_owner
                reply = "linked"
    elif command == "/unlink":
        if link:
            db.execute(update(Outbox).where(Outbox.user_id == link.user_id, Outbox.channel == platform, Outbox.kind != "bot_reply", Outbox.status.in_(["pending", "retry"])).values(status="cancelled", last_error="messenger_unlinked"))
            db.delete(link)
            reply = "unlinked"
        else:
            reply = "not_linked"
    elif command == "/status":
        reply = "status_" + owner.role if owner and owner.email_verified and not owner.disabled else "not_linked"
    elif command in {"/start", "/help", ""}:
        reply = "help"
    db.add(Inbox(id=event_key, platform=platform))
    if reply_allowed:
        db.add(Outbox(user_id=owner.id if owner else None, channel=platform, kind="bot_reply", payload={"recipient_id": str(user_id), "reply_key": reply}))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.get(Inbox, event_key):
            return None
        return "already_linked"
    return reply
