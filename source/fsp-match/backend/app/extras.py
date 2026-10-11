import time
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .database import get_db
from .models import Company, Microtask, MicrotaskAttempt, Profile, User, Vacancy, VacancyResponse
from .schemas import MicrotaskInput, MicrotaskSubmit, ResponseInput, VacancyInput
from .security import authenticated, candidate, csrf, digest, employer, rate_limit, user_mutex
from .services import answer_normalized, company_payload, get_company, grant_contacts, has_grant, notify, owned, profile_payload, record

router = APIRouter(tags=["vacancies and microtasks"], dependencies=[Depends(csrf)])


def vacancy_payload(db, row):
    return {"id": row.id, **row.data, "company": company_payload(db.get(Company, row.company_id)), "published": row.published, "closed": row.closed, "currency": "RUB", "created_at": row.created_at}


@router.get("/vacancies")
def vacancies(user: User = Depends(authenticated), db: Session = Depends(get_db)):
    return {"items": [vacancy_payload(db, x) for x in db.scalars(select(Vacancy).where(Vacancy.published.is_(True), Vacancy.closed.is_(False)).order_by(Vacancy.created_at.desc()))]}


@router.get("/vacancies/{identifier}")
def vacancy(identifier: str, user: User = Depends(authenticated), db: Session = Depends(get_db)):
    row = db.get(Vacancy, identifier)
    if not row or not row.published or row.closed:
        raise HTTPException(404, "Вакансия не найдена")
    return vacancy_payload(db, row)


