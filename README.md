# CertForge

A Python API for generating a batch of PDF certificates from one fixed template. Built with FastAPI, SQLAlchemy, SQLite, and ReportLab. Each recipient has an independent result: bad input or a rendering failure does not stop valid recipients.

## Setup and run

Requires Python 3.12 or newer; tested with Python 3.12.10. Clone the repository and run commands from its root. `requirements-lock.txt` pins the full tested environment; `requirements-dev.txt` allows compatible version updates.

```sh
git clone https://github.com/Abhinav-0311/CertForge.git
cd CertForge
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Linux/macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

For application dependencies alone, install `requirements.txt`. Interactive API documentation is available at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs). The database and PDFs are created under `data/` on startup. Set `CERTIFICATE_DATA_DIR` to override that location. Do not run multiple server processes against the same data directory; startup recovery assumes that any unfinished work belongs to a stopped process.

## Submit a batch

`POST /jobs` accepts common certificate information and 1–100 recipients:

```json
{
  "event_name": "Python Backend Workshop",
  "issuer": "Example Academy",
  "issue_date": "2026-10-07",
  "recipients": [
    {"name": "Abhinav Jain", "email": "abhinav@example.com"},
    {"name": "Grace Hopper", "email": "grace@example.com"},
    {"name": "Invalid recipient", "email": "not-an-email"}
  ]
}
```

PowerShell:

```powershell
$job = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/jobs -ContentType 'application/json' -Body (Get-Content -Raw examples/job.json)
$job | ConvertTo-Json -Depth 8
```

Alternatively, with curl (`curl.exe` on Windows):

```sh
curl -X POST http://127.0.0.1:8000/jobs -H "Content-Type: application/json" --data-binary @examples/job.json
```

The response is HTTP **201** because the job resource was created, including when some or all recipients fail. This example returns `completed_with_errors`, `total: 3`, `processed: 3`, `succeeded: 2`, `failed: 1`, and `pending: 0`. Each result includes its UUID, zero-based input position, validated name/email, status, error code/message, and a relative download URL on success. Invalid recipients have null name/email; their position identifies the corresponding original input. No email is sent.

Request-level errors (missing certificate information, invalid date, empty/oversized batch, unknown top-level fields) return **422** and create no job. A recipient's missing/invalid fields, unknown keys, wrong types, or malformed email create an `invalid_recipient` result and allow the rest of the batch to proceed.

Printed names and issuer are limited to 120 characters, and event names to 160; surrounding whitespace is trimmed. Control/invisible formatting characters are rejected. The template uses PDF's built-in fonts, so printed text supports Western European characters (Windows-1252), including accented Latin names. Unsupported scripts are explicitly rejected rather than rendered with missing glyphs. Broader script support would require an embedded font and, for scripts such as Devanagari, a shaping-capable renderer. Emails are validated and normalized by Pydantic's `EmailStr` and must also be renderable. Dates must be valid calendar dates in `YYYY-MM-DD` form. Repeated recipients are allowed and receive separate certificates.

## Inspect results and retrieve a PDF

`GET /jobs/{job_id}` returns the persisted job and ordered recipient results.

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/jobs/$($job.id)"
$certificate = $job.recipients | Where-Object status -eq 'succeeded' | Select-Object -First 1
Invoke-WebRequest "http://127.0.0.1:8000$($certificate.download_url)" -OutFile certificate.pdf
```

Or replace the returned IDs in these curl commands:

```sh
curl http://127.0.0.1:8000/jobs/JOB_UUID
curl http://127.0.0.1:8000/jobs/JOB_UUID/certificates/CERTIFICATE_UUID --output certificate.pdf
```

The download response is `application/pdf` with an attachment filename. Unknown job/certificate IDs and a certificate requested under a different job return **404**; malformed UUIDs return **422**; failed/pending recipients return **409**; a previously generated file missing from storage returns **410**. UUIDs determine storage paths; recipient text never becomes a path.

## Job semantics and processing choice

| Status | Meaning |
| --- | --- |
| `processing` | At least some recipient processing may still be pending. |
| `completed` | Every recipient succeeded. |
| `completed_with_errors` | Processing ended with both successes and failures. |
| `failed` | Processing ended with no successes. |

