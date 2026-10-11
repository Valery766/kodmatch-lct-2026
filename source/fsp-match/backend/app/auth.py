import secrets
import time
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import case, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .database import get_db
from .models import AuthSession, AuthToken, Outbox, User
from .schemas import Forgot, Login, Register, Reset, TokenInput
from .security import csrf, current_session, digest, password_hash, password_valid, rate_limit, user_public

router = APIRouter(prefix="/auth", tags=["authentication"], dependencies=[Depends(csrf)])


def set_cookie(response, name, value, settings, ttl, messenger=False):
    response.set_cookie(name, value, max_age=ttl, httponly=True, secure=settings.production, samesite="none" if messenger and settings.production else "lax", path="/")


def start_session(db, user, response, settings, messenger=False):
    raw = secrets.token_urlsafe(40)
    session = AuthSession(id=digest(raw), user_id=user.id, csrf_token=secrets.token_urlsafe(32), expires_at=time.time() + settings.session_ttl_seconds)
    db.add(session)
    db.commit()
    set_cookie(response, settings.cookie_name, raw, settings, settings.session_ttl_seconds, messenger=messenger)
    response.delete_cookie(settings.preauth_cookie_name, path="/")
    return {"user": user_public(user), "csrf_token": session.csrf_token}


def issue_token(db, user, kind, settings):
    db.execute(delete(AuthToken).where(AuthToken.user_id == user.id, AuthToken.kind == kind, AuthToken.used_at.is_(None)))
    # A replacement link invalidates older tokens. Remove their queued messages
    # as well so SMTP recovery cannot deliver obsolete links ahead of this one.
    db.execute(update(Outbox).where(
        Outbox.user_id == user.id, Outbox.channel == "email", Outbox.kind == kind,
        Outbox.status.in_(["pending", "retry", "failed"]),
    ).values(status="cancelled", last_error=case(
        (or_(Outbox.last_error == "SMTPAuthenticationError",
             Outbox.last_error.like("SMTPAuthenticationError:%")), Outbox.last_error),
        else_="superseded_auth_token"), lease_until=None))
    raw = secrets.token_urlsafe(40)
    duration = 86400 if kind == "confirm_email" else 3600
    db.add(AuthToken(id=digest(raw), user_id=user.id, kind=kind, expires_at=time.time() + duration))
    path = "confirm-email" if kind == "confirm_email" else "reset-password"
    db.add(Outbox(user_id=user.id, channel="email", kind=kind, payload={"to": user.email, "subject": "КОД.МЭТЧ: подтверждение email" if kind == "confirm_email" else "КОД.МЭТЧ: восстановление доступа", "link": settings.app_url.rstrip("/") + "/" + path + "?token=" + raw}))


@router.get("/me")
def me(request: Request, response: Response, db: Session = Depends(get_db)):
    session, user = current_session(request, db)
    if user:
        return {"user": user_public(user), "csrf_token": session.csrf_token}
    token = request.cookies.get(request.app.state.settings.preauth_cookie_name) or secrets.token_urlsafe(32)
    set_cookie(response, request.app.state.settings.preauth_cookie_name, token, request.app.state.settings, 3600)
    return {"user": None, "csrf_token": token}


@router.post("/register", status_code=201)
def register(body: Register, request: Request, db: Session = Depends(get_db)):
    if not request.app.state.settings.registration_enabled:
        raise HTTPException(503, "Публичная регистрация временно закрыта. Обратитесь в поддержку.")
    email = str(body.email).lower()
    rate_limit(db, "register", request.client.host, 6, 3600)
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "Регистрация недоступна для этого адреса. Воспользуйтесь входом или восстановлением.")
    user = User(email=email, password_hash=password_hash(body.password), role=body.role, consent_version=body.consent_version)
    db.add(user)
    try:
        db.flush()
        issue_token(db, user, "confirm_email", request.app.state.settings)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Регистрация недоступна для этого адреса")
    return {"message": "Письмо подтверждения поставлено в очередь отправки. Подтвердите email перед входом."}


@router.post("/confirm-email")
def confirm(body: TokenInput, db: Session = Depends(get_db)):
    token = db.scalar(select(AuthToken).where(AuthToken.id == digest(body.token), AuthToken.kind == "confirm_email").with_for_update())
    if not token or token.used_at or token.expires_at < time.time():
        raise HTTPException(400, "Ссылка недействительна или истекла")
    token.used_at = time.time()
    db.get(User, token.user_id).email_verified = True
    db.commit()
    return {"message": "Email подтверждён. Теперь можно войти."}


@router.post("/login")
def login(body: Login, request: Request, response: Response, db: Session = Depends(get_db)):
    rate_limit(db, "login_ip", request.client.host, 20, 900)
    email = str(body.email).lower()
    rate_limit(db, "login_account", email, 8, 900)
    user = db.scalar(select(User).where(User.email == email))
    encoded = user.password_hash if user else password_hash("dummy-password-to-equalize")
    if not password_valid(body.password, encoded) or not user or user.disabled:
        raise HTTPException(401, "Неверный email или пароль")
    if not user.email_verified:
        raise HTTPException(403, "Подтвердите email по ссылке в письме")
    return start_session(db, user, response, request.app.state.settings)


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    session, _ = current_session(request, db)
    if session:
        db.delete(session)
        db.commit()
    response.delete_cookie(request.app.state.settings.cookie_name, path="/")
    return {"message": "Вы вышли из аккаунта"}


@router.post("/forgot-password")
def forgot(body: Forgot, request: Request, db: Session = Depends(get_db)):
    rate_limit(db, "forgot", request.client.host, 5, 3600)
    user = db.scalar(select(User).where(User.email == str(body.email).lower()))
    if user and not user.disabled and not user.is_demo:
        issue_token(db, user, "reset_password", request.app.state.settings)
        db.commit()
    return {"message": "Если адрес зарегистрирован, письмо восстановления будет отправлено."}


@router.post("/resend-confirmation")
def resend(body: Forgot, request: Request, db: Session = Depends(get_db)):
    rate_limit(db, "resend_confirmation", request.client.host, 5, 3600)
    user = db.scalar(select(User).where(User.email == str(body.email).lower()))
    if user and not user.disabled and not user.is_demo and not user.email_verified:
        issue_token(db, user, "confirm_email", request.app.state.settings)
        db.commit()
    return {"message": "Если адресу требуется подтверждение, новое письмо будет отправлено."}


@router.post("/reset-password")
def reset(body: Reset, db: Session = Depends(get_db)):
    token = db.scalar(select(AuthToken).where(AuthToken.id == digest(body.token), AuthToken.kind == "reset_password").with_for_update())
    if not token or token.used_at or token.expires_at < time.time():
        raise HTTPException(400, "Ссылка недействительна или истекла")
    user = db.get(User, token.user_id)
    user.password_hash = password_hash(body.password)
    token.used_at = time.time()
    db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    db.commit()
    return {"message": "Пароль обновлён. Войдите заново."}
