import time
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .database import get_db
from .domain.matching import rank_candidates
from .integrations.parsing import parse
from .integrations import grounded_explanations
from .models import Company, History, Invitation, Need, Profile, Shortlist, User
from .pdf import render_profile
from .schemas import CompanyInput, Decision, InvitationInput, NeedInput, ParseText, ShortlistInput
from .security import candidate, csrf, employer, rate_limit, user_mutex
from .services import company_payload, get_company, grant_contacts, has_grant, notify, owned, profile_payload, profile_payloads, record

router = APIRouter(tags=["employer and invitations"], dependencies=[Depends(csrf)])


def need_payload(row):
    return {"id": row.id, "company_id": row.company_id, **row.data, "created_at": row.created_at, "updated_at": row.updated_at}


@router.get("/employer/company")
def company(user: User = Depends(employer), db: Session = Depends(get_db)):
    row = db.scalar(select(Company).where(Company.user_id == user.id))
    return company_payload(row) if row else None


@router.put("/employer/company")
def update_company(body: CompanyInput, user: User = Depends(employer), db: Session = Depends(get_db)):
    row = db.scalar(select(Company).where(Company.user_id == user.id))
    if not row:
        row = Company(user_id=user.id, data=body.model_dump(), verification_status="synthetic_demo" if user.is_demo else "unverified")
        db.add(row)
    else:
        row.data = body.model_dump()
    db.flush()
    record(db, user.id, "company_updated", {"company_id": row.id, "verification_status": row.verification_status})
    db.commit()
    return company_payload(row)


@router.post("/employer/needs/parse")
def parse_need(body: ParseText, request: Request, user: User = Depends(employer), db: Session = Depends(get_db)):
    rate_limit(db, "llm_employer_action", user.id, 20, 3600)
    return parse(body.text, "need", request.app.state.settings)


