import hashlib
import hmac
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest

from app.integrations.messengers import deliver, validate_launch, extract_update, validate_webhook_secret


TOKEN = "synthetic_test_token_not_a_real_credential"
NOW = 1800000000


def signed(**changes):
    fields = {"auth_date": str(NOW), "user": json.dumps({"id": 778899, "first_name": "Тест"}),
              "query_id": "synthetic"}
    fields.update(changes)
    data = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.mark.parametrize("platform", ["max"])
def test_valid_unicode_identity_and_signature_mutation(platform):
    raw = signed()
    assert validate_launch(platform, raw, TOKEN, now=NOW)["user_id"] == "778899"
    with pytest.raises(ValueError):
        validate_launch(platform, raw.replace("synthetic", "changed"), TOKEN, now=NOW)


def test_max_double_encoding_wrapper_and_duplicates():
    raw = urlencode({"WebAppData": signed(), "WebAppPlatform": "web"})
    assert validate_launch("max", "https://example.test/#" + raw, TOKEN, now=NOW)["user_id"] == "778899"
    for malformed in [raw + "&WebAppData=x", signed() + "&auth_date=0", signed() + "&hash=x"]:
        with pytest.raises(ValueError):
            validate_launch("max", malformed, TOKEN, now=NOW)


@pytest.mark.parametrize("changes", [{"auth_date": str(NOW - 301)}, {"auth_date": str(NOW + 31)},
                                    {"user": '{"id":true}'}, {"user": '{"id":-1}'},
                                    {"user": '{"id":"778899"}'}, {"auth_date": "nan"}])
def test_bad_identity_or_age_rejected_even_when_signed(changes):
    with pytest.raises(ValueError):
        validate_launch("max", signed(**changes), TOKEN, now=NOW)


def test_malformed_encoding_rejected():
    for raw in ["user=%ff&hash=x", "user=%QZ&hash=x", "a=x\na=y&hash=x", "x=" * 9000]:
        with pytest.raises(ValueError):
            validate_launch("max", raw, TOKEN, now=NOW)


def settings(**kw):
    return SimpleNamespace(app_url="https://fsp.digital-projects.tech", telegram_bot_token=TOKEN,
                           max_bot_token=TOKEN, **kw)


