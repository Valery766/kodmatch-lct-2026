import time
from sqlalchemy import select
from .integrations.fsp import snapshot
from .models import Company, Microtask, Need, Profile, User, Vacancy
from .schemas import CandidateProfile
from .security import digest, password_hash
from .services import answer_normalized


DEMO_DOMAIN = "demo.example.com"


def seed_demo(factory, settings):
    if not settings.seed_demo:
        return
    with factory() as db:
        # Never overwrite previous passwords, accounts, assessment history or edits.
        if db.scalar(select(User.id).where(User.email == "candidate@" + DEMO_DOMAIN)):
            return
        password = password_hash(settings.demo_password)
        skills = {"backend_python": ["python", "api", "testing"], "frontend_ts": ["typescript", "javascript", "async", "testing"], "data_sql": ["sql", "data_modeling", "testing"]}
        headlines = {"backend_python": "Python · Backend", "frontend_ts": "TypeScript · Frontend", "data_sql": "SQL · Data"}
        bios = {"backend_python": "Разработка API на Python, тестирование и отладка. Интересны серверные сервисы и работа с граничными случаями.", "frontend_ts": "Пользовательские интерфейсы на TypeScript и JavaScript. Интересны асинхронные сценарии, удобство интерфейса и тестирование.", "data_sql": "SQL-запросы и моделирование данных. Интересны качество данных, обработка NULL и проверка корректности результатов."}
        for index, (spec, grade) in enumerate([(s, g) for s in skills for g in ["junior", "middle"]]):
            email = "candidate@" + DEMO_DOMAIN if index == 0 else spec + "-" + grade + "@" + DEMO_DOMAIN
            user = User(email=email, password_hash=password, role="candidate", email_verified=True, is_demo=True, consent_version="2026-10-06")
            db.add(user)
            db.flush()
            data = CandidateProfile(full_name=["Алексей Примеров", "Мария Демонстрационная", "Ирина Тестовая", "Денис Учебный", "Анна Примерная", "Олег Синтетический"][index], headline=f"{headlines[spec]} · {grade.title()}", bio=bios[spec], specialization=spec, declared_grade=grade, skills=skills[spec], visible=True, salary_min=60000 if grade == "junior" else 140000, salary_max=120000 if grade == "junior" else 240000, work_format="remote", location="Санкт-Петербург").model_dump()
            verification = {spec: {"grade": grade, "skills": skills[spec], "score": 84 + index, "version": "synthetic_seed_v1", "verified_at": time.time(), "synthetic": True}}
            db.add(Profile(user_id=user.id, data=data, visible=True, verification=verification, fsp=snapshot(data, True) if index % 2 == 0 else {"status": "not_linked", "achievements": [], "verified": False}))
        for index, label in enumerate(["employer", "second"]):
            user = User(email=label + "@" + DEMO_DOMAIN, password_hash=password, role="employer", email_verified=True, is_demo=True, consent_version="2026-10-06")
            db.add(user)
            db.flush()
            company = Company(user_id=user.id, data={"name": "Команда разработки " + str(index + 1), "description": "Разработка цифровых сервисов: API, пользовательские интерфейсы и работа с данными.", "website": "", "inn": "", "contact_method": "Ответ в кабинете КОД.МЭТЧ"}, verification_status="synthetic_demo")
            db.add(company)
            db.flush()
            data = {"title": "Python-разработчик для внутреннего сервиса", "description": "Разработка и тестирование API на Python, работа с граничными случаями и отладкой.", "specialization": "backend_python", "grade": "junior", "required_skills": ["python"], "preferred_skills": ["testing"], "salary_min": 70000, "salary_max": 160000, "work_format": "remote", "location": "", "require_fsp": False}
            db.add(Need(company_id=company.id, data=data))
            db.add(Vacancy(company_id=company.id, data=data, published=True))
        db.add(Microtask(company_id=None, data={"title": "Короткая практика: SQL NULL", "description": "Какой SQL-предикат проверяет отсутствие значения в поле email? Введите короткое выражение для этого поля.", "specialization": "data_sql", "grade": "junior", "due_at": None}, expected_hash=digest(answer_normalized("email IS NULL"))))
        db.commit()
