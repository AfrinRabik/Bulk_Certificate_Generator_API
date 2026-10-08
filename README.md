# Bulk Certificate Generator

A backend API that accepts **one request containing many recipients**, generates a PDF certificate
for each valid recipient from a single predefined template, tracks progress, and lets the client
download the results (individually or as one ZIP).

* **Python 3.10+**, **FastAPI**, **SQLAlchemy 2** (SQLite by default, any SQL database via `DATABASE_URL`)
* **reportlab** draws the certificate PDF
* **pytest** test-suite (25 tests)

---

## 1. Setup

```bash
git clone <this repo> && cd bulk-certificate-generator      # or unzip it
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt  # use requirements.txt for runtime only
```

## 2. Run the application

```bash
uvicorn app.main:app --reload
```

* API: <http://127.0.0.1:8000>
* Interactive docs (Swagger UI): <http://127.0.0.1:8000/docs>
* The SQLite database (`certificates.db`) and PDFs (`storage/`) are created in the working directory.

Optional environment variables:

| Variable          | Default                          | Meaning                                         |
|-------------------|----------------------------------|-------------------------------------------------|
| `DATABASE_URL`    | `sqlite:///./certificates.db`    | Any SQLAlchemy URL, e.g. `postgresql+psycopg://user:pw@host/db` (install the driver yourself) |
| `STORAGE_DIR`     | `./storage`                      | Where generated PDFs are stored                 |
| `MAX_RECIPIENTS`  | `1000`                           | Max recipients accepted in one request          |

## 3. Run the tests

```bash
pytest
```

Every test uses its own temporary database and storage folder, so tests never touch real data.

| File                              | Covers                                                                              |
|-----------------------------------|-------------------------------------------------------------------------------------|
| `tests/test_jobs_api.py`          | creating a job, input validation, status/progress, individual failure, retrieval (single PDF, list, ZIP), 404/409 cases |
| `tests/test_certificate_generator.py` | the PDF really contains the recipient data, long names, unsupported characters, no half-written files |
| `tests/test_processing.py`        | progress visible *while* running, idempotent processing, resuming an interrupted job |

---

## 4. Using the API

### Submit a generation request

`POST /api/v1/jobs` → **202 Accepted** (the work continues in the background)

```bash
curl -X POST http://127.0.0.1:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
        "event_name": "Python Bootcamp 2026",
        "issued_by": "Acme Academy",
        "issue_date": "2026-10-08",
        "recipients": [
          {"name": "Asha Raman",  "email": "asha@example.com"},
          {"name": "Ben Carter",  "email": "ben@example.com", "achievement": "With distinction"},
          {"name": "",            "email": "broken@example.com"}
        ]
      }'
```

| Field                      | Required | Notes                                                              |
|----------------------------|----------|--------------------------------------------------------------------|
| `event_name`               | yes      | 1-100 chars, printed on every certificate                          |
| `issued_by`                | yes      | 1-100 chars                                                        |
| `issue_date`               | no       | `YYYY-MM-DD`, defaults to today                                    |
| `recipients[].name`        | yes      | 1-100 chars                                                        |
| `recipients[].email`       | no       | validated if present (stored, not printed)                         |
| `recipients[].achievement` | no       | up to 200 chars, printed under the event name                      |

Response (shortened):

```json
{
  "id": "74b428fe-ffb4-48d3-aae8-3639b3541f24",
  "status": "queued",
  "counts": {"total": 3, "succeeded": 0, "failed": 1, "pending": 2},
  "progress_percent": 33.3,
  "links": {"self": ".../api/v1/jobs/74b4...", "certificates": ".../certificates", "download_all": ".../download"}
}
```

The `Location` header also points to the job.

### Check progress / result

`GET /api/v1/jobs/{job_id}`

```json
{
  "id": "74b428fe-...",
  "status": "completed_with_errors",
  "counts": {"total": 3, "succeeded": 2, "failed": 1, "pending": 0},
  "progress_percent": 100.0,
  "failures": [
    {"index": 2, "recipient_name": null,
     "error": "Validation failed - name: String should have at least 1 character"}
  ]
}
```

Job statuses: `queued` → `processing` → `completed` | `completed_with_errors` | `failed`
(`failed` = not a single certificate could be generated). `failures[].index` is the 0-based position
of the recipient in your request.

### Retrieve the certificates

| What                          | Request                                                                  |
|-------------------------------|--------------------------------------------------------------------------|
| List (paginated, filterable)  | `GET /api/v1/jobs/{job_id}/certificates?status=success&limit=100&offset=0` |
| One certificate (PDF)         | `GET /api/v1/jobs/{job_id}/certificates/{certificate_id}/download` (the `download_url` from the list) |
| Everything as a ZIP           | `GET /api/v1/jobs/{job_id}/download`                                     |

