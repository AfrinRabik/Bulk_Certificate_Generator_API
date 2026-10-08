"""API tests: job creation, validation, status, failures and retrieval.

Note: FastAPI's TestClient runs background tasks before `client.post(...)`
returns, so the job is already finished when we query its status.
"""
import io
import zipfile

from app.models import Certificate, CertificateStatus, GenerationJob
from app.schemas import JobCreate
from app.services.jobs import create_job
from tests.conftest import job_payload


# ------------------------------------------------------------- creating jobs
def test_create_job_returns_202_with_id_location_and_links(client):
    resp = client.post("/api/v1/jobs", json=job_payload())

    assert resp.status_code == 202
    body = resp.json()
    assert body["id"]
    assert body["status"] == "queued"  # response is sent before processing starts
    assert body["counts"]["total"] == 3
    assert resp.headers["location"].endswith(f"/api/v1/jobs/{body['id']}")
    assert body["links"]["certificates"].endswith(f"/jobs/{body['id']}/certificates")


def test_job_runs_in_background_and_completes(client):
    job_id = client.post("/api/v1/jobs", json=job_payload()).json()["id"]

    body = client.get(f"/api/v1/jobs/{job_id}").json()

    assert body["status"] == "completed"
    assert body["counts"] == {"total": 3, "succeeded": 3, "failed": 0, "pending": 0}
    assert body["progress_percent"] == 100.0
    assert body["failures"] == []
    assert body["started_at"] and body["completed_at"]


# ---------------------------------------------------------- request validation
def test_missing_required_fields_are_rejected(client):
    assert client.post("/api/v1/jobs", json={"recipients": [{"name": "A"}]}).status_code == 422
    assert client.post("/api/v1/jobs", json=job_payload(event_name="")).status_code == 422
    assert client.post("/api/v1/jobs", json=job_payload(issued_by="  ")).status_code == 422


def test_empty_or_malformed_recipient_list_is_rejected(client):
    assert client.post("/api/v1/jobs", json=job_payload(recipients=[])).status_code == 422
    assert client.post("/api/v1/jobs", json=job_payload(recipients="nope")).status_code == 422


def test_too_many_recipients_is_rejected(client):
    recipients = [{"name": f"Person {i}"} for i in range(51)]  # limit is 50 in tests
    resp = client.post("/api/v1/jobs", json=job_payload(recipients=recipients))
    assert resp.status_code == 422
    assert "at most 50" in resp.json()["detail"]


def test_unprintable_event_name_is_rejected(client):
    resp = client.post("/api/v1/jobs", json=job_payload(event_name="தமிழ் Workshop"))
    assert resp.status_code == 422


def test_invalid_recipients_do_not_block_valid_ones(client):
    recipients = [
        {"name": "Valid One", "email": "one@example.com"},
        {"name": "", "email": "blank@example.com"},          # blank name
        {"email": "noname@example.com"},                      # missing name
        {"name": "Bad Email", "email": "not-an-email"},       # invalid email
        "just a string",                                      # not an object
        {"name": "Valid Two"},
    ]
    job_id = client.post("/api/v1/jobs", json=job_payload(recipients=recipients)).json()["id"]

    body = client.get(f"/api/v1/jobs/{job_id}").json()

    assert body["status"] == "completed_with_errors"
    assert body["counts"] == {"total": 6, "succeeded": 2, "failed": 4, "pending": 0}
    assert [f["index"] for f in body["failures"]] == [1, 2, 3, 4]
    assert all("Validation failed" in f["error"] for f in body["failures"])
    assert body["failures"][2]["recipient_name"] == "Bad Email"


def test_job_with_only_invalid_recipients_fails_immediately(client):
    job_id = client.post(
        "/api/v1/jobs", json=job_payload(recipients=[{"name": ""}, {}])
    ).json()["id"]

    body = client.get(f"/api/v1/jobs/{job_id}").json()
    assert body["status"] == "failed"
    assert body["counts"]["failed"] == 2
    assert body["completed_at"] is not None


# ---------------------------------------------------- individual failures
def test_one_generation_failure_does_not_stop_the_others(make_client):
    from app.services.certificate import generate_certificate_pdf

    def flaky_generator(data, path):
        if data.recipient_name == "Boom":
            raise RuntimeError("renderer exploded")
        generate_certificate_pdf(data, path)

    client = make_client(generator=flaky_generator)
    recipients = [{"name": "First"}, {"name": "Boom"}, {"name": "Last"}]
    job_id = client.post("/api/v1/jobs", json=job_payload(recipients=recipients)).json()["id"]

    body = client.get(f"/api/v1/jobs/{job_id}").json()
    assert body["status"] == "completed_with_errors"
    assert body["counts"] == {"total": 3, "succeeded": 2, "failed": 1, "pending": 0}
    assert body["failures"] == [
        {"index": 1, "recipient_name": "Boom", "error": "Generation failed - renderer exploded"}
    ]

    certs = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"]
    assert [c["status"] for c in certs] == ["success", "failed", "success"]
    assert certs[1]["download_url"] is None
    # the failed certificate cannot be downloaded, the others can
    failed_id = certs[1]["id"]
    resp = client.get(f"/api/v1/jobs/{job_id}/certificates/{failed_id}/download")
    assert resp.status_code == 409
    assert "renderer exploded" in resp.json()["detail"]
    assert client.get(certs[0]["download_url"]).status_code == 200