@router.get("/employer/needs")
def needs(user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    return {"items": [need_payload(x) for x in db.scalars(select(Need).where(Need.company_id == company.id).order_by(Need.created_at.desc()))]}


@router.post("/employer/needs", status_code=201)
def create_need(body: NeedInput, user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    rate_limit(db, "create_need", user.id, 30, 3600)
    row = Need(company_id=company.id, data=body.model_dump())
    db.add(row)
    db.flush()
    record(db, user.id, "need_created", {"need_id": row.id})
    db.commit()
    return need_payload(row)


@router.get("/employer/needs/{identifier}")
def need(identifier: str, user: User = Depends(employer), db: Session = Depends(get_db)):
    return need_payload(owned(db, Need, identifier, "company_id", get_company(db, user).id))


@router.put("/employer/needs/{identifier}")
def update_need(identifier: str, body: NeedInput, user: User = Depends(employer), db: Session = Depends(get_db)):
    row = owned(db, Need, identifier, "company_id", get_company(db, user).id, lock=True)
    row.data, row.updated_at = body.model_dump(), time.time()
    record(db, user.id, "need_updated", {"need_id": row.id})
    db.commit()
    return need_payload(row)


@router.get("/employer/needs/{identifier}/search")
def search(identifier: str, user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    need = owned(db, Need, identifier, "company_id", company.id)
    rows = list(db.scalars(select(Profile).join(User, User.id == Profile.user_id).where(Profile.visible.is_(True), User.disabled.is_(False), User.email_verified.is_(True))))
    by_id = {x.user_id: x for x in rows}
    public_profiles = profile_payloads(db, rows)
    domain_profiles = []
    for row in rows:
        public = public_profiles[row.user_id]
        verification = row.verification.get(row.data.get("specialization"), {})
        public.update(test_score=verification.get("score"), skill_evidence={x: "verified" for x in public["verified_skills"]})
        domain_profiles.append(public)
    ranked = rank_candidates(need.data, domain_profiles)
    if need.data.get("require_fsp"):
        ranked = [x for x in ranked if x.get("fsp_achievements")]
    granted_profiles = profile_payloads(db, [by_id[x["id"]] for x in ranked], company_id=company.id)
    result = [{"id": x["id"], "profile": granted_profiles[x["id"]], "score": x["score"], "explanation": x["explanation"], "matching": x["matching"]} for x in ranked]
    version = ranked[0]["matching"]["version"] if ranked else "matching-v1"
    record(db, user.id, "search", {"need_id": need.id, "candidate_ids": [x["id"] for x in result], "scores": [x["score"] for x in result], "formula_version": version})
    db.commit()
    return {"items": result, "formula_version": version, "need_id": need.id}


@router.get("/employer/search-history")
def search_history(user: User = Depends(employer), db: Session = Depends(get_db)):
    return {"items": [{"id": x.id, "created_at": x.created_at, **x.payload} for x in db.scalars(select(History).where(History.user_id == user.id, History.kind == "search").order_by(History.created_at.desc()).limit(100))]}


def explanation_rank(db, company_id, identifier, candidate_id):
    need = owned(db, Need, identifier, "company_id", company_id)
    row = db.scalar(select(Profile).join(User, User.id == Profile.user_id).where(
        Profile.user_id == candidate_id, Profile.visible.is_(True),
        User.disabled.is_(False), User.email_verified.is_(True), User.role == "candidate"))
    if row is None:
        raise HTTPException(404, "Профиль не найден")
    public = profile_payload(db, row)
    verification = row.verification.get(row.data.get("specialization"), {})
    public.update(test_score=verification.get("score"), skill_evidence={skill: "verified" for skill in public["verified_skills"]})
    ranked = rank_candidates(need.data, [public])
    if not ranked or (need.data.get("require_fsp") and not ranked[0].get("fsp_achievements")):
        raise HTTPException(404, "Кандидат сейчас не входит в подборку этой потребности")
    return need.data, ranked[0]


@router.post("/employer/needs/{identifier}/candidates/{candidate_id}/explanation")
def candidate_explanation(identifier: str, candidate_id: str, body: grounded_explanations.ExplanationRequest,
                          request: Request, user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    company_id, user_id = company.id, user.id
    need_data, ranked = explanation_rank(db, company_id, identifier, candidate_id)
    settings = request.app.state.settings
    key = grounded_explanations.cache_key((company_id, identifier, candidate_id), need_data, ranked, settings)
    result = grounded_explanations.cached_result(key)
    if result is not None:
        return result
    # This is explicit, on demand, and shares the employer hourly budget with
    # need parsing. GET search never calls a model for every candidate.
    rate_limit(db, "llm_employer_action", user_id, 20, 3600)
    expected = grounded_explanations.fingerprint(need_data, ranked)
    db.rollback()  # Release the read transaction/connection before upstream IO.
    result = grounded_explanations.explain(need_data, ranked, settings)
    # Recheck after IO: a hide/disable/edit/revocation must not reuse stale
    # authorization or evidence, even when model generation succeeded.
    fresh_user, fresh_company = db.get(User, user_id), db.get(Company, company_id)
    if not fresh_user or fresh_user.disabled or not fresh_user.email_verified:
        raise HTTPException(401, "Войдите в подтверждённый аккаунт")
    if fresh_user.role != "employer":
        raise HTTPException(403, "Действие недоступно этой роли")
    if not fresh_company or fresh_company.user_id != user_id:
        raise HTTPException(404, "Компания не найдена")
    fresh_need, fresh_ranked = explanation_rank(db, company_id, identifier, candidate_id)
    if grounded_explanations.fingerprint(fresh_need, fresh_ranked) != expected:
        raise HTTPException(409, "Данные подбора изменились. Повторите запрос.")
    grounded_explanations.save_result(key, result)
    return result


@router.get("/employer/candidates")
def bank(specialization: str | None = None, grade: str | None = None, skill: str | None = Query(None, max_length=80), fsp: bool | None = None, page: int = Query(1, ge=1, le=2147483647), page_size: int = Query(12, ge=1, le=100), user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    if specialization and specialization not in {"backend_python", "frontend_ts", "data_sql"} or grade and grade not in {"junior", "middle"}:
        raise HTTPException(422, "Неизвестная специализация или категория")
    query = select(Profile).join(User, User.id == Profile.user_id).where(Profile.visible.is_(True), User.disabled.is_(False), User.email_verified.is_(True))
    specs = [specialization] if specialization else ["backend_python", "frontend_ts", "data_sql"]
    if specialization:
        query = query.where(Profile.data["specialization"].as_string() == specialization)
    if grade:
        query = query.where(or_(*[and_(Profile.data["specialization"].as_string() == spec, Profile.verification[spec]["grade"].as_string() == grade) for spec in specs]))
    if skill:
        conditions = []
        for spec in specs:
            if db.get_bind().dialect.name == "postgresql":
                values = func.json_array_elements_text(Profile.verification[spec]["skills"]).table_valued("value").render_derived(name="verified_skill")
            else:
                values = func.json_each(Profile.verification, "$." + spec + ".skills").table_valued("value")
            found = exists(select(1).select_from(values).where(func.lower(cast(values.c.value, String)) == skill.casefold()).correlate(Profile))
            conditions.append(and_(Profile.data["specialization"].as_string() == spec, found))
        query = query.where(or_(*conditions))
    if fsp is not None:
        length = func.coalesce(func.json_array_length(Profile.fsp["achievements"]), 0)
        query = query.where(length > 0 if fsp else length == 0)
    # Filtering/counting cover the complete visible bank before pagination.
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(db.scalars(query.order_by(Profile.user_id).offset((page - 1) * page_size).limit(page_size)))
    return {"items": list(profile_payloads(db, rows, company_id=company.id).values()), "page": page, "page_size": page_size, "total": total, "pages": (total + page_size - 1) // page_size,
            "filter_evidence": "Грейд и навыки фильтруются по подтверждённым результатам; ФСП demo помечен синтетическим."}


def bank_profile(db, identifier, company_id):
    row = db.get(Profile, identifier)
    user = db.get(User, identifier)
    if not row or not user or user.disabled or (not row.visible and not has_grant(db, company_id, identifier)):
        raise HTTPException(404, "Профиль не найден")
    return row


@router.get("/candidates/{identifier}")
def candidate_view(identifier: str, user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    return profile_payload(db, bank_profile(db, identifier, company.id), company_id=company.id)


@router.get("/candidates/{identifier}/pdf")
def candidate_pdf(identifier: str, request: Request, user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    row = bank_profile(db, identifier, company.id)
    try:
        data = render_profile(profile_payload(db, row, company_id=company.id), request.app.state.settings)
    except RuntimeError:
        raise HTTPException(503, "Шрифт PDF не настроен")
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="fsp-candidate.pdf"', "Cache-Control": "no-store"})


@router.get("/employer/shortlist")
def shortlist(user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    result = []
    for row in db.scalars(select(Shortlist).where(Shortlist.company_id == company.id)):
        profile = db.get(Profile, row.candidate_id)
        candidate_user = db.get(User, row.candidate_id)
        allowed = profile and candidate_user and not candidate_user.disabled and (profile.visible or has_grant(db, company.id, row.candidate_id))
        result.append({"id": row.id, "need_id": row.need_id, "candidate_id": row.candidate_id, "profile": profile_payload(db, profile, company_id=company.id) if allowed else None, "created_at": row.created_at})
    return {"items": result}


@router.post("/employer/shortlist", status_code=201)
def add_shortlist(body: ShortlistInput, user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    owned(db, Need, body.need_id, "company_id", company.id)
    bank_profile(db, body.candidate_id, company.id)
    row = db.scalar(select(Shortlist).where(Shortlist.need_id == body.need_id, Shortlist.candidate_id == body.candidate_id))
    if not row:
        row = Shortlist(company_id=company.id, need_id=body.need_id, candidate_id=body.candidate_id)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.scalar(select(Shortlist).where(Shortlist.need_id == body.need_id, Shortlist.candidate_id == body.candidate_id))
    return {"id": row.id, "need_id": row.need_id, "candidate_id": row.candidate_id}


@router.delete("/employer/shortlist/{identifier}")
def remove_shortlist(identifier: str, user: User = Depends(employer), db: Session = Depends(get_db)):
    row = owned(db, Shortlist, identifier, "company_id", get_company(db, user).id)
    db.delete(row)
    db.commit()
    return {"message": "Удалено из списка"}


def invitation_payload(db, row, for_employer=False):
    company = db.get(Company, row.company_id)
    need = db.get(Need, row.need_id)
    profile = db.get(Profile, row.candidate_id)
    data = {"id": row.id, "need_id": row.need_id, "candidate_id": row.candidate_id, "company": row.data.get("_company_snapshot") or company_payload(company), "need": row.data.get("_need_snapshot") or need_payload(need), **{k: v for k, v in row.data.items() if not k.startswith("_")}, "currency": "RUB", "status": row.status, "created_at": row.created_at, "viewed_at": row.viewed_at, "responded_at": row.responded_at}
    if for_employer:
        candidate_user = db.get(User, row.candidate_id)
        allowed = profile and candidate_user and not candidate_user.disabled and (profile.visible or has_grant(db, company.id, row.candidate_id))
        data["candidate"] = profile_payload(db, profile, company_id=company.id) if allowed else None
    return data


@router.post("/employer/invitations", status_code=201)
def invite(body: InvitationInput, request: Request, user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    need = owned(db, Need, body.need_id, "company_id", company.id)
    bank_profile(db, body.candidate_id, company.id)
    row = db.scalar(select(Invitation).where(Invitation.need_id == body.need_id, Invitation.candidate_id == body.candidate_id))
    if row:
        return invitation_payload(db, row, True)
    rate_limit(db, "invitations", user.id, 30, 86400)
    row = Invitation(company_id=company.id, need_id=body.need_id, candidate_id=body.candidate_id, data={**{k: v for k, v in body.model_dump().items() if k not in {"need_id", "candidate_id"}}, "_company_snapshot": company_payload(company), "_need_snapshot": need_payload(need)})
    db.add(row)
    try:
        db.flush()
        notify(db, body.candidate_id, "invitation", {"invitation_id": row.id, "company_name": company.data["name"], "title": need.data["title"], "salary_min": body.salary_min, "salary_max": body.salary_max, "status": "sent", "app_url": request.app.state.settings.app_url + "/candidate/invitations"})
        record(db, user.id, "invitation_sent", {"invitation_id": row.id})
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.scalar(select(Invitation).where(Invitation.need_id == body.need_id, Invitation.candidate_id == body.candidate_id))
        if not row:
            raise HTTPException(409, "Приглашение конфликтует с другой операцией")
    return invitation_payload(db, row, True)


@router.get("/employer/invitations")
def employer_invitations(user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    return {"items": [invitation_payload(db, x, True) for x in db.scalars(select(Invitation).where(Invitation.company_id == company.id).order_by(Invitation.created_at.desc()))]}


@router.get("/candidate/invitations")
def candidate_invitations(user: User = Depends(candidate), db: Session = Depends(get_db)):
    return {"items": [invitation_payload(db, x) for x in db.scalars(select(Invitation).where(Invitation.candidate_id == user.id).order_by(Invitation.created_at.desc()))]}


@router.post("/candidate/invitations/{identifier}/view")
def viewed(identifier: str, user: User = Depends(candidate), db: Session = Depends(get_db)):
    row = owned(db, Invitation, identifier, "candidate_id", user.id, lock=True)
    if row.status == "sent":
        row.status, row.viewed_at = "viewed", time.time()
        db.commit()
    return invitation_payload(db, row)


@router.post("/candidate/invitations/{identifier}/respond")
def respond(identifier: str, body: Decision, request: Request, user: User = Depends(candidate), db: Session = Depends(get_db)):
    user_mutex(db, user.id)
    row = owned(db, Invitation, identifier, "candidate_id", user.id, lock=True)
    if row.status in {"accepted", "rejected"}:
        if row.status != body.decision:
            raise HTTPException(409, "Ответ уже сохранён")
        return invitation_payload(db, row)
    row.status, row.responded_at = body.decision, time.time()
    if body.decision == "accepted":
        grant_contacts(db, row.company_id, user.id, "invitation", row.id)
    company = db.get(Company, row.company_id)
    notify(db, company.user_id, "invitation_response", {"invitation_id": row.id, "status": row.status, "app_url": request.app.state.settings.app_url + "/employer/invitations"})
    record(db, user.id, "invitation_response", {"invitation_id": row.id, "status": row.status, "company_id": row.company_id})
    db.commit()
    return invitation_payload(db, row)