```bash
curl -OJ http://127.0.0.1:8000/api/v1/jobs/<job_id>/download     # saves certificates_<job_id>.zip
```

### HTTP status codes

| Code | When                                                                                  |
|------|----------------------------------------------------------------------------------------|
| 202  | job accepted                                                                           |
| 422  | the *request itself* is invalid (missing field, empty list, too many recipients, bad query parameter) |
| 404  | unknown job / certificate, or nothing to download                                      |
| 409  | the certificate/job is not ready yet, or that certificate failed (the body says why)   |

---

## 5. How it works

```
POST /jobs ─► validate request shape ─► create_job(): one DB row per recipient
                                         (bad recipients are stored as "failed" right away)
          ◄─ 202 + job id
                      │
                      └─► background task: process_job()
                            for every pending certificate:
                               try: render PDF → mark success
                               except: mark failed + error   (commit after each one)
                            set final job status

GET /jobs/{id} ─► counts are computed from the certificate rows
```

```
app/
  factory.py          create_app(): wires settings, DB, generator, routes
  main.py             `app = create_app()` for uvicorn
  config.py           Settings (env vars)
  database.py         engine + session factory
  models.py           GenerationJob, Certificate
  schemas.py          request/response models + per-recipient validation
  routers/jobs.py     HTTP endpoints
  services/jobs.py    create_job(), process_job(), get_counts()
  services/certificate.py   the single PDF template (reportlab)
tests/
```

## 6. Important design decisions

**Processing: asynchronous, in-process background task.**
A request may contain hundreds or thousands of recipients, so generating inside the request would
mean long HTTP calls, client time-outs and no progress information. Instead `POST /jobs` stores the
job and returns `202` immediately; FastAPI's `BackgroundTasks` runs `process_job()` in a worker
thread, and the client polls `GET /jobs/{id}`.
*Why not Celery/RQ?* It needs a broker (Redis/RabbitMQ) and extra processes, which is unnecessary for
this scope. The design keeps that door open: `process_job()` is a plain function that only receives
IDs and reads everything from the database, so moving it into a Celery task is a small change and
the API contract stays the same. *Trade-off:* if the server dies mid-job, the background task is lost
(see "Limitations").

**Two levels of validation.**
1. *Request level* (HTTP 422): missing/empty `event_name`, `issued_by`, empty `recipients`, too many
   recipients. Nothing can be processed, so the request is rejected.
2. *Recipient level*: every recipient is validated on its own. An invalid one (blank name, bad email,
   not an object, text the font cannot print) becomes a `failed` certificate with a readable error,
   while the valid recipients are still generated. A single typo in a 1000-row list should not
   cost the client the whole batch.

**Failure isolation.** Each certificate is generated inside its own `try/except` and committed on its
own. A crash for one recipient is recorded on that row and the loop continues. The final job status
tells the client what happened: `completed`, `completed_with_errors` or `failed`.

**One row per recipient** (`certificates` table, including invalid ones) is what makes
`failures[]` and the per-certificate list possible, and gives each recipient a stable `index`.

**Counts are computed, not stored.** Succeeded/failed/pending are `GROUP BY` queries on the
certificate rows, so they can never drift out of sync with the real data (an index on
`(job_id, status)` keeps it cheap).

**Live progress.** Because every certificate is committed individually, `GET /jobs/{id}` shows
accurate progress while the job is running.

**Idempotent processing.** `process_job()` only touches `pending` certificates and skips finished
jobs, so calling it again after a crash resumes the work without regenerating existing PDFs.

**Safe files.** PDFs are written to a temporary name and renamed when complete; a failed
certificate never leaves a half-written file. Stored paths are relative to `STORAGE_DIR` and checked
against it when serving (no path traversal).

**Application factory + injectable generator.** `create_app(settings, generator)` makes tests
independent (own DB and folder) and allows a fake/failing generator to test failure handling.

**Database.** SQLAlchemy 2 with SQLite by default (zero setup). Because it uses plain SQLAlchemy,
PostgreSQL/MySQL work by changing `DATABASE_URL`. Tables are created at start-up with
`create_all`; a real deployment would use Alembic migrations.

## 7. Limitations / what I would do next

* **Jobs are lost if the process stops while running**: they stay `processing`. Because processing is
  idempotent, a start-up hook (or a Celery worker with retries) could resume unfinished jobs.
* **Single process**: for horizontal scale, move to a task queue + several workers and shared storage (S3).
* **SQLite** allows one writer at a time; use PostgreSQL when several jobs run concurrently.
* **Fonts**: the template uses the built-in PDF font, so only Latin text (Windows-1252, e.g. `José`)
  can be printed. Other scripts are reported as a per-recipient validation error. Supporting them means
  registering a Unicode `.ttf` font with reportlab.
* No authentication, rate limiting, retry-failed endpoint, duplicate detection or e-mailing of
  certificates (the recipient e-mail is stored for that purpose).
