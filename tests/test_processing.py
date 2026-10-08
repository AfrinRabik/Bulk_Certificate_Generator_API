"""Service-level tests: job progress and idempotent processing."""
from app.models import Certificate, CertificateStatus, GenerationJob, JobStatus
from app.schemas import JobCreate
from app.services.certificate import generate_certificate_pdf
from app.services.jobs import create_job, get_counts, process_job
from tests.conftest import job_payload


def test_progress_is_visible_while_job_is_running(settings, make_client):
    client = make_client()
    session_factory = client.app.state.session_factory
    with session_factory() as db:
        job_id = create_job(db, JobCreate(**job_payload())).id

    snapshots = []  # counts seen just BEFORE each certificate is generated

    def spying_generator(data, path):
        with session_factory() as other_session:  # a separate "client" polling status
            job = other_session.get(GenerationJob, job_id)
            snapshots.append((job.status, get_counts(other_session, job_id)))
        generate_certificate_pdf(data, path)

    process_job(session_factory, settings.storage_dir, job_id, spying_generator)

    assert [s[0] for s in snapshots] == ["processing"] * 3
    assert [(s[1]["succeeded"], s[1]["pending"]) for s in snapshots] == [(0, 3), (1, 2), (2, 1)]
    with session_factory() as db:
        assert db.get(GenerationJob, job_id).status == JobStatus.COMPLETED.value


def test_processing_a_finished_job_again_does_nothing(settings, make_client):
    client = make_client()
    session_factory = client.app.state.session_factory
    with session_factory() as db:
        job_id = create_job(db, JobCreate(**job_payload())).id
    process_job(session_factory, settings.storage_dir, job_id)

    calls = []
    process_job(session_factory, settings.storage_dir, job_id, lambda d, p: calls.append(d))

    assert calls == []


def test_resuming_an_interrupted_job_only_generates_pending_certificates(settings, make_client):
    """Simulates a server restart half-way through a job."""
    client = make_client()
    session_factory = client.app.state.session_factory
    with session_factory() as db:
        job_id = create_job(db, JobCreate(**job_payload())).id

    # first run "crashes" after the first certificate
    class Crash(BaseException):  # BaseException: bypasses the per-certificate handler
        pass

    state = {"n": 0}

    def crashing(data, path):
        state["n"] += 1
        if state["n"] == 2:
            raise Crash()
        generate_certificate_pdf(data, path)

    try:
        process_job(session_factory, settings.storage_dir, job_id, crashing)
    except Crash:
        pass
    with session_factory() as db:
        assert get_counts(db, job_id)["succeeded"] == 1
        assert db.get(GenerationJob, job_id).status == JobStatus.PROCESSING.value

    generated = []

    def recording(data, path):
        generated.append(data.recipient_name)
        generate_certificate_pdf(data, path)

    process_job(session_factory, settings.storage_dir, job_id, recording)

    assert generated == ["Ben Carter", "Chitra Devi"]  # Asha was NOT regenerated
    with session_factory() as db:
        assert db.get(GenerationJob, job_id).status == JobStatus.COMPLETED.value
        assert get_counts(db, job_id)["succeeded"] == 3
