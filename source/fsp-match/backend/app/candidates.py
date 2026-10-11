import json
import secrets
import time
import subprocess
import sys
import os
import threading
from pathlib import Path
from starlette.concurrency import run_in_threadpool
from io import BytesIO
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from .database import get_db
from .domain.assessment import build_form, public_form, grade_form
from .integrations.fsp import snapshot
from .integrations.parsing import parse
from .models import Attempt, Company, ContactGrant, History, Profile, User
from .pdf import render_profile
from .schemas import AttemptStart, AttemptSubmit, CandidateProfile, Survey
from .security import candidate, csrf, rate_limit, user_mutex
from .services import get_profile, profile_payload, record

router = APIRouter(prefix="/candidate", tags=["candidate"], dependencies=[Depends(csrf)])
_pdf_slots = threading.BoundedSemaphore(2)


@router.get("/profile")
def profile(user: User = Depends(candidate), db: Session = Depends(get_db)):
    row = get_profile(db, user)
    db.commit()
    return profile_payload(db, row, owner=True)


@router.put("/profile")
def update_profile(body: CandidateProfile, user: User = Depends(candidate), db: Session = Depends(get_db)):
    row = get_profile(db, user)
    data = body.model_dump()
    # The retired contact is no longer editable in the current UI. Omission
    # preserves its private historical value; an explicit value still follows
    # the validated legacy schema for existing clients and account export.
    if "telegram" not in body.contacts.model_fields_set:
        legacy = row.data.get("contacts", {}).get("telegram")
        if legacy is not None:
            data["contacts"]["telegram"] = legacy
    row.data = data
    row.visible = body.visible
    row.updated_at = time.time()
    # A demo account alone is not evidence of an FSP achievement.
    row.fsp = snapshot(row.data, bool(user.is_demo and (row.fsp or {}).get("status") == "synthetic_demo"))
    user.consent_version, user.consent_at = body.consent_version, time.time()
    record(db, user.id, "profile_updated", {"specialization": body.specialization, "declared_grade": body.declared_grade, "visible": body.visible})
    db.commit()
    return profile_payload(db, row, owner=True)


@router.post("/profile/import")
async def import_pdf(request: Request, file: UploadFile = File(...), user: User = Depends(candidate), db: Session = Depends(get_db)):
    rate_limit(db, "pdf_import", user.id, 10, 3600)
    content = await file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "PDF должен быть меньше 5 МБ")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(415, "Нужен PDF документ")
    if not _pdf_slots.acquire(blocking=False):
        raise HTTPException(429, "Обработка других PDF ещё идёт. Повторите через несколько секунд.", headers={"Retry-After": "3"})
    try:
        extracted = await run_in_threadpool(subprocess.run, [sys.executable, "-I", str(Path(__file__).with_name("pdf_extract.py"))], input=content, capture_output=True, timeout=8, env={"PATH": os.defpath, "LANG": "C.UTF-8"})
        if extracted.returncode != 0 or len(extracted.stdout) > 80000:
            raise ValueError("invalid_pdf")
        data = json.loads(extracted.stdout)
        text = data["text"]
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "PDF повреждён, защищён паролем или не поддерживается")
    finally:
        _pdf_slots.release()
    if len(text.strip()) < 10:
        raise HTTPException(422, "В PDF нет извлекаемого текста. Заполните профиль вручную.")
    result = await run_in_threadpool(parse, text, "resume", request.app.state.settings)
    result["provenance"] = "pdf_import"
    record(db, user.id, "pdf_import_preview", {"parser": result["parser"], "pages": data["pages"], "requires_confirmation": True})
    db.commit()
    return result


