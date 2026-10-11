"""Required M08 fields are bounded self-report, including import and PDF."""
from io import BytesIO
import math

import pytest
from pydantic import ValidationError
from pypdf import PdfReader

from app.config import Settings
from app.integrations.parsing import ParsedFields, parse
from app.schemas import CandidateProfile
from test_api import env, candidate_data, login


def test_required_profile_fields_roundtrip_public_privacy_and_pdf(env):
    app, _ = env
    candidate, cid = login(app, "candidate@demo.example.com")
    employer, _ = login(app, "employer@demo.example.com")
    original = candidate.get("/api/candidate/profile").json()
    body = {**candidate_data(), "roles": ["API-разработчик", "private-role@example.com"],
            "experience_years": 2.5, "soft_skills": ["Командная работа", "@privatecoach"]}
    result = candidate.put("/api/candidate/profile", json=body)
    assert result.status_code == 200, result.text
    owner = result.json()
    assert owner["roles"] == body["roles"]
    assert owner["soft_skills"] == body["soft_skills"]
    assert owner["experience_years"] == 2.5
    assert owner["verified_grade"] == original["verified_grade"]
    assert owner["verified_skills"] == original["verified_skills"]
    public = employer.get(f"/api/candidates/{cid}")
    assert public.json()["experience_years"] == 2.5
    assert "API-разработчик" in public.text and "Командная работа" in public.text
    assert "private-role@example.com" not in public.text and "@privatecoach" not in public.text
    assert "contacts" not in public.json()
    pdf = candidate.get("/api/candidate/profile/pdf")
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
    assert "Профессиональные роли" in text and "API-разработчик" in text
    assert "2.5 лет" in text and "Soft skills" in text and "Командная работа" in text
    preview = candidate.post("/api/candidate/profile/import", files={"file": ("own-profile.pdf", pdf.content, "application/pdf")})
    assert preview.status_code == 200, preview.text
    assert preview.json()["fields"]["experience_years"] == 2.5
    assert preview.json()["fields"]["roles"] == body["roles"]
    assert preview.json()["fields"]["soft_skills"] == body["soft_skills"]
    public_pdf = employer.get(f"/api/candidates/{cid}/pdf")
    public_text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(public_pdf.content)).pages)
    assert "private-role@example.com" not in public_text and "@privatecoach" not in public_text


@pytest.mark.parametrize("bad", [
    {"roles": ["x" * 81]}, {"roles": [" "]}, {"roles": ["Role"] * 21},
    {"soft_skills": ["x" * 81]}, {"soft_skills": [""]}, {"soft_skills": ["Skill"] * 21},
    {"experience_years": -0.1}, {"experience_years": 60.1},
    {"experience_years": math.inf}, {"experience_years": math.nan},
])
def test_profile_and_parser_reject_unbounded_self_report(bad):
    for schema in (CandidateProfile, ParsedFields):
        with pytest.raises(ValidationError):
            schema.model_validate(bad)


def test_resume_local_parser_only_extracts_explicit_labels():
    settings = Settings(llm_base_url="", llm_model="")
    result = parse("ФИО: Анна Тестовая\nРоли: API-разработчик, Backend developer\n"
                   "Стаж: 2,5 года\nSoft skills: Командная работа; коммуникация\nPython FastAPI", "resume", settings)
    assert result["fields"]["full_name"] == "Анна Тестовая"
    assert result["fields"]["roles"] == ["API-разработчик", "Backend developer"]
    assert result["fields"]["experience_years"] == 2.5
    assert result["fields"]["soft_skills"] == ["Командная работа", "коммуникация"]
    assert result["requires_confirmation"] and "verified_grade" not in result["fields"]
    missing = parse("Python разработчик любит командную работу и работал давно", "resume", settings)["fields"]
    assert all(key not in missing for key in ("full_name", "roles", "experience_years", "soft_skills"))
