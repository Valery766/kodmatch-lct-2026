import re
import time
import unicodedata
from sqlalchemy import select
from fastapi import HTTPException
from .models import Company, ContactGrant, History, MessengerLink, Notification, Outbox, Profile, User
from .schemas import CandidateProfile


def get_profile(db, user):
    row = db.get(Profile, user.id)
    if not row:
        row = Profile(user_id=user.id, data=CandidateProfile().model_dump(), visible=False, verification={}, fsp={})
        db.add(row)
        db.flush()
    return row


def get_company(db, user):
    row = db.scalar(select(Company).where(Company.user_id == user.id))
    if not row:
        raise HTTPException(409, "Сначала заполните профиль компании")
    return row


def owned(db, cls, identifier, owner_field, owner_id, lock=False):
    query = select(cls).where(cls.id == identifier, getattr(cls, owner_field) == owner_id)
    if lock:
        query = query.with_for_update()
    row = db.scalar(query)
    if not row:
        raise HTTPException(404, "Запись не найдена")
    return row


def record(db, user_id, kind, payload):
    item = History(user_id=user_id, kind=kind, payload=payload)
    db.add(item)
    db.flush()
    return item


def notify(db, user_id, kind, payload):
    # Only business IDs, states, public company and salary go here; never candidate contacts.
    item = Notification(user_id=user_id, kind=kind, payload=payload)
    db.add(item)
    user = db.get(User, user_id)
    if user and user.email_verified and not user.is_demo:
        db.add(Outbox(user_id=user_id, channel="email", kind=kind,
                      payload={"to": user.email, "subject": "КОД.МЭТЧ: новое событие", "notification": payload}))
    for link in db.scalars(select(MessengerLink).where(MessengerLink.user_id == user_id, MessengerLink.platform == "max")):
        db.add(Outbox(user_id=user_id, channel=link.platform, kind=kind,
                      payload={"recipient_id": link.recipient_id, **payload}))
    return item


def grant_contacts(db, company_id, candidate_id, source, source_id):
    row = db.scalar(select(ContactGrant).where(ContactGrant.company_id == company_id, ContactGrant.candidate_id == candidate_id).with_for_update())
    if not row:
        row = ContactGrant(company_id=company_id, candidate_id=candidate_id, source=source, source_id=source_id)
        db.add(row)
    elif row.revoked_at:
        row.revoked_at = None
        row.source, row.source_id, row.created_at = source, source_id, time.time()
    return row


def has_grant(db, company_id, candidate_id):
    return bool(db.scalar(select(ContactGrant.id).where(ContactGrant.company_id == company_id, ContactGrant.candidate_id == candidate_id, ContactGrant.revoked_at.is_(None))))


def redact_contacts(text):
    if not isinstance(text, str):
        return text
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}", "[контакт скрыт]", text)
    text = re.sub(r"(?:\+?\d[\d ()-]{7,}\d)", "[контакт скрыт]", text)
    text = re.sub(r"(?:https?://)?(?:t\.me|telegram\.me)/\S+|(?<!\w)@[A-Za-z0-9_]{4,}", "[контакт скрыт]", text)
    text = re.sub(r"(?:https?://)?max\.ru/(?:u/)?\S+", "[контакт скрыт]", text)
    text = re.sub(r"(?:https?://|www\.)\S+", "[ссылка скрыта]", text)
    return text


def clean_public(value):
    if isinstance(value, dict):
        return {k: clean_public(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_public(x) for x in value]
    return redact_contacts(value)


def profile_payload(db, profile, company_id=None, owner=False, *, resolved_user=None, resolved_contact_access=None):
    # Batch callers resolve these from the same request's owned-company query.
    # No cross-request cache; a missing/wrong user fails closed.
    user = resolved_user if resolved_user is not None else db.get(User, profile.user_id)
    if not user or user.id != profile.user_id:
        raise HTTPException(404, "Профиль не найден")
    data = dict(profile.data)
    specialization = data.get("specialization", "backend_python")
    verification = profile.verification.get(specialization, {})
    public = {k: v for k, v in data.items() if k not in {"contacts", "full_name", "fsp_id", "fsp_link", "consent_version"}}
    name_parts = data.get("full_name", "").split()
    public["display_name"] = (name_parts[0] + (" " + name_parts[-1][0] + "." if len(name_parts) > 1 else "")) if name_parts else "Специалист"
    public = clean_public(public)
    public.update(id=profile.user_id, verified_grade=verification.get("grade"), verified_skills=verification.get("skills", []),
                  verified_at=verification.get("verified_at"), verification_version=verification.get("version"),
                  test_score=verification.get("score"), verification_origin="synthetic_seed" if verification.get("synthetic") else "assessment" if verification else "none",
                  verified_skill_evidence=verification.get("skill_provenance", {}),
                  fsp_achievements=profile.fsp.get("achievements", []), fsp_status=profile.fsp.get("status", "not_linked"),
                  is_demo=user.is_demo, visible=profile.visible, updated_at=profile.updated_at,
                  contact_access=owner or (company_id is not None and (resolved_contact_access if resolved_contact_access is not None else has_grant(db, company_id, profile.user_id))))
    public["category"] = specialization + "/" + verification["grade"] if verification.get("grade") else "not_verified"
    if owner or public["contact_access"]:
        # Keep legacy fields in the owner's stored profile, but do not publish
        # a retired messenger as an active contact channel to employers.
        contacts = data.get("contacts", {})
        if not owner:
            contacts = {key: value for key, value in contacts.items() if key != "telegram"}
        public.update(full_name=data.get("full_name", ""), contacts={"email": user.email, **contacts})
    if owner:
        public.update({k: v for k, v in data.items() if k not in {"contacts", "visible"}})
        public.update(fsp_id=data.get("fsp_id", ""), fsp_link=data.get("fsp_link", ""), consent_version=user.consent_version,
                      consent_at=user.consent_at, verified_categories=profile.verification)
    return public


def profile_payloads(db, profiles, company_id=None):
    """Resolve a bank in bounded queries while applying exactly the same gate.

    Each call reads current grants for the explicit company. Other companies,
    revoked grants, hidden-owner fields and public projections remain separate.
    This removes per-profile User/grant lookups under concurrent bank searches.
    """
    rows = list(profiles)
    if not rows:
        return {}
    identifiers = [profile.user_id for profile in rows]
    users = {user.id:user for user in db.scalars(select(User).where(User.id.in_(identifiers)))}
    grants = set()
    if company_id is not None:
        grants = set(db.scalars(select(ContactGrant.candidate_id).where(ContactGrant.company_id == company_id, ContactGrant.candidate_id.in_(identifiers), ContactGrant.revoked_at.is_(None))))
    return {profile.user_id:profile_payload(db, profile, company_id=company_id, resolved_user=users.get(profile.user_id), resolved_contact_access=profile.user_id in grants) for profile in rows}


def company_payload(company):
    return {"id": company.id, **company.data, "verification_status": company.verification_status, "created_at": company.created_at}


def answer_normalized(answer):
    return " ".join(unicodedata.normalize("NFKC", answer).strip().casefold().split())
