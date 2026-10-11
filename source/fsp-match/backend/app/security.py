import hashlib
import hmac
import secrets
import time
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from .database import get_db
from .models import AuthSession, RateBucket, User


def digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str):
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return "scrypt$" + salt.hex() + "$" + key.hex()


def password_valid(password: str, encoded: str):
    try:
        kind, salt, expected = encoded.split("$")
        if kind != "scrypt":
            return False
        key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(key.hex(), expected)
    except (ValueError, TypeError):
        return False


def current_session(request: Request, db: Session):
    cookie = request.cookies.get(request.app.state.settings.cookie_name, "")
    session = db.get(AuthSession, digest(cookie)) if cookie else None
    if session and session.expires_at > time.time():
        user = db.get(User, session.user_id)
        if user and not user.disabled and user.email_verified:
            return session, user
    return None, None


def authenticated(request: Request, db: Session = Depends(get_db)):
    _, user = current_session(request, db)
    if not user:
        raise HTTPException(401, "Войдите в подтверждённый аккаунт")
    return user


def require_role(role):
    def dependency(user: User = Depends(authenticated)):
        if user.role != role:
            raise HTTPException(403, "Действие недоступно этой роли")
        return user
    return dependency


candidate = require_role("candidate")
employer = require_role("employer")


def csrf(request: Request, db: Session = Depends(get_db)):
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in request.app.state.settings.origins:
        raise HTTPException(403, "Недопустимый Origin")
    session, _ = current_session(request, db)
    expected = session.csrf_token if session else request.cookies.get(request.app.state.settings.preauth_cookie_name, "")
    provided = request.headers.get("x-csrf-token", "")
    if not expected or not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(403, "Недействительный CSRF token")


def rate_limit(db, scope, identifier, limit, seconds):
    now = time.time()
    key = scope + ":" + digest(identifier)
    engine = db.get_bind()
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    # Separate transaction: limiting survives failed auth without committing or
    # releasing any caller-owned business lock / pending business changes.
    with Session(bind=getattr(engine, "_fsp_limiter_engine", engine)) as limiter_db:
        limiter_db.execute(insert(RateBucket).values(id=key, count=0, reset_at=now + seconds).on_conflict_do_nothing(index_elements=["id"]))
        limiter_db.execute(update(RateBucket).where(RateBucket.id == key, RateBucket.reset_at <= now).values(count=0, reset_at=now + seconds))
        count = limiter_db.execute(update(RateBucket).where(RateBucket.id == key, RateBucket.count < limit).values(count=RateBucket.count + 1).returning(RateBucket.count)).scalar_one_or_none()
        reset_at = limiter_db.scalar(select(RateBucket.reset_at).where(RateBucket.id == key))
        limiter_db.commit()
    if count is None:
        raise HTTPException(429, "Слишком много запросов. Повторите позже.", headers={"Retry-After": str(max(1, int(reset_at - now)))})


def user_mutex(db, user_id):
    """Serializes user's state transitions through final business commit."""
    if db.get_bind().dialect.name == "sqlite":
        # SQLite local stand has no row locks. SELECTs use legacy autocommit;
        # reserve writer before reading the mutable state to prevent stale reads.
        connection = db.connection()
        raw = connection.connection.driver_connection
        if not raw.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    db.scalar(select(User).where(User.id == user_id).with_for_update())


def user_public(user):
    return {"id": user.id, "email": user.email, "role": user.role, "email_verified": user.email_verified, "is_demo": user.is_demo}
