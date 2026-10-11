from sqlalchemy import create_engine, event
from fastapi import Request
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from .models import Base


def build_database(url):
    kwargs = {"pool_pre_ping": True}
    if url.startswith("postgresql"):
        # Forty browser sessions can queue behind the former 15-connection
        # default during simultaneous bank reads. Keep the own PG pool bounded;
        # SQLite defaults/StaticPool and the separate limiter pool are unchanged.
        kwargs.update(pool_size=10, max_overflow=14, pool_timeout=5)
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def foreign_keys(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
    # Rate limiting uses a separate bounded pool. Reusing the business pool for
    # a second session can deadlock when all request connections are occupied.
    if ":memory:" in url:
        engine._fsp_limiter_engine = engine
    else:
        limiter_kwargs = {"pool_pre_ping": True, "pool_size": 3, "max_overflow": 5, "pool_timeout": 5}
        if url.startswith("sqlite"):
            limiter_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        engine._fsp_limiter_engine = create_engine(url, **limiter_kwargs)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory


def dispose_database(engine):
    limiter = getattr(engine, "_fsp_limiter_engine", engine)
    if limiter is not engine:
        limiter.dispose()
    engine.dispose()


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        try:
            yield db
        except Exception:
            db.rollback()
            raise
