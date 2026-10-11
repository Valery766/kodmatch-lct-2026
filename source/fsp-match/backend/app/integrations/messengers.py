"""MAX transport and signed Mini App launch validation.

No platform credential, initData, or response body is returned to the caller.
Contacts are never copied into outgoing notifications. Live delivery is separate
from the candidate's invitation decision and its contact permission.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import ssl
import time
from urllib.parse import parse_qsl, urlsplit

import httpx


PLATFORMS = {"max"}
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_HEX = re.compile(r"^[0-9a-fA-F]{64}$")
_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")


def _pairs(raw: str) -> dict[str, str]:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 16384:
        raise ValueError("Invalid launch data")
    if _BAD_PERCENT.search(raw):
        raise ValueError("Invalid launch encoding")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=32)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid launch encoding") from exc
    result = {}
    for key, value in pairs:
        if key in result or not _KEY.fullmatch(key) or "\n" in value or "\r" in value:
            raise ValueError("Ambiguous launch data")
        result[key] = value
    return result


def validate_launch(platform: str, init_data: str, token: str, *,
                    now: int | None = None, max_age: int = 300) -> dict:
    """Validate bot-token HMAC and freshness; return only the platform identity.

    MAX accepts either window.WebApp.initData or its URL fragment wrapper.
    This is identity evidence, not consent to link an account: the API also
    consumes a single-use link code.
    """
    if platform not in PLATFORMS or not token or not 1 <= max_age <= 3600:
        raise ValueError("Launch validation is unavailable")
    if platform == "max" and isinstance(init_data, str):
        if init_data.startswith(("https://", "http://")):
            init_data = urlsplit(init_data).fragment
        elif init_data.startswith("#"):
            init_data = init_data[1:]
        if init_data.startswith("WebAppData=") or "&WebAppData=" in init_data:
            outer = _pairs(init_data)
            init_data = outer.get("WebAppData", "")
    fields = _pairs(init_data)
    signature = fields.pop("hash", "")
    if not _HEX.fullmatch(signature):
        raise ValueError("Invalid launch signature")
    data = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise ValueError("Invalid launch signature")
    try:
        date = fields["auth_date"]
        if not re.fullmatch(r"[0-9]{1,12}", date):
            raise ValueError
        timestamp = int(date)
        user = json.loads(fields["user"])
        identity = user["id"]
        if not isinstance(identity, int) or isinstance(identity, bool) or not 0 < identity < 2**63:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid launch identity") from exc
    current = int(time.time()) if now is None else now
    if timestamp > current + 30 or timestamp < current - max_age:
        raise ValueError("Expired launch data")
    return {"platform": platform, "user_id": str(identity), "auth_date": timestamp}


def _value(settings, name: str, default=""):
    value = getattr(settings, name, default)
    return value.get_secret_value() if hasattr(value, "get_secret_value") else value


def _text(payload: dict) -> str:
    # Deliberately do not serialize arbitrary JSON: it can contain a private token
    # or profile contact. Notification metadata is a small explicit allowlist.
    replies = {
        "help": "Откройте КОД.МЭТЧ, подтвердите email и свяжите мессенджер в кабинете. Команды: /status – состояние связи, /unlink – отключить уведомления.",
        "linked": "Кабинет связан. Приглашения и изменения статуса будут приходить сюда. Контакты раскрываются только по вашему решению в кабинете.",
        "unlinked": "Связь с кабинетом отключена. Уведомления в этот мессенджер остановлены.",
        "not_linked": "Кабинет пока не связан. Получите одноразовый код в разделе «Мессенджеры» и отправьте /start КОД.",
        "expired": "Код недействителен или истёк. Получите новый код в своём кабинете.",
        "already_linked": "Эта связь уже существует или принадлежит другому кабинету. Отключите прежнюю связь в её кабинете перед повторной привязкой.",
        "status_candidate": "Связан кабинет специалиста. Приглашения доступны в разделе «Приглашения».",
        "status_employer": "Связан кабинет работодателя. Ответы специалистов доступны в разделе «Приглашения».",
        "limited": "Слишком много попыток. Повторите позже.",
        "error": "Не удалось выполнить команду. Попробуйте позже или откройте кабинет.",
    }
    if payload.get("reply_key") in replies:
        return "КОД.МЭТЧ\n" + replies[payload["reply_key"]]
    statuses = {"sent": "Новое приглашение", "accepted": "Приглашение принято",
                "rejected": "Приглашение отклонено", "viewed": "Приглашение просмотрено"}
    lines = ["КОД.МЭТЧ", statuses.get(payload.get("status"), "Обновление в вашем кабинете")]
    company = payload.get("company_name", payload.get("company"))
    if isinstance(company, str) and company.strip():
        lines.append(company.strip()[:160].replace("\n", " ").replace("\r", " "))
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        lines.append(title.strip()[:180].replace("\n", " ").replace("\r", " "))
    lo, hi = payload.get("salary_min"), payload.get("salary_max")
    if (isinstance(lo, int) and not isinstance(lo, bool) and isinstance(hi, int)
            and not isinstance(hi, bool) and 0 < lo <= hi <= 100000000):
        lines.append(f"Предложение: {lo:,}–{hi:,} ₽/мес. до вычета НДФЛ.".replace(",", " "))
    lines.append("Подробности и решение – в личном кабинете.")
    return "\n".join(lines)


def validate_webhook_secret(platform: str, received: str | None, settings) -> bool:
    if platform not in PLATFORMS or not received or len(received) > 256:
        return False
    expected = _value(settings, f"{platform}_webhook_secret")
    if not isinstance(expected, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,256}", expected):
        return False
    return hmac.compare_digest(expected.encode(), received.encode())


def extract_update(platform: str, payload: dict) -> dict | None:
    """Normalize private-chat commands AFTER webhook header authentication.

    Identity is taken from the sender object, never from message text or a
    forwarded sender. Inbox keys retain no names/phones or raw command codes.
    This function alone does not authenticate an HTTP request.
    """
    try:
        if not isinstance(payload, dict):
            return None
        if platform == "max":
            kind = payload.get("update_type")
            if kind == "message_created":
                message = payload["message"]
                if message["recipient"].get("chat_type") != "dialog":
                    return None
                sender = message["sender"]
                user_id = sender["user_id"]
                if sender.get("is_bot"):
                    return None
                mid = message["body"]["mid"]
                if not isinstance(mid, str) or not mid or len(mid) > 128:
                    return None
                event_id = "max:message:" + hashlib.sha256(mid.encode()).hexdigest()
                text = message["body"].get("text", "")
            elif kind == "bot_started":
                sender = payload["user"]
                user_id = sender["user_id"]
                if sender.get("is_bot"):
                    return None
                timestamp = payload["timestamp"]
                if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0:
                    return None
                event_id = "max:started:" + hashlib.sha256(
                    f"{user_id}:{payload.get('chat_id')}:{timestamp}".encode()).hexdigest()
                start = payload.get("payload", "")
                if not isinstance(start, str):
                    return None
                text = "/start " + start
            else:
                return None
        else:
            return None
        if (not isinstance(user_id, int) or isinstance(user_id, bool) or not 0 < user_id < 2**63
                or not isinstance(text, str) or len(text) > 512 or not text.startswith("/")):
            return None
        return {"event_id": event_id, "user_id": str(user_id), "text": text}
    except (KeyError, TypeError, ValueError):
        return None


def _failure(error: str, retry_after: int | None = None) -> dict:
    return {"delivered": False, "external_id": None, "retry_after": retry_after, "error": error}


def deliver(platform: str, recipient_id: str, payload: dict, settings) -> dict:
    """Send to an explicitly linked platform user. Safe retry metadata only."""
    if platform not in PLATFORMS:
        return _failure("unsupported_platform")
    if not re.fullmatch(r"[1-9][0-9]{0,18}", str(recipient_id)) or int(recipient_id) >= 2**63:
        return _failure("invalid_recipient")
    token = _value(settings, f"{platform}_bot_token")
    if not token:
        return _failure("configuration_missing", 300)
    app_url = _value(settings, "app_url", "")
    parsed = urlsplit(app_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return _failure("https_app_url_required")
    headers, params = {}, None
    text = _text(payload)
    verify: bool | ssl.SSLContext = True
    base = _value(settings, "max_api_base_url", "https://platform-api2.max.ru").rstrip("/")
    # Pin the official API host to prevent a credential exfiltration through
    # an accidental or malicious runtime URL override.
    if base != "https://platform-api2.max.ru":
        return _failure("invalid_max_api_host")
    url = base + "/messages"
    headers = {"Authorization": token}
    params = {"user_id": str(recipient_id), "disable_link_preview": "true"}
    username = _value(settings, "max_bot_username", "").removeprefix("@")
    if username and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", username):
        return _failure("invalid_max_bot_username")
    # MAX OpenAppButton.web_app is the bot's public name, not the app URL.
    # Without a registered name, explicitly offer the ordinary web cabinet.
    button = ({"type": "open_app", "text": "Открыть КОД.МЭТЧ", "web_app": username}
              if username else {"type": "link", "text": "Открыть веб-кабинет", "url": app_url})
    body = {"text": text, "attachments": [{"type": "inline_keyboard", "payload": {
        "buttons": [[button]]}}]}
    ca_path = _value(settings, "max_ca_bundle")
    if ca_path:
        try:
            verify = ssl.create_default_context(cafile=ca_path)
        except (OSError, ssl.SSLError):
            return _failure("invalid_ca_bundle")
    try:
        with httpx.Client(timeout=12, verify=verify, follow_redirects=False) as client:
            response = client.post(url, headers=headers, params=params, json=body)
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "60")
            try:
                retry = max(1, min(3600, int(retry)))
            except (TypeError, ValueError):
                retry = 60
            return _failure("rate_limited", retry)
        if response.status_code >= 500:
            return _failure("platform_unavailable", 60)
        if response.status_code in (401, 403):
            return _failure("platform_auth_or_permission")
        if not 200 <= response.status_code < 300:
            return _failure("platform_rejected")
        if len(response.content) > 1048576:
            return _failure("invalid_platform_response", 60)
        data = response.json()
        external_id = data.get("message", {}).get("body", {}).get("mid")
        if external_id is None:
            return _failure("invalid_platform_response", 60)
        return {"delivered": True, "external_id": str(external_id), "retry_after": None, "error": None}
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        # Do not include exception text or platform response bodies: both may
        # contain private credentials or recipient data.
        return _failure("transport_error", 60)