For every response, `processed = succeeded + failed` and `total = processed + pending`. A finished job has `pending = 0` and a UTC `finished_at` timestamp. Counts are derived from the recipient rows rather than separately maintained counters that could drift.

Generation is **synchronous**, capped at 100 recipients. The POST response contains the final result, and the client keeps the connection open while it runs. FastAPI runs the synchronous handler in its thread pool. Each outcome is committed before processing the next recipient; a status read sees committed progress if its job ID is known. Since a fresh job's ID is returned only after generation ends, this API's normal flow uses GET for retrieving results, not live polling of a newly submitted job.

This choice makes the required workflow reproducible without a broker or background worker. It is appropriate for an assignment and modest batches, not arbitrarily large or concurrent workloads. Rendering time, sequential commits, SQLite write contention, and client/proxy timeouts limit throughput. Move to a durable queue with worker ownership and return HTTP 202 before generation if larger workloads are required; then clients can poll using the immediately returned job ID.

Jobs and recipients form a one-to-many relationship with a foreign key and unique input position. A recipient transitions from `pending` to `succeeded` or `failed`. Validation errors are persisted before rendering starts. Rendering exceptions are logged server-side and exposed as a generic `generation_failed` message, preserving the remaining batch.

PDFs are written to a temporary file in the output directory and atomically renamed after completion. Filesystem publication and a database commit cannot form one shared transaction. If the process stops between them, single-process startup recovery removes PDFs belonging to pending results, marks them `processing_interrupted`, preserves committed successes, and finalizes the job. No automatic replay occurs. A hard termination may leave an unused `.tmp` file; it is never served.

There is no automatic deduplication or request retry: resubmitting creates a new job. A dropped response can therefore leave a completed job whose ID the client did not receive. An idempotency key is the next change if retry-safe submission becomes a requirement. A database/storage-wide outage can still abort a request; recipient isolation does not claim to recover from an unusable database.

The API has no authentication because it is a local assignment service. Bind to localhost. Before public deployment, add authentication/ownership checks, request size/rate limits, storage retention, and backups of both database and PDFs. A UUID is an identifier, not access control. There is no frontend, template editor, email delivery, Redis, or Celery.

## Run tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

On Linux/macOS, use `.venv/bin/python -m pytest -q`.

If an existing Windows global pytest temp directory raises `WinError 5`, use a fresh local directory:

```powershell
New-Item -ItemType Directory -Path .test-artifacts -Force | Out-Null
$testTemp = Join-Path (Get-Location) ('.test-artifacts/' + [guid]::NewGuid().ToString())
.\.venv\Scripts\python.exe -m pytest -q --basetemp $testTemp
```

The locked Starlette version emits a deprecation warning about its HTTPX test-client adapter; it does not affect test results.

Tests use temporary databases and output directories and do not touch application data. They cover all six required areas: job creation, validation, PDF generation (parsed PDF text and page structure), job status/progress, isolated generation failures, and file retrieval. Additional cases cover all-failed jobs, the maximum batch, duplicate recipients, cross-job lookup, missing files, and restart recovery.

## Code map

- `app/main.py`: HTTP endpoints, synchronous processing, startup recovery, result serialization.
- `app/database.py`: the two relational models and database constraints.
- `app/schemas.py`: request/response contracts and validation.
- `app/certificates.py`: the single PDF template and atomic file publication.
- `tests/test_api.py`: API, database, PDF, failure, and restart checks.
- `examples/job.json`: a batch with two valid recipients and one invalid recipient.

## Assignment source and submission

Implements the required functionality in the [Bulk Certificate Generator brief](https://ausindia-my.sharepoint.com/:w:/g/personal/krish_maheshwari_aereo_io/IQBamHCzaXogS5ZgDMwZPNQWAd3xUgYgM0ZBIlXqZxbs8lA?e=Mxgq36). Optional functionality has been limited to consistency and retrieval edge cases.

Submission repository: [Abhinav-0311/CertForge](https://github.com/Abhinav-0311/CertForge). In the form, select **Bulk Certificate Generator API** and provide this public repository URL, your name, email, and an accessible resume link. `.venv/`, application data, and generated test artifacts are excluded from Git. Form submission is a separate step; it does not happen when running this API.