def test_every_generation_failing_marks_job_failed(make_client):
    def always_fails(data, path):
        raise RuntimeError("nope")

    client = make_client(generator=always_fails)
    job_id = client.post("/api/v1/jobs", json=job_payload()).json()["id"]

    body = client.get(f"/api/v1/jobs/{job_id}").json()
    assert body["status"] == "failed"
    assert body["counts"]["failed"] == 3


# ----------------------------------------------------------------- retrieval
def test_list_certificates_with_filter_and_pagination(client):
    recipients = [{"name": f"Person {i}"} for i in range(5)] + [{"name": ""}]
    job_id = client.post("/api/v1/jobs", json=job_payload(recipients=recipients)).json()["id"]
    url = f"/api/v1/jobs/{job_id}/certificates"

    everything = client.get(url).json()
    assert everything["total"] == 6
    assert [c["index"] for c in everything["items"]] == [0, 1, 2, 3, 4, 5]

    ok_only = client.get(url, params={"status": "success"}).json()
    assert ok_only["total"] == 5
    failed_only = client.get(url, params={"status": "failed"}).json()
    assert failed_only["total"] == 1 and failed_only["items"][0]["index"] == 5

    page = client.get(url, params={"limit": 2, "offset": 2}).json()
    assert [c["index"] for c in page["items"]] == [2, 3]
    assert page["total"] == 6

    assert client.get(url, params={"status": "bogus"}).status_code == 422


def test_download_single_certificate_pdf(client):
    job_id = client.post("/api/v1/jobs", json=job_payload()).json()["id"]
    item = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"][0]

    resp = client.get(item["download_url"])

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")
    assert "attachment" in resp.headers["content-disposition"]
    assert ".pdf" in resp.headers["content-disposition"]


def test_download_all_certificates_as_zip(client):
    recipients = [{"name": "Asha Raman"}, {"name": ""}, {"name": "Ben Carter"}]
    job_id = client.post("/api/v1/jobs", json=job_payload(recipients=recipients)).json()["id"]

    resp = client.get(f"/api/v1/jobs/{job_id}/download")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        names = archive.namelist()
        assert len(names) == 2  # only the successful ones
        assert any("Asha-Raman" in n for n in names)
        assert all(archive.read(n).startswith(b"%PDF") for n in names)


def test_zip_of_job_without_certificates_is_404(client):
    job_id = client.post("/api/v1/jobs", json=job_payload(recipients=[{"name": ""}])).json()["id"]
    assert client.get(f"/api/v1/jobs/{job_id}/download").status_code == 404


def test_unfinished_job_cannot_be_downloaded(client):
    """Create a job but do NOT process it -> it is still queued."""
    with client.app.state.session_factory() as db:
        job = create_job(db, JobCreate(**job_payload()))
        job_id = job.id
        cert_id = db.query(Certificate).filter_by(job_id=job_id).first().id

    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "queued"
    assert client.get(f"/api/v1/jobs/{job_id}/download").status_code == 409
    resp = client.get(f"/api/v1/jobs/{job_id}/certificates/{cert_id}/download")
    assert resp.status_code == 409
    assert "not generated yet" in resp.json()["detail"]


def test_unknown_ids_return_404(client):
    job_id = client.post("/api/v1/jobs", json=job_payload()).json()["id"]
    cert_id = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"][0]["id"]
    other_job = client.post("/api/v1/jobs", json=job_payload()).json()["id"]

    assert client.get("/api/v1/jobs/does-not-exist").status_code == 404
    assert client.get("/api/v1/jobs/does-not-exist/certificates").status_code == 404
    assert client.get("/api/v1/jobs/does-not-exist/download").status_code == 404
    assert client.get(f"/api/v1/jobs/{job_id}/certificates/nope/download").status_code == 404
    # a certificate id must belong to the job in the URL
    assert client.get(f"/api/v1/jobs/{other_job}/certificates/{cert_id}/download").status_code == 404


def test_deleted_file_returns_404_instead_of_crashing(client, settings):
    job_id = client.post("/api/v1/jobs", json=job_payload()).json()["id"]
    item = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"][0]
    for pdf in settings.storage_dir.rglob(f"{item['id']}.pdf"):
        pdf.unlink()

    assert client.get(item["download_url"]).status_code == 404


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
