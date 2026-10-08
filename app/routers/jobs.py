"""HTTP API."""
import io
import re
import zipfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ACTIVE_JOB_STATUSES,
    Certificate,
    CertificateStatus,
    GenerationJob,
)
from app.schemas import (
    CertificateListOut,
    CertificateOut,
    FailureOut,
    JobCounts,
    JobCreate,
    JobLinks,
    JobOut,
)
from app.services.jobs import create_job, get_counts, process_job

router = APIRouter(prefix="/api/v1", tags=["certificate jobs"])


# ------------------------------------------------------------ dependencies
def get_db(request: Request):
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def _get_job_or_404(db: Session, job_id: str) -> GenerationJob:
    job = db.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ----------------------------------------------------------------- helpers
def _download_name(cert: Certificate) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", cert.recipient_name or "").strip("-")[:50]
    return f"{cert.certificate_number}_{slug or 'recipient'}.pdf"


def _resolve_file(request: Request, cert: Certificate) -> Path | None:
    """Absolute path of the stored PDF, or None if missing / outside storage."""
    if not cert.file_path:
        return None
    storage_dir = request.app.state.settings.storage_dir.resolve()
    path = (storage_dir / cert.file_path).resolve()
    if storage_dir not in path.parents or not path.is_file():
        return None
    return path


def _certificate_out(request: Request, cert: Certificate) -> CertificateOut:
    ok = cert.status == CertificateStatus.SUCCESS.value
    return CertificateOut(
        id=cert.id,
        index=cert.position,
        recipient_name=cert.recipient_name,
        recipient_email=cert.recipient_email,
        certificate_number=cert.certificate_number,
        status=cert.status,
        error=cert.error,
        generated_at=cert.generated_at,
        download_url=str(
            request.url_for(
                "download_certificate", job_id=cert.job_id, certificate_id=cert.id
            )
        )
        if ok
        else None,
    )


def _job_out(request: Request, db: Session, job: GenerationJob) -> JobOut:
    counts = get_counts(db, job.id)
    done = counts["succeeded"] + counts["failed"]
    progress = round(done / counts["total"] * 100, 1) if counts["total"] else 100.0
    failures = db.execute(
        select(Certificate.position, Certificate.recipient_name, Certificate.error)
        .where(
            Certificate.job_id == job.id,
            Certificate.status == CertificateStatus.FAILED.value,
        )
        .order_by(Certificate.position)
    ).all()
    return JobOut(
        id=job.id,
        status=job.status,
        event_name=job.event_name,
        issued_by=job.issued_by,
        issue_date=job.issue_date,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        counts=JobCounts(**counts),
        progress_percent=progress,
        error_message=job.error_message,
        failures=[
            FailureOut(index=pos, recipient_name=name, error=err)
            for pos, name, err in failures
        ],
        links=JobLinks(
            self=str(request.url_for("get_job", job_id=job.id)),
            certificates=str(request.url_for("list_certificates", job_id=job.id)),
            download_all=str(request.url_for("download_job_zip", job_id=job.id)),
        ),
    )


# --------------------------------------------------------------- endpoints
@router.post("/jobs", response_model=JobOut, status_code=202, summary="Submit a bulk generation job")
def submit_job(
    payload: JobCreate,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    state = request.app.state
    if len(payload.recipients) > state.settings.max_recipients:
        raise HTTPException(
            status_code=422,
            detail=f"Too many recipients: at most {state.settings.max_recipients} per request",
        )

    job = create_job(db, payload)
    if job.status in ACTIVE_JOB_STATUSES:
        background_tasks.add_task(
            process_job,
            state.session_factory,
            state.settings.storage_dir,
            job.id,
            state.generator,
        )

    response.headers["Location"] = str(request.url_for("get_job", job_id=job.id))
    return _job_out(request, db, job)


@router.get("/jobs/{job_id}", response_model=JobOut, name="get_job", summary="Job status and progress")
def get_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    return _job_out(request, db, _get_job_or_404(db, job_id))


@router.get(
    "/jobs/{job_id}/certificates",
    response_model=CertificateListOut,
    name="list_certificates",
    summary="List the certificates of a job",
)
def list_certificates(
    job_id: str,
    request: Request,
    status: CertificateStatus | None = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    _get_job_or_404(db, job_id)
    conditions = [Certificate.job_id == job_id]
    if status is not None:
        conditions.append(Certificate.status == status.value)

    total = db.scalar(select(func.count()).select_from(Certificate).where(*conditions))
    rows = db.scalars(
        select(Certificate)
        .where(*conditions)
        .order_by(Certificate.position)
        .limit(limit)
        .offset(offset)
    ).all()
    return CertificateListOut(
        total=total,
        limit=limit,
        offset=offset,
        items=[_certificate_out(request, c) for c in rows],
    )


@router.get(
    "/jobs/{job_id}/certificates/{certificate_id}/download",
    name="download_certificate",
    summary="Download one certificate (PDF)",
)
def download_certificate(
    job_id: str, certificate_id: str, request: Request, db: Session = Depends(get_db)
):
    cert = db.scalar(
        select(Certificate).where(
            Certificate.id == certificate_id, Certificate.job_id == job_id
        )
    )
    if cert is None:
        raise HTTPException(status_code=404, detail="Certificate not found")
    if cert.status == CertificateStatus.PENDING.value:
        raise HTTPException(status_code=409, detail="Certificate is not generated yet")
    if cert.status == CertificateStatus.FAILED.value:
        raise HTTPException(
            status_code=409, detail=f"Certificate generation failed: {cert.error}"
        )
    path = _resolve_file(request, cert)
    if path is None:
        raise HTTPException(status_code=404, detail="Certificate file is no longer available")
    return FileResponse(path, media_type="application/pdf", filename=_download_name(cert))


@router.get(
    "/jobs/{job_id}/download",
    name="download_job_zip",
    summary="Download all successfully generated certificates as a ZIP",
)
def download_job_zip(job_id: str, request: Request, db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    if job.status in ACTIVE_JOB_STATUSES:
        raise HTTPException(
            status_code=409, detail="Job is still being processed; check its status first"
        )
    certs = db.scalars(
        select(Certificate)
        .where(
            Certificate.job_id == job_id,
            Certificate.status == CertificateStatus.SUCCESS.value,
        )
        .order_by(Certificate.position)
    ).all()

    buffer = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for cert in certs:
            path = _resolve_file(request, cert)
            if path is not None:
                archive.write(path, arcname=_download_name(cert))
                added += 1
    if added == 0:
        raise HTTPException(status_code=404, detail="No certificates available for this job")

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="certificates_{job_id}.zip"'},
    )