@router.get("/employer/vacancies")
def own_vacancies(user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    return {"items": [vacancy_payload(db, x) for x in db.scalars(select(Vacancy).where(Vacancy.company_id == company.id).order_by(Vacancy.created_at.desc()))]}


@router.post("/employer/vacancies", status_code=201)
def create_vacancy(body: VacancyInput, user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    rate_limit(db, "create_vacancy", user.id, 30, 3600)
    row = Vacancy(company_id=company.id, data=body.model_dump(exclude={"published"}), published=body.published)
    db.add(row)
    db.commit()
    return vacancy_payload(db, row)


@router.put("/employer/vacancies/{identifier}")
def update_vacancy(identifier: str, body: VacancyInput, user: User = Depends(employer), db: Session = Depends(get_db)):
    row = owned(db, Vacancy, identifier, "company_id", get_company(db, user).id, lock=True)
    if row.closed:
        raise HTTPException(409, "Вакансия закрыта")
    row.data, row.published = body.model_dump(exclude={"published"}), body.published
    db.commit()
    return vacancy_payload(db, row)


@router.delete("/employer/vacancies/{identifier}")
def close_vacancy(identifier: str, user: User = Depends(employer), db: Session = Depends(get_db)):
    row = owned(db, Vacancy, identifier, "company_id", get_company(db, user).id, lock=True)
    row.closed, row.published = True, False
    record(db, user.id, "vacancy_closed", {"vacancy_id": row.id})
    db.commit()
    return {"message": "Вакансия закрыта; история сохранена"}


def response_payload(db, row, for_employer=False):
    vacancy = db.get(Vacancy, row.vacancy_id)
    result = {"id": row.id, "vacancy_id": row.vacancy_id, "candidate_id": row.candidate_id, "message": row.message, "created_at": row.created_at, "vacancy": vacancy_payload(db, vacancy)}
    if for_employer:
        profile = db.get(Profile, row.candidate_id)
        candidate_user = db.get(User, row.candidate_id)
        allowed = profile and candidate_user and not candidate_user.disabled and (profile.visible or has_grant(db, vacancy.company_id, row.candidate_id))
        result["candidate"] = profile_payload(db, profile, company_id=vacancy.company_id) if allowed else None
    return result


@router.post("/candidate/vacancies/{identifier}/respond", status_code=201)
def respond(identifier: str, body: ResponseInput, request: Request, user: User = Depends(candidate), db: Session = Depends(get_db)):
    rate_limit(db, "vacancy_response", user.id, 30, 86400)
    user_mutex(db, user.id)
    vacancy = db.get(Vacancy, identifier)
    if not vacancy or vacancy.closed or not vacancy.published:
        raise HTTPException(404, "Вакансия не найдена")
    row = db.scalar(select(VacancyResponse).where(VacancyResponse.vacancy_id == identifier, VacancyResponse.candidate_id == user.id))
    if row:
        return response_payload(db, row)
    row = VacancyResponse(vacancy_id=identifier, candidate_id=user.id, message=body.message)
    db.add(row)
    try:
        db.flush()
        grant_contacts(db, vacancy.company_id, user.id, "vacancy_response", row.id)
        company = db.get(Company, vacancy.company_id)
        notify(db, company.user_id, "vacancy_response", {"response_id": row.id, "vacancy_id": vacancy.id, "title": vacancy.data["title"], "app_url": request.app.state.settings.app_url + "/employer/vacancies"})
        record(db, user.id, "vacancy_response", {"response_id": row.id, "vacancy_id": identifier, "company_id": vacancy.company_id, "contacts_granted": True})
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.scalar(select(VacancyResponse).where(VacancyResponse.vacancy_id == identifier, VacancyResponse.candidate_id == user.id))
        if not row:
            raise HTTPException(409, "Отклик конфликтует с другой операцией")
    return response_payload(db, row)


@router.get("/candidate/responses")
def responses(user: User = Depends(candidate), db: Session = Depends(get_db)):
    return {"items": [response_payload(db, x) for x in db.scalars(select(VacancyResponse).where(VacancyResponse.candidate_id == user.id).order_by(VacancyResponse.created_at.desc()))]}


@router.get("/employer/responses")
def employer_responses(user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    rows = db.scalars(select(VacancyResponse).join(Vacancy, Vacancy.id == VacancyResponse.vacancy_id).where(Vacancy.company_id == company.id).order_by(VacancyResponse.created_at.desc()))
    return {"items": [response_payload(db, x, True) for x in rows]}


def task_payload(db, task):
    company = db.get(Company, task.company_id) if task.company_id else None
    return {"id": task.id, **task.data, "company": company_payload(company) if company else None, "created_at": task.created_at, "active": task.active, "changes_verified_grade": False, "format": "short_answer_exact", "rubric": "Совпадение нормализованного короткого ответа; дополнительный сигнал, не основной грейд."}


@router.get("/microtasks")
def microtasks(user: User = Depends(authenticated), db: Session = Depends(get_db)):
    return {"items": [task_payload(db, x) for x in db.scalars(select(Microtask).where(Microtask.active.is_(True)).order_by(Microtask.created_at.desc())) if not x.data.get("due_at") or x.data["due_at"] > time.time()]}


@router.post("/employer/microtasks", status_code=201)
def create_task(body: MicrotaskInput, request: Request, user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    rate_limit(db, "create_microtask", user.id, 20, 86400)
    if body.due_at and body.due_at <= time.time():
        raise HTTPException(422, "Срок задания должен быть в будущем")
    task = Microtask(company_id=company.id, data=body.model_dump(exclude={"expected_answer"}), expected_hash=digest(answer_normalized(body.expected_answer)))
    db.add(task)
    db.flush()
    candidates = list(db.scalars(select(Profile).where(Profile.visible.is_(True))))
    for profile in candidates:
        if profile.data.get("specialization") == body.specialization:
            notify(db, profile.user_id, "microtask", {"task_id": task.id, "title": task.data["title"], "company_name": company.data["name"], "app_url": request.app.state.settings.app_url + "/candidate/microtasks"})
    db.commit()
    return task_payload(db, task)


@router.get("/employer/microtasks")
def own_tasks(user: User = Depends(employer), db: Session = Depends(get_db)):
    company = get_company(db, user)
    result = []
    for task in db.scalars(select(Microtask).where(Microtask.company_id == company.id)):
        attempts = []
        for x in db.scalars(select(MicrotaskAttempt).where(MicrotaskAttempt.task_id == task.id)):
            profile = db.get(Profile, x.candidate_id)
            attempts.append({"id": x.id, "candidate_id": x.candidate_id, "score": x.score, "created_at": x.created_at, "candidate": profile_payload(db, profile, company_id=company.id) if profile and profile.visible else None})
        result.append({**task_payload(db, task), "attempts": attempts})
    return {"items": result}


@router.post("/candidate/microtasks/{identifier}/submit", status_code=201)
def submit_task(identifier: str, body: MicrotaskSubmit, user: User = Depends(candidate), db: Session = Depends(get_db)):
    task = db.get(Microtask, identifier)
    if not task or not task.active or (task.data.get("due_at") and task.data["due_at"] < time.time()):
        raise HTTPException(404, "Задание недоступно")
    rate_limit(db, "microtask:" + identifier, user.id, 5, 3600)
    score = 100 if digest(answer_normalized(body.answer)) == task.expected_hash else 0
    row = MicrotaskAttempt(task_id=identifier, candidate_id=user.id, score=score, answer=body.answer)
    db.add(row)
    db.flush()
    record(db, user.id, "microtask_completed", {"attempt_id": row.id, "task_id": identifier, "score": score, "changes_verified_grade": False})
    db.commit()
    return {"id": row.id, "score": score, "feedback": "Ответ совпал с эталоном." if score else "Ответ не совпал с эталоном. Проверьте условие и попробуйте позже.", "changes_verified_grade": False}


@router.get("/candidate/microtask-history")
def task_history(user: User = Depends(candidate), db: Session = Depends(get_db)):
    return {"items": [{"id": x.id, "task_id": x.task_id, "score": x.score, "created_at": x.created_at, "changes_verified_grade": False} for x in db.scalars(select(MicrotaskAttempt).where(MicrotaskAttempt.candidate_id == user.id).order_by(MicrotaskAttempt.created_at.desc()).limit(100))]}
