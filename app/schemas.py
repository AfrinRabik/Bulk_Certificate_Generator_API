"""Request / response models (Pydantic)."""
from datetime import date, datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    ValidationError,
    field_validator,
)

from app.services.certificate import can_render


def _check_text(value: str) -> str:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("must not contain control characters or line breaks")
    if not can_render(value):
        raise ValueError("contains characters that cannot be printed on the certificate")
    return value


# ----------------------------------------------------------------- requests
class RecipientIn(BaseModel):
    """One recipient. Validated INDIVIDUALLY so one bad entry never rejects the batch."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    email: EmailStr | None = None
    achievement: str | None = Field(default=None, max_length=200)

    @field_validator("name", "achievement")
    @classmethod
    def _printable(cls, value: str | None) -> str | None:
        return _check_text(value) if value else value

    @field_validator("achievement")
    @classmethod
    def _blank_achievement_is_none(cls, value: str | None) -> str | None:
        return value or None


class JobCreate(BaseModel):
    """The batch request. Only STRUCTURAL problems are rejected here (HTTP 422).

    `recipients` is deliberately a list of raw values: each one is validated
    later with RecipientIn so valid recipients still get their certificates.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    event_name: str = Field(min_length=1, max_length=100)
    issued_by: str = Field(min_length=1, max_length=100)
    issue_date: date | None = None  # defaults to today
    recipients: list[Any] = Field(min_length=1)

    @field_validator("event_name", "issued_by")
    @classmethod
    def _printable(cls, value: str) -> str:
        return _check_text(value)


def validate_recipient(raw: Any) -> tuple[RecipientIn | None, str | None]:
    """Return (recipient, None) if valid, otherwise (None, readable error)."""
    try:
        return RecipientIn.model_validate(raw), None
    except ValidationError as exc:
        parts = []
        for err in exc.errors():
            field = ".".join(str(p) for p in err["loc"]) or "recipient"
            parts.append(f"{field}: {err['msg']}")
        return None, "; ".join(parts)


# ---------------------------------------------------------------- responses
class JobCounts(BaseModel):
    total: int
    succeeded: int
    failed: int
    pending: int


class FailureOut(BaseModel):
    index: int
    recipient_name: str | None
    error: str | None


class JobLinks(BaseModel):
    self: str
    certificates: str
    download_all: str


class JobOut(BaseModel):
    id: str
    status: str
    event_name: str
    issued_by: str
    issue_date: date
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    counts: JobCounts
    progress_percent: float
    error_message: str | None = None
    failures: list[FailureOut]
    links: JobLinks


class CertificateOut(BaseModel):
    id: str
    index: int
    recipient_name: str | None
    recipient_email: str | None
    certificate_number: str
    status: str
    error: str | None
    generated_at: datetime | None
    download_url: str | None


class CertificateListOut(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CertificateOut]
