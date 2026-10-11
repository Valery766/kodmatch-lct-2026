"""Real EmailMessage/header construction, without sending external mail."""
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.worker import send_email


def capture_smtp(monkeypatch):
    observed = {"connections": [], "messages": []}
    class SMTP:
        def __init__(self, *args, **kwargs):
            observed["connections"].append((args, kwargs))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def starttls(self, **kwargs):
            observed["tls"] = True
        def login(self, *args):
            observed["authenticated"] = True
        def send_message(self, message):
            observed["messages"].append(message)
            return {}
    monkeypatch.setattr("app.worker.smtplib.SMTP", SMTP)
    monkeypatch.setattr("app.worker.smtplib.SMTP_SSL", SMTP)
    return observed


def test_reply_to_sets_actual_header_without_changing_envelope(monkeypatch):
    observed = capture_smtp(monkeypatch)
    monkeypatch.setenv("SMTP_REPLY_TO", " support@EXAMPLE.COM ")
    settings = Settings(smtp_from="sender@example.com", smtp_tls_mode="starttls", smtp_host="smtp.example.com")
    assert send_email({"to": "candidate@example.com", "subject": "Confirmation", "link": "https://fsp.example.com/confirm?token=synthetic"}, settings)["delivered"]
    message = observed["messages"][0]
    assert str(message["Reply-To"]) == "support@example.com"
    assert str(message["From"]) == "sender@example.com" and str(message["To"]) == "candidate@example.com"
    assert observed["tls"] and "synthetic" in message.get_content()


def test_blank_reply_to_uses_sender_without_empty_header(monkeypatch):
    observed = capture_smtp(monkeypatch)
    send_email({"to": "candidate@example.com", "subject": "Event", "notification": {}}, Settings(smtp_reply_to=""))
    assert observed["messages"][0]["Reply-To"] is None


@pytest.mark.parametrize("bad", ["support@example.com\r\nBcc: attacker@example.com", "support@example.com\n", "support@example.com\x00", "one@example.com,two@example.com", "Support <support@example.com>", "not-a-mailbox"])
def test_reply_to_rejects_header_injection_and_multiple_recipients(bad):
    with pytest.raises(ValidationError):
        Settings(smtp_reply_to=bad)


def test_header_injection_after_unsafe_runtime_mutation_stops_before_smtp(monkeypatch):
    observed = capture_smtp(monkeypatch)
    settings = Settings()
    settings.smtp_reply_to = "support@example.com\r\nBcc: attacker@example.com"
    with pytest.raises(ValueError):
        send_email({"to": "candidate@example.com", "subject": "Event"}, settings)
    assert observed["connections"] == []


def test_brand_name_covers_legacy_queued_subject_and_current_body(monkeypatch):
    observed = capture_smtp(monkeypatch)
    send_email({"to": "candidate@example.com", "subject": "КОД." + "МАТЧ: подтверждение email", "link": "https://fsp.example.com/confirm?token=synthetic"}, Settings(smtp_from="КОД.МЭТЧ <sender@example.com>"))
    message = observed["messages"][0]
    assert str(message["Subject"]) == "КОД.МЭТЧ: подтверждение email"
    assert "КОД.МЭТЧ" in str(message["From"]) and message.get_content().startswith("КОД.МЭТЧ\n")
