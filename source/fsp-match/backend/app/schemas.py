from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, model_validator

Specialization = Literal["backend_python", "frontend_ts", "data_sql"]
Grade = Literal["junior", "middle"]
Role = Literal["candidate", "employer"]
WorkFormat = Literal["remote", "office", "hybrid", "any"]
CONSENT_VERSION = "2026-10-06"
ProfileListItem = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Register(StrictModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    role: Role
    consent_version: Literal["2026-10-06"]


class Login(StrictModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenInput(StrictModel):
    token: str = Field(min_length=20, max_length=120)


class Forgot(StrictModel):
    email: EmailStr


class Reset(TokenInput):
    password: str = Field(min_length=12, max_length=128)


class Contacts(StrictModel):
    phone: str = Field(default="", max_length=60)
    telegram: str = Field(default="", max_length=80)
    website: str = Field(default="", max_length=300)


class CandidateProfile(StrictModel):
    full_name: str = Field(default="", max_length=160)
    headline: str = Field(default="", max_length=160)
    bio: str = Field(default="", max_length=4000)
    location: str = Field(default="", max_length=120)
    work_format: WorkFormat = "any"
    skills: list[str] = Field(default_factory=list, max_length=40)
    roles: list[ProfileListItem] = Field(default_factory=list, max_length=20)
    experience_years: float | None = Field(default=None, ge=0, le=60, allow_inf_nan=False)
    soft_skills: list[ProfileListItem] = Field(default_factory=list, max_length=20)
    specialization: Specialization = "backend_python"
    declared_grade: Grade = "junior"
    salary_min: int | None = Field(default=None, ge=1, le=10_000_000)
    salary_max: int | None = Field(default=None, ge=1, le=10_000_000)
    contacts: Contacts = Field(default_factory=Contacts)
    fsp_id: str = Field(default="", max_length=80)
    fsp_link: str = Field(default="", max_length=300)
    visible: bool = False
    consent_version: Literal["2026-10-06"] = CONSENT_VERSION

    @model_validator(mode="after")
    def check_salary(self):
        if self.salary_min and self.salary_max and self.salary_max < self.salary_min:
            raise ValueError("salary_max must be >= salary_min")
        if any(len(x) > 80 or not x.strip() for x in self.skills):
            raise ValueError("Skills must be nonempty and <=80 characters")
        return self


class Survey(StrictModel):
    specialization: Specialization
    declared_grade: Grade
    answers: dict[str, Any] = Field(default_factory=dict)


class CompanyInput(StrictModel):
    name: str = Field(min_length=2, max_length=160)
    description: str = Field(default="", max_length=4000)
    website: str = Field(default="", max_length=300)
    inn: str = Field(default="", pattern=r"^(\d{10}|\d{12})?$")
    contact_method: str = Field(min_length=3, max_length=200)


class Salary(StrictModel):
    salary_min: int = Field(ge=1, le=10_000_000)
    salary_max: int = Field(ge=1, le=10_000_000)

    @model_validator(mode="after")
    def range(self):
        if self.salary_max < self.salary_min:
            raise ValueError("salary_max must be >= salary_min")
        return self


class NeedInput(Salary):
    title: str = Field(min_length=3, max_length=160)
    description: str = Field(min_length=10, max_length=6000)
    specialization: Specialization
    grade: Grade
    required_skills: list[str] = Field(default_factory=list, max_length=30)
    preferred_skills: list[str] = Field(default_factory=list, max_length=30)
    work_format: WorkFormat = "any"
    location: str = Field(default="", max_length=120)
    require_fsp: bool = False

    @model_validator(mode="after")
    def skill_limits(self):
        if any(not x.strip() or len(x) > 80 for x in self.required_skills + self.preferred_skills):
            raise ValueError("Skills must be nonempty and <=80 characters")
        return self


class VacancyInput(NeedInput):
    published: bool = False


class ParseText(StrictModel):
    text: str = Field(min_length=10, max_length=12000)


class AttemptStart(StrictModel):
    specialization: Specialization
    grade: Grade


class AttemptSubmit(StrictModel):
    answers: dict[str, Any]


class InvitationInput(Salary):
    need_id: str = Field(min_length=36, max_length=36)
    candidate_id: str = Field(min_length=36, max_length=36)
    message: str = Field(min_length=10, max_length=4000)
    contact_method: str = Field(min_length=3, max_length=200)


class Decision(StrictModel):
    decision: Literal["accepted", "rejected"]


class ShortlistInput(StrictModel):
    need_id: str
    candidate_id: str


class ResponseInput(StrictModel):
    message: str = Field(min_length=5, max_length=4000)


class MicrotaskInput(StrictModel):
    title: str = Field(min_length=3, max_length=160)
    description: str = Field(min_length=10, max_length=4000)
    specialization: Specialization
    grade: Grade
    expected_answer: str = Field(min_length=1, max_length=2000)
    due_at: float | None = None


class MicrotaskSubmit(StrictModel):
    answer: str = Field(min_length=1, max_length=2000)


class LinkInput(StrictModel):
    platform: Literal["max"]


class ReportInput(StrictModel):
    subject_type: Literal["company", "vacancy", "invitation", "microtask"]
    subject_id: str = Field(min_length=36, max_length=36)
    reason: str = Field(min_length=10, max_length=3000)
