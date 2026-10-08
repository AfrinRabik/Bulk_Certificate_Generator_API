"""Job creation, background processing and status queries.

Flow
----
1. create_job()  - validates every recipient, stores one Certificate row per
                   recipient (invalid ones are stored as `failed` immediately).
2. process_job() - runs in the background; generates every `pending`
                   certificate one by one. Each certificate is wrapped in its
                   own try/except and committed on its own, so
                   * one failure never stops the others, and
                   * progress is visible to GET /jobs/{id} while running.
"""
import logging
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    ACTIVE_JOB_STATUSES,
    Certificate,
    CertificateStatus,
    GenerationJob,
    JobStatus,
    utcnow,
)
from app.schemas import JobCreate, validate_recipient
from app.services.certificate import CertificateData, generate_certificate_pdf

logger = logging.getLogger(__name__)

Generator = Callable[[CertificateData, Path], None]


def _best_effort_name(raw: Any) -> str | None:
    """Name to show next to a failed validation, if the input had a usable one."""
    if isinstance(raw, dict) and isinstance(raw.get("name"), str):
        return raw["name"].strip()[:200] or None
    return None


def create_job(db: Session, payload: JobCreate) -> GenerationJob:
    job = GenerationJob(
        event_name=payload.event_name,
        issued_by=payload.issued_by,
        issue_date=payload.issue_date or date.today(),
        total=len(payload.recipients),
        status=JobStatus.QUEUED.value,
    )
    db.add(job)
    db.flush()  # assigns job.id

    pending = 0
    for position, raw in enumerate(payload.recipients):
        recipient, error = validate_recipient(raw)
        cert = Certificate(
            job_id=job.id,
            position=position,
            certificate_number=f"CERT-{job.id[:8].upper()}-{position + 1:05d}",
        )
        if recipient is None:
            cert.status = CertificateStatus.FAILED.value
            cert.recipient_name = _best_effort_name(raw)
            cert.error = f"Validation failed - {error}"
        else:
            cert.status = CertificateStatus.PENDING.value
            cert.recipient_name = recipient.name
            cert.recipient_email = recipient.email
            cert.achievement = recipient.achievement
            pending += 1
        db.add(cert)

    if pending == 0:  # nothing to generate -> job is finished right away
        job.status = JobStatus.FAILED.value
        job.completed_at = utcnow()
    db.commit()
    return job


def get_counts(db: Session, job_id: str) -> dict[str, int]:
    """Counts come from the certificate rows themselves (single source of truth)."""
    rows = db.execute(
        select(Certificate.status, func.count())
        .where(Certificate.job_id == job_id)
        .group_by(Certificate.status)
    ).all()
    by_status = {status: count for status, count in rows}
    succeeded = by_status.get(CertificateStatus.SUCCESS.value, 0)
    failed = by_status.get(CertificateStatus.FAILED.value, 0)
    pending = by_status.get(CertificateStatus.PENDING.value, 0)
    return {
        "total": succeeded + failed + pending,
        "succeeded": succeeded,
        "failed": failed,
        "pending": pending,
    }


def _generate_one(
    db: Session,
    job: GenerationJob,
    cert: Certificate,
    storage_dir: Path,
    generator: Generator,
) -> None:
    relative_path = f"{job.id}/{cert.id}.pdf"
    try:
        generator(
            CertificateData(
                recipient_name=cert.recipient_name,
                event_name=job.event_name,
                issued_by=job.issued_by,
                issue_date=job.issue_date,
                certificate_number=cert.certificate_number,
                achievement=cert.achievement,
            ),
            storage_dir / relative_path,
        )
    except Exception as exc:  # noqa: BLE001 - isolate ANY per-certificate failure
        logger.warning("Certificate %s failed: %s", cert.id, exc)
        (storage_dir / relative_path).unlink(missing_ok=True)
        cert.status = CertificateStatus.FAILED.value
        cert.error = f"Generation failed - {exc}"[:500]
        cert.file_path = None
    else:
        cert.status = CertificateStatus.SUCCESS.value
        cert.file_path = relative_path
        cert.error = None
        cert.generated_at = utcnow()
    db.commit()  # commit per certificate -> live progress + crash safety


def process_job(
    session_factory: sessionmaker,
    storage_dir: Path,
    job_id: str,
    generator: Generator | None = None,
) -> None:
    """Generate all pending certificates of a job. Safe to call again (idempotent):
    only `pending` certificates are processed, finished jobs are skipped."""
    generator = generator or generate_certificate_pdf
    storage_dir = Path(storage_dir)

    with session_factory() as db:
        job = db.get(GenerationJob, job_id)
        if job is None or job.status not in ACTIVE_JOB_STATUSES:
            return
        job.status = JobStatus.PROCESSING.value
        job.started_at = job.started_at or utcnow()
        db.commit()

        try:
            pending_ids = db.scalars(
                select(Certificate.id)
                .where(
                    Certificate.job_id == job_id,
                    Certificate.status == CertificateStatus.PENDING.value,
                )
                .order_by(Certificate.position)
            ).all()
            for cert_id in pending_ids:
                cert = db.get(Certificate, cert_id)
                _generate_one(db, job, cert, storage_dir, generator)

            counts = get_counts(db, job_id)
            if counts["succeeded"] == 0:
                job.status = JobStatus.FAILED.value
            elif counts["failed"] > 0:
                job.status = JobStatus.COMPLETED_WITH_ERRORS.value
            else:
                job.status = JobStatus.COMPLETED.value
            job.completed_at = utcnow()
            db.commit()
        except Exception as exc:  # noqa: BLE001 - e.g. the database went away
            logger.exception("Job %s crashed", job_id)
            db.rollback()
            job = db.get(GenerationJob, job_id)
            job.status = JobStatus.FAILED.value
            job.error_message = f"Processing aborted: {exc}"[:500]
            job.completed_at = utcnow()
            db.commit()
