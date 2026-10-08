"""Database models.

GenerationJob  1 ---- *  Certificate

A Certificate row exists for EVERY recipient in the request - including the
ones that failed validation. That is what lets the status endpoint tell the
client exactly which recipients succeeded and which failed (and why).
"""
import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite does not keep timezone info anyway)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class JobStatus(str, enum.Enum):
    QUEUED = "queued"                          # accepted, waiting for processing
    PROCESSING = "processing"                  # certificates are being generated
    COMPLETED = "completed"                    # every certificate generated
    COMPLETED_WITH_ERRORS = "completed_with_errors"  # some succeeded, some failed
    FAILED = "failed"                          # nothing could be generated


ACTIVE_JOB_STATUSES = {JobStatus.QUEUED.value, JobStatus.PROCESSING.value}


class CertificateStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_name: Mapped[str] = mapped_column(String(200))
    issued_by: Mapped[str] = mapped_column(String(200))
    issue_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(
        String(32), default=JobStatus.QUEUED.value, index=True
    )
    total: Mapped[int] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    certificates: Mapped[list["Certificate"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="Certificate.position",
    )


class Certificate(Base):
    __tablename__ = "certificates"
    __table_args__ = (Index("ix_certificates_job_status", "job_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True
    )
    # 0-based position of the recipient in the submitted list, so clients can
    # map results back to their own input.
    position: Mapped[int] = mapped_column(Integer)
    recipient_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    recipient_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    achievement: Mapped[str | None] = mapped_column(String(200), nullable=True)
    certificate_number: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(
        String(16), default=CertificateStatus.PENDING.value
    )
    # Path relative to the storage directory, e.g. "<job_id>/<certificate_id>.pdf"
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    job: Mapped[GenerationJob] = relationship(back_populates="certificates")
