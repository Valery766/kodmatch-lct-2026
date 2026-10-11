from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator, field_validator, ValidationInfo
from typing import Literal
from pydantic import EmailStr, Field, TypeAdapter


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False, hide_input_in_errors=True)
    database_url: str = "sqlite:///./fsp-match.db"
    environment: str = "development"
    app_url: str = "http://localhost:5173"
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://testserver"
    session_ttl_seconds: int = 86400 * 7
    cookie_name: str = "fsp_session"
    smtp_host: str = "localhost"
    smtp_tls_mode: Literal["none", "starttls", "ssl", "auto"] = "none"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = Field(default="", repr=False)
    smtp_starttls: bool = False
    smtp_ssl: bool = False
    smtp_from: str = "КОД.МЭТЧ <noreply@digital-projects.tech>"
    smtp_reply_to: str = Field(default="", max_length=254)
    seed_demo: bool = False
    demo_password: str = ""
    llm_base_url: str = ""
    llm_api_key: str = Field(default="", repr=False)
    llm_model: str = ""
    llm_timeout_seconds: int = Field(default=12, ge=1, le=120)
    llm_personal_data_allowed: bool = False
    llm_max_concurrency: int = Field(default=2, ge=1, le=16)
    max_bot_token: str = Field(default="", repr=False)
    max_bot_username: str = ""
    max_api_base_url: str = "https://platform-api2.max.ru"
    max_ca_bundle: str = ""
    max_webhook_secret: str = Field(default="", repr=False)
    pdf_font_path: str = ""
    release_version: str = "0.1.0"
    registration_enabled: bool = True
    legal_operator_name: str = ""
    legal_operator_contact: str = ""

    @field_validator("smtp_tls_mode", mode="before")
    @classmethod
    def normalize_tls(cls, value):
        return str(value or "none").strip().lower()

    @field_validator("smtp_reply_to", mode="before")
    @classmethod
    def validate_reply_to(cls, value):
        # One mailbox only. Reject control characters before normalizing so a
        # newline cannot be stripped into a seemingly valid header value.
        if value is None or value == "":
            return ""
        if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("SMTP_REPLY_TO must be one mailbox without control characters")
        normalized = TypeAdapter(EmailStr).validate_python(value.strip())
        if "<" in value or ">" in value or "," in value or ";" in value:
            raise ValueError("SMTP_REPLY_TO must be one bare mailbox")
        return str(normalized)

    @field_validator("smtp_port", mode="before")
    @classmethod
    def normalize_smtp_port(cls, value, info: ValidationInfo):
        if value is None or value == "":
            return {"ssl": 465, "starttls": 587}.get(info.data.get("smtp_tls_mode"), 1025)
        return value

    @field_validator("smtp_starttls", "smtp_ssl", mode="before")
    @classmethod
    def normalize_blank_bool(cls, value):
        return False if value is None or value == "" else value

    @field_validator("llm_max_concurrency", "llm_timeout_seconds", mode="before")
    @classmethod
    def normalize_blank_llm_limits(cls, value, info: ValidationInfo):
        return (2 if info.field_name == "llm_max_concurrency" else 12) if value is None or value == "" else value

    @property
    def production(self):
        return self.environment == "production"

    @property
    def origins(self):
        return [x.strip().rstrip("/") for x in self.allowed_origins.split(",") if x.strip()]

    @property
    def preauth_cookie_name(self):
        return "__Host-fsp_preauth" if self.production else "fsp_preauth"

    @model_validator(mode="after")
    def production_guard(self):
        if self.smtp_ssl and self.smtp_starttls:
            raise ValueError("Configure either SMTP SSL or STARTTLS")
        if self.smtp_tls_mode == "auto":
            self.smtp_tls_mode = "ssl" if self.smtp_port == 465 else "starttls" if self.smtp_port == 587 else "none"
        if self.smtp_tls_mode == "none" and (self.smtp_ssl or self.smtp_starttls):
            self.smtp_tls_mode = "ssl" if self.smtp_ssl else "starttls"
        if "smtp_port" not in self.model_fields_set and self.smtp_tls_mode != "none":
            self.smtp_port = 465 if self.smtp_tls_mode == "ssl" else 587
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("SMTP_PORT must be 1..65535")
        self.smtp_ssl = self.smtp_tls_mode == "ssl"
        self.smtp_starttls = self.smtp_tls_mode == "starttls"
        if self.production and not self.app_url.startswith("https://"):
            raise ValueError("Production APP_URL must use HTTPS")
        if self.production:
            self.cookie_name = "__Host-fsp_session"
        if self.production and self.registration_enabled:
            if not self.legal_operator_name or not self.legal_operator_contact:
                raise ValueError("Public registration requires configured legal operator")
            if not self.smtp_host or self.smtp_host in {"localhost", "mailpit"} or self.smtp_from == "КОД.МЭТЧ <noreply@digital-projects.tech>":
                raise ValueError("Public registration requires approved external SMTP and sender")
            if self.smtp_tls_mode not in {"starttls", "ssl"}:
                raise ValueError("Public registration requires encrypted SMTP transport")
        if self.seed_demo and len(self.demo_password) < 12:
            raise ValueError("SEED_DEMO requires DEMO_PASSWORD of at least 12 characters")
        return self


@lru_cache
def get_settings():
    return Settings()
