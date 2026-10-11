import json
import re
import secrets
import time
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from ..auth import start_session
from ..database import get_db
from ..models import LinkCode, MessengerLink, Notification, Report, User
from ..schemas import LinkInput, ReportInput, StrictModel
from ..security import authenticated, csrf, digest, rate_limit
from .bot_commands import handle_bot_command

router = APIRouter(tags=["notifications and integrations"], dependencies=[Depends(csrf)])
transport_router = APIRouter(tags=["signed messenger transport"])


def public_bot_url(platform: str, settings) -> str | None:
    """Public chat link only: never embed a token or an account linking code."""
    if platform != "max" or not getattr(settings, f"{platform}_bot_token", ""):
        return None
    username = getattr(settings, f"{platform}_bot_username", "").removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", username):
        return None
    return "https://max.ru/" + username


@router.get("/notifications")
def notifications(user: User = Depends(authenticated), db: Session = Depends(get_db)):
    return {"items": [{"id": x.id, "kind": x.kind, "payload": x.payload, "created_at": x.created_at, "read_at": x.read_at} for x in db.scalars(select(Notification).where(Notification.user_id == user.id).order_by(Notification.created_at.desc()).limit(100))]}


@router.post("/notifications/{identifier}/read")
def read(identifier: str, user: User = Depends(authenticated), db: Session = Depends(get_db)):
    row = db.scalar(select(Notification).where(Notification.id == identifier, Notification.user_id == user.id))
    if not row:
        raise HTTPException(404, "Уведомление не найдено")
    row.read_at = row.read_at or time.time()
    db.commit()
    return {"id": row.id, "read_at": row.read_at}


@router.get("/integrations/status")
def status(request: Request, user: User = Depends(authenticated), db: Session = Depends(get_db)):
    settings = request.app.state.settings
    links = {x.platform for x in db.scalars(select(MessengerLink).where(MessengerLink.user_id == user.id))}
    return {"email": {"configured": bool(settings.smtp_host), "verified": user.email_verified}, "llm": {"configured": bool(settings.llm_base_url and settings.llm_model), "fallback_available": True}, "fsp": {"mode": "synthetic_demo_adapter", "external_verified": False},
            "max": {"configured": bool(settings.max_bot_token), "linked": "max" in links, "url": public_bot_url("max", settings)}}


@router.post("/integrations/messenger/link-code")
def link_code(body: LinkInput, request: Request, user: User = Depends(authenticated), db: Session = Depends(get_db)):
    if not getattr(request.app.state.settings, f"{body.platform}_bot_token", ""):
        raise HTTPException(503, "Этот бот пока не настроен")
    rate_limit(db, "issue_link_code", user.id, 10, 3600)
    db.execute(delete(LinkCode).where(LinkCode.user_id == user.id, LinkCode.platform == body.platform, LinkCode.used_at.is_(None)))
    raw = secrets.token_urlsafe(24)
    expires = time.time() + 600
    db.add(LinkCode(id=digest(raw), user_id=user.id, platform=body.platform, expires_at=expires))
    db.commit()
    return {"code": raw, "expires_at": expires, "platform": body.platform, "url": public_bot_url(body.platform, request.app.state.settings), "instruction": "Отправьте новому боту /start " + raw}


class LaunchInput(LinkInput):
    init_data: str = Field(min_length=10, max_length=20000)


class ConsumeInput(LaunchInput):
    code: str = Field(min_length=20, max_length=120)


def signed_identity(body, settings):
    from .messengers import validate_launch
    token = settings.max_bot_token
    if not token:
        raise HTTPException(503, "Этот бот пока не настроен")
    try:
        return validate_launch(body.platform, body.init_data, token)
    except (ValueError, TypeError, KeyError):
        raise HTTPException(401, "Подпись или срок запуска мессенджера недействительны")


@transport_router.post("/integrations/messenger/consume")
def consume(body: ConsumeInput, request: Request, db: Session = Depends(get_db)):
    proof = signed_identity(body, request.app.state.settings)
    identity = str(proof["user_id"])
    reply = handle_bot_command(body.platform, identity, "/start " + body.code, "launch-" + digest(body.code), request.app.state.settings, db)
    linked = db.scalar(select(MessengerLink).where(MessengerLink.platform == body.platform, MessengerLink.recipient_id == identity))
    code = db.get(LinkCode, digest(body.code))
    if reply not in {"linked", None} or not linked or not code or linked.user_id != code.user_id:
        raise HTTPException(409, "Связывание недоступно: код истёк или аккаунт уже связан")
    return {"message": "Мессенджер связан с подтверждённым email-аккаунтом"}


@transport_router.post("/integrations/messenger/launch")
def launch(body: LaunchInput, request: Request, response: Response, db: Session = Depends(get_db)):
    proof = signed_identity(body, request.app.state.settings)
    row = db.scalar(select(MessengerLink).where(MessengerLink.platform == body.platform, MessengerLink.recipient_id == str(proof["user_id"])))
    user = db.get(User, row.user_id) if row else None
    if not user or not user.email_verified or user.disabled:
        raise HTTPException(403, "Сначала войдите по email и свяжите мессенджер одноразовым кодом")
    rate_limit(db, "messenger_launch", user.id, 60, 3600)
    return start_session(db, user, response, request.app.state.settings, messenger=True)


@transport_router.post("/integrations/{platform}/webhook")
async def webhook(platform: str, request: Request, db: Session = Depends(get_db)):
    from .messengers import extract_update, validate_webhook_secret
    if platform != "max":
        raise HTTPException(404, "Платформа не найдена")
    header = "X-Max-Bot-Api-Secret"
    if not validate_webhook_secret(platform, request.headers.get(header, ""), request.app.state.settings):
        raise HTTPException(401, "Недействительная подпись доставки")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 65536:
            raise HTTPException(413, "Слишком большой callback")
    try:
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError()
        update = extract_update(platform, payload)
    except (ValueError, TypeError, KeyError):
        raise HTTPException(400, "Некорректный callback")
    if update:
        handle_bot_command(platform, update["user_id"], update["text"], update["event_id"], request.app.state.settings, db)
    return {"ok": True}


@router.post("/feedback/report", status_code=201)
def report(body: ReportInput, user: User = Depends(authenticated), db: Session = Depends(get_db)):
    rate_limit(db, "report", user.id, 10, 86400)
    item = Report(user_id=user.id, **body.model_dump())
    db.add(item)
    db.commit()
    return {"id": item.id, "message": "Жалоба сохранена для рассмотрения оператором"}