def intercept(monkeypatch, response):
    requests = []
    def handler(request):
        requests.append(request)
        return response
    original = httpx.Client
    monkeypatch.setattr("app.integrations.messengers.httpx.Client",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return requests


@pytest.mark.parametrize("platform,response", [
    ("max", httpx.Response(200, json={"message": {"body": {"mid": "mid1"}}})),
])
def test_delivery_filters_private_payload_and_uses_pinned_host(monkeypatch, platform, response):
    requests = intercept(monkeypatch, response)
    result = deliver(platform, "778899", {"status": "sent", "company_name": "Тест", "salary_min": 100000,
                      "salary_max": 150000, "email": "private@example.test", "token": "PRIVATE",
                      "contacts": {"phone": "PRIVATE"}, "app_url": "https://evil.test"}, settings())
    assert result["delivered"] is True
    body = requests[0].content.decode()
    assert "PRIVATE" not in body and "private@example.test" not in body and "evil.test" not in body
    assert requests[0].url.host == "platform-api2.max.ru"
    assert requests[0].headers["Authorization"] == TOKEN
    assert TOKEN not in str(requests[0].url)


def test_max_open_app_uses_registered_bot_name_not_site_url(monkeypatch):
    requests = intercept(monkeypatch, httpx.Response(200, json={"message": {"body": {"mid": "mid1"}}}))
    result = deliver("max", "778899", {}, settings(max_bot_username="@synthetic_fsp_bot"))
    assert result["delivered"]
    button = json.loads(requests[0].content)["attachments"][0]["payload"]["buttons"][0][0]
    assert button == {"type": "open_app", "text": "Открыть КОД.МЭТЧ", "web_app": "synthetic_fsp_bot"}
    assert deliver("max", "778899", {}, settings(max_bot_username="https://evil.test"))["error"] == "invalid_max_bot_username"


def test_429_respects_max_retry_after_without_returning_response_secret(monkeypatch):
    intercept(monkeypatch, httpx.Response(429, headers={"Retry-After": "25"}, json={"description": TOKEN}))
    result = deliver("max", "778899", {}, settings())
    assert result["retry_after"] == 25 and TOKEN not in repr(result)


def test_refuses_unconfigured_host_and_non_https_app(monkeypatch):
    assert deliver("max", "778899", {}, settings(max_api_base_url="https://evil.test"))["error"] == "invalid_max_api_host"
    s = settings()
    s.app_url = "http://localhost"
    assert deliver("max", "778899", {}, s)["error"] == "https_app_url_required"
    s.max_bot_token = ""
    assert deliver("max", "778899", {}, s)["error"] == "configuration_missing"


def test_secret_required_and_constant_comparison():
    s = settings(max_webhook_secret="b" * 32)
    assert validate_webhook_secret("max", "b" * 32, s)
    assert not validate_webhook_secret("max", "a" * 32, s)
    assert not validate_webhook_secret("max", None, s)
    assert not validate_webhook_secret("max", "", settings())


def test_max_private_sender_proof_and_duplicate_key():
    payload = {"update_type": "message_created", "message": {"sender": {"user_id": 88},
               "recipient": {"chat_type": "dialog"}, "body": {"mid": "test-mid", "text": "/status"},
               "forwarded_sender": {"user_id": 999}}}
    result = extract_update("max", payload)
    assert result["user_id"] == "88" and result["text"] == "/status"
    assert result["event_id"].startswith("max:message:")
    payload["message"]["recipient"]["chat_type"] = "chat"
    assert extract_update("max", payload) is None
    payload["message"]["recipient"]["chat_type"] = "dialog"
    payload["message"]["sender"]["user_id"] = True
    assert extract_update("max", payload) is None


def test_max_message_and_start_shapes_and_no_group_linking():
    message = {"update_type": "message_created", "message": {"sender": {"user_id": 88},
        "recipient": {"chat_type": "dialog"}, "body": {"mid": "synthetic-id", "text": "/start CODE"}}}
    result = extract_update("max", message)
    assert result["user_id"] == "88" and result["text"] == "/start CODE"
    assert "CODE" not in result["event_id"]
    message["message"]["recipient"]["chat_type"] = "chat"
    assert extract_update("max", message) is None
    started = {"update_type": "bot_started", "user": {"user_id": 88}, "chat_id": 7,
               "timestamp": NOW * 1000, "payload": "CODE"}
    assert extract_update("max", started)["text"] == "/start CODE"


def test_fixed_command_reply_cannot_echo_raw_private_data(monkeypatch):
    requests = intercept(monkeypatch, httpx.Response(200, json={"message": {"body": {"mid": "reply-mid"}}}))
    deliver("max", "778899", {"reply_key": "linked", "reply_text": "PRIVATE", "token": "PRIVATE"}, settings())
    assert "PRIVATE" not in requests[0].content.decode()


def test_telegram_transport_is_unavailable_even_with_stale_credentials(monkeypatch):
    def unexpected_network(**kwargs):
        pytest.fail("Retired Telegram transport must not open an HTTP client")
    monkeypatch.setattr("app.integrations.messengers.httpx.Client", unexpected_network)
    legacy = settings(telegram_webhook_secret="synthetic-retired-webhook-secret")
    assert deliver("telegram", "778899", {"reply_key": "help"}, legacy)["error"] == "unsupported_platform"
    assert not validate_webhook_secret("telegram", legacy.telegram_webhook_secret, legacy)
    with pytest.raises(ValueError):
        validate_launch("telegram", signed(), TOKEN, now=NOW)
    assert extract_update("telegram", {"update_id": 42, "message": {"from": {"id": 88}, "chat": {"id": 88, "type": "private"}, "text": "/status"}}) is None