@router.get("/profile/pdf")
def profile_pdf(request: Request, user: User = Depends(candidate), db: Session = Depends(get_db)):
    row = get_profile(db, user)
    db.commit()
    try:
        content = render_profile(profile_payload(db, row, owner=True), request.app.state.settings)
    except RuntimeError:
        raise HTTPException(503, "Шрифт PDF не настроен")
    return Response(content, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="fsp-profile.pdf"', "Cache-Control": "no-store"})


@router.get("/survey")
def saved_survey(user: User = Depends(candidate), db: Session = Depends(get_db)):
    latest = db.scalar(select(History).where(History.user_id == user.id, History.kind == "survey").order_by(History.created_at.desc()).limit(1))
    if not latest:
        return {"survey": None}
    return {"survey": {**latest.payload, "history_id": latest.id, "created_at": latest.created_at}}


@router.post("/survey")
def survey(body: Survey, user: User = Depends(candidate), db: Session = Depends(get_db)):
    if len(json.dumps(body.answers)) > 12000:
        raise HTTPException(413, "Слишком большой ответ опроса")
    profile = get_profile(db, user)
    profile.data = {**profile.data, "specialization": body.specialization, "declared_grade": body.declared_grade}
    item = record(db, user.id, "survey", body.model_dump())
    db.commit()
    return {"specialization": body.specialization, "declared_grade": body.declared_grade, "history_id": item.id}


@router.get("/history")
def history(user: User = Depends(candidate), db: Session = Depends(get_db)):
    return {"items": [{"id": x.id, "kind": x.kind, "created_at": x.created_at, "payload": x.payload} for x in db.scalars(select(History).where(History.user_id == user.id).order_by(History.created_at.desc()).limit(100))]}


@router.get("/fsp")
def fsp(user: User = Depends(candidate), db: Session = Depends(get_db)):
    row = get_profile(db, user)
    return row.fsp or snapshot(row.data, bool(user.is_demo and (row.fsp or {}).get("status") == "synthetic_demo"))


@router.post("/fsp/sync")
def fsp_sync(user: User = Depends(candidate), db: Session = Depends(get_db)):
    row = get_profile(db, user)
    row.fsp = snapshot(row.data, bool(user.is_demo and (row.fsp or {}).get("status") == "synthetic_demo"))
    record(db, user.id, "fsp_sync", {"status": row.fsp["status"], "external_verified": False})
    db.commit()
    return row.fsp


def attempt_payload(row, form=False):
    data = {"id": row.id, "specialization": row.specialization, "grade": row.grade, "status": row.status, "started_at": row.started_at, "expires_at": row.expires_at, "submitted_at": row.submitted_at, "cooldown_until": row.cooldown_until, "result": row.result}
    if form:
        data["form"] = public_form(row.form)
    return data


@router.post("/attempts", status_code=201)
def start_attempt(body: AttemptStart, user: User = Depends(candidate), db: Session = Depends(get_db)):
    rate_limit(db, "attempt_starts", user.id, 12, 86400)
    user_mutex(db, user.id)
    now = time.time()
    rows = list(db.scalars(select(Attempt).where(Attempt.user_id == user.id, Attempt.specialization == body.specialization, Attempt.grade == body.grade).order_by(Attempt.started_at.desc())))
    for row in rows:
        if row.status == "in_progress" and row.expires_at > now:
            return attempt_payload(row, form=True)
        if row.status == "in_progress":
            row.status = "expired"
            row.cooldown_until = row.expires_at + 90 * 86400
        if row.cooldown_until and row.cooldown_until > now:
            raise HTTPException(409, {"message": "Повтор этой категории доступен после периода ожидания. Можно добровольно выбрать другой уровень.", "cooldown_until": row.cooldown_until})
    form = build_form(body.specialization, body.grade, secrets.token_hex(16))
    row = Attempt(user_id=user.id, specialization=body.specialization, grade=body.grade, form=form, expires_at=now + 45 * 60)
    db.add(row)
    db.flush()
    record(db, user.id, "attempt_started", {"attempt_id": row.id, "specialization": row.specialization, "grade": row.grade, "version": form["version"]})
    db.commit()
    return attempt_payload(row, form=True)


@router.get("/attempts")
def attempts(user: User = Depends(candidate), db: Session = Depends(get_db)):
    return {"items": [attempt_payload(x) for x in db.scalars(select(Attempt).where(Attempt.user_id == user.id).order_by(Attempt.started_at.desc()).limit(100))]}


@router.get("/attempts/{identifier}")
def attempt(identifier: str, user: User = Depends(candidate), db: Session = Depends(get_db)):
    row = db.scalar(select(Attempt).where(Attempt.id == identifier, Attempt.user_id == user.id))
    if not row:
        raise HTTPException(404, "Попытка не найдена")
    return attempt_payload(row, form=True)


@router.post("/attempts/{identifier}/submit")
def submit(identifier: str, body: AttemptSubmit, user: User = Depends(candidate), db: Session = Depends(get_db)):
    user_mutex(db, user.id)
    row = db.scalar(select(Attempt).where(Attempt.id == identifier, Attempt.user_id == user.id).with_for_update())
    if not row:
        raise HTTPException(404, "Попытка не найдена")
    profile = get_profile(db, user)
    if row.result:
        return {**attempt_payload(row), "verified_grade": profile.verification.get(row.specialization, {}).get("grade")}
    now = time.time()
    if row.status != "in_progress" or row.expires_at < now:
        row.status, row.cooldown_until = "expired", row.expires_at + 90 * 86400
        db.commit()
        raise HTTPException(409, "Время попытки истекло")
    if len(json.dumps(body.answers)) > 32000:
        raise HTTPException(413, "Слишком большой ответ")
    try:
        result = grade_form(row.form, body.answers)
    except ValueError as exc:
        raise HTTPException(422, str(exc)[:200])
    row.result, row.status, row.submitted_at, row.cooldown_until = result, "passed" if result["passed"] else "failed", now, now + 90 * 86400
    previous = profile.verification.get(row.specialization, {})
    if result["passed"]:
        grade = "middle" if previous.get("grade") == "middle" or row.grade == "middle" else "junior"
        skills = list(dict.fromkeys(previous.get("skills", []) + [k for k, v in result["competencies"].items() if v.get("verified")]))
        evidence = {"grade": row.grade, "skills": [k for k, v in result["competencies"].items() if v.get("verified")], "verified_at": now, "version": result["version"], "attempt_id": row.id, "score": result["score"], "synthetic": False}
        by_grade = {**previous.get("by_grade", {}), row.grade: evidence}
        if previous.get("grade") == "middle" and row.grade == "junior":
            # A Junior result must never masquerade as fresh Middle evidence.
            active_evidence = {**previous, "skills": skills, "by_grade": by_grade}
        else:
            active_evidence = {**evidence, "grade": grade, "skills": skills, "by_grade": by_grade}
        skill_provenance = dict(previous.get("skill_provenance", {}))
        for skill in evidence["skills"]:
            if skill_provenance.get(skill, {}).get("grade") != "middle" or row.grade == "middle":
                skill_provenance[skill] = {"grade": row.grade, "attempt_id": row.id, "version": result["version"]}
        active_evidence["skill_provenance"] = skill_provenance
        profile.verification = {**profile.verification, row.specialization: active_evidence}
    record(db, user.id, "attempt_completed", {"attempt_id": row.id, "specialization": row.specialization, "grade": row.grade, "passed": result["passed"], "score": result["score"], "version": result["version"]})
    db.commit()
    return {**attempt_payload(row), "verified_grade": profile.verification.get(row.specialization, {}).get("grade")}


@router.get("/contact-grants")
def grants(user: User = Depends(candidate), db: Session = Depends(get_db)):
    rows = db.execute(select(ContactGrant, Company).join(Company, Company.id == ContactGrant.company_id).where(ContactGrant.candidate_id == user.id))
    return {"items": [{"id": grant.id, "company_id": grant.company_id, "company_name": company.data.get("name", "Компания"), "source": grant.source, "created_at": grant.created_at, "revoked_at": grant.revoked_at} for grant, company in rows]}


@router.delete("/contact-grants/{identifier}")
def revoke(identifier: str, user: User = Depends(candidate), db: Session = Depends(get_db)):
    row = db.scalar(select(ContactGrant).where(ContactGrant.id == identifier, ContactGrant.candidate_id == user.id).with_for_update())
    if not row:
        raise HTTPException(404, "Разрешение не найдено")
    row.revoked_at = time.time()
    record(db, user.id, "contact_grant_revoked", {"company_id": row.company_id, "grant_id": row.id})
    db.commit()
    return {"message": "Доступ к контактам отозван"}
