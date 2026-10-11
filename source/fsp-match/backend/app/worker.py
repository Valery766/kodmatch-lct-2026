import json
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.parse import parse_qs, urlsplit
from sqlalchemy import or_, select
from .config import get_settings
from .database import build_database, dispose_database
from .models import AuthToken, MessengerLink, Outbox
from .security import digest


AUTH_EMAIL_KINDS = {"confirm_email", "reset_password"}
SMTP_AUTH_COOLDOWN_SECONDS = 900


def auth_email_is_current(db, row, now):
    """Check legacy and current queued links without exposing their raw token."""
    if row.kind not in AUTH_EMAIL_KINDS:
        return True
    try:
        values = parse_qs(urlsplit(row.payload.get("link", "")).query,
                          strict_parsing=True, max_num_fields=8).get("token", [])
        if len(values) != 1 or not values[0]:
            return False
        token = db.get(AuthToken, digest(values[0]))
    except (AttributeError, TypeError, ValueError):
        return False
    return bool(token and token.user_id == row.user_id and token.kind == row.kind
                and token.used_at is None and token.expires_at > now)


def send_email(payload, settings):
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = settings.smtp_from, payload["to"], str(payload["subject"]).replace("КОД." + "МАТЧ", "КОД.МЭТЧ")
    if settings.smtp_reply_to:
        message["Reply-To"] = settings.smtp_reply_to
    text = "КОД.МЭТЧ\n\n"
    if payload.get("link"):
        text += "Откройте одноразовую ссылку:\n" + payload["link"] + "\nЕсли вы не запрашивали это действие, проигнорируйте письмо."
    else:
        notification = payload.get("notification", {})
        text += "Новое событие в вашем кабинете.\n" + str(notification.get("title", "")) + "\n" + str(notification.get("app_url", settings.app_url))
    message.set_content(text)
    smtp_class = smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
    kwargs = {"timeout": 15}
    if settings.smtp_ssl:
        kwargs["context"] = ssl.create_default_context()
    with smtp_class(settings.smtp_host, settings.smtp_port, **kwargs) as smtp:
        if settings.smtp_starttls and not settings.smtp_ssl:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        refused = smtp.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    return {"delivered": True, "external_id": None}


def process_once(factory, settings, now=None):
    now = time.time() if now is None else now
    with factory() as db:
        # One refused SMTP account affects every recipient. Pause this channel
        # rather than trying the same blocked login for each queued message.
        # Old deployments stored only the class; both formats remain readable.
        email_paused = db.scalar(select(Outbox.id).where(
            Outbox.channel == "email", Outbox.next_attempt_at > now,
            or_(Outbox.last_error == "SMTPAuthenticationError",
                Outbox.last_error.like("SMTPAuthenticationError:%")),
        ).limit(1)) is not None
        query = select(Outbox).where(Outbox.next_attempt_at <= now, or_(Outbox.status.in_(["pending", "retry"]), (Outbox.status == "processing") & (Outbox.lease_until < now)))
        if email_paused:
            query = query.where(Outbox.channel != "email")
        query = query.order_by(Outbox.created_at).limit(1).with_for_update(skip_locked=True)
        row = db.scalar(query)
        if not row:
            return False
        # Retired channels must never reach a transport, including old queued
        # bot replies. Preserve their rows and the saved links for audit.
        if row.channel not in {"email", "max"}:
            row.status, row.last_error, row.lease_until = "cancelled", "unsupported_platform", None
            db.commit()
            return True
        if row.channel == "email" and not auth_email_is_current(db, row, now):
            old_error = row.last_error or ""
            row.status, row.lease_until = "cancelled", None
            if old_error.split(":", 1)[0] != "SMTPAuthenticationError":
                row.last_error = "invalid_auth_token"
            db.commit()
            return True
        row.status, row.lease_until, row.attempts = "processing", now + 120, row.attempts + 1
        db.commit()
        try:
            if row.channel == "email":
                result = send_email(row.payload, settings)
            else:
                if row.kind != "bot_reply":
                    linked = db.scalar(select(MessengerLink.id).where(MessengerLink.user_id == row.user_id, MessengerLink.platform == row.channel, MessengerLink.recipient_id == row.payload.get("recipient_id")))
                    if not linked:
                        row.status, row.last_error, row.lease_until = "cancelled", "messenger_unlinked", None
                        db.commit()
                        return True
                from .integrations.messengers import deliver
                result = deliver(row.channel, row.payload.get("recipient_id", ""), row.payload, settings)
        except smtplib.SMTPAuthenticationError as exc:
            # Provider text can contain credentials, URLs and mailbox names.
            # Store only the stable class and validated numeric SMTP code.
            code = exc.smtp_code
            safe_code = f":{code}" if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599 else ""
            result = {"delivered": False, "error": "SMTPAuthenticationError" + safe_code,
                      "retry_after": SMTP_AUTH_COOLDOWN_SECONDS}
        except Exception as exc:
            # Only exception class enters storage/log; SMTP/API may embed tokens or email.
            result = {"delivered": False, "error": type(exc).__name__}
        if result.get("delivered"):
            row.status, row.external_id, row.last_error = "delivered", result.get("external_id"), None
        else:
            auth_failure = row.channel == "email" and str(result.get("error", "")).split(":", 1)[0] == "SMTPAuthenticationError"
            # An account lock can outlast the recipient retry budget; keep
            # valid messages queued until access recovers or the token expires.
            row.status = "failed" if row.attempts >= 8 and not auth_failure else "retry"
            row.last_error = str(result.get("error", "delivery_failed"))[:120]
            delay = result.get("retry_after") or min(3600, 30 * 2 ** row.attempts)
            row.next_attempt_at = now + min(86400, max(5, int(delay)))
        row.lease_until = None
        db.commit()
    return True


def main():
    settings = get_settings()
    engine, factory = build_database(settings.database_url)
    try:
        while True:
            if not process_once(factory, settings):
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        dispose_database(engine)


if __name__ == "__main__":
    main()
