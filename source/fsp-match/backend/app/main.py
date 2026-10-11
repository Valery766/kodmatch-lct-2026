from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from .config import Settings, get_settings
from .database import build_database, dispose_database
from .models import Base
from .seed import seed_demo
from . import auth, candidates, employers, extras
from .integrations.routes import router as integration_router, transport_router
from .domain.assessment import build_form, public_form
from .migrate import migrate


SPECIALIZATIONS = [{"id": "backend_python", "label": "Backend / Python", "grades": ["junior", "middle"]}, {"id": "frontend_ts", "label": "Frontend / TypeScript", "grades": ["junior", "middle"]}, {"id": "data_sql", "label": "Data / SQL", "grades": ["junior", "middle"]}]


def create_app(settings: Settings | None = None):
    settings = settings or get_settings()
    engine, factory = build_database(settings.database_url)

    @asynccontextmanager
    async def lifespan(application):
        migrate(engine)
        seed_demo(factory, settings)
        yield
        dispose_database(engine)

    application = FastAPI(title="КОД.МЭТЧ", version=settings.release_version, lifespan=lifespan, docs_url=None if settings.production else "/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    application.state.settings, application.state.engine, application.state.session_factory = settings, engine, factory
    application.add_middleware(CORSMiddleware, allow_origins=settings.origins, allow_credentials=True, allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "X-CSRF-Token"])

    @application.middleware("http")
    async def safe_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if settings.production or not request.url.path.startswith("/api/docs"):
            response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        if settings.production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    for router in [auth.router, candidates.router, employers.router, extras.router, integration_router, transport_router]:
        application.include_router(router, prefix="/api")

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error):
        return JSONResponse(status_code=422, content={"detail": [{"loc": list(x["loc"]), "msg": x["msg"], "type": x["type"]} for x in error.errors()]})

    @application.get("/api/health", tags=["operations"])
    def health():
        return {"status": "ok", "service": "fsp-match"}

    @application.get("/api/ready", tags=["operations"])
    def ready():
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return {"status": "ready", "database": "ok"}
        except Exception:
            raise HTTPException(503, "База данных недоступна")

    @application.get("/api/version", tags=["operations"])
    def version():
        return {"version": settings.release_version, "assessment_version": build_form("backend_python", "junior", "catalog")["version"], "matching_version": "matching-v1"}

    @application.get("/api/catalog", tags=["catalog"])
    def catalog():
        return {"specializations": SPECIALIZATIONS, "grades": [{"id": "junior", "label": "Junior"}, {"id": "middle", "label": "Middle"}], "work_formats": ["remote", "office", "hybrid", "any"], "consent_version": "2026-10-06", "registration_enabled": settings.registration_enabled,
                "legal_operator": {"configured": bool(settings.legal_operator_name and settings.legal_operator_contact), "name": settings.legal_operator_name, "contact": settings.legal_operator_contact}, "demo_mode": settings.seed_demo}

    @application.get("/api/assessments/catalog", tags=["catalog"])
    def assessment_catalog():
        result = []
        for spec in SPECIALIZATIONS:
            for grade in ["junior", "middle"]:
                form = public_form(build_form(spec["id"], grade, "catalog"))
                result.append({"specialization": spec["id"], "grade": grade, "version": form["version"], "blueprint": form["blueprint"], "rubric": form["rubric"], "question_count": len(form["questions"]), "duration_minutes": 45, "duration_label": "Лимит попытки, а не измеренное время прохождения", "cooldown_days": 90})
        return {"items": result, "scope": "Фиксированные практические задания и рассуждение об ограниченном коде; не полная оценка профессии и не гарантия отсутствия помощи."}
    return application


app = create_app()
