from datetime import date
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import certificates
from app.database import Job, RecipientResult
from app.main import create_app


def payload(*recipients):
    return {
        "event_name": "Python Backend Workshop",
        "issuer": "Example Academy",
        "issue_date": "2026-10-07",
        "recipients": list(recipients) or [{"name": "Abhinav Jain", "email": "abhinav@example.com"}],
    }


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as test_client:
        yield test_client


def assert_counts(job, *, total, succeeded, failed, pending=0):
    assert (job["total"], job["succeeded"], job["failed"], job["pending"]) == (total, succeeded, failed, pending)
    assert job["processed"] == succeeded + failed
    assert job["processed"] + job["pending"] == job["total"]


def test_create_job_persists_related_rows(client):
    response = client.post("/jobs", json=payload())
    assert response.status_code == 201
    job = response.json()
    assert job["status"] == "completed"
    assert job["finished_at"] is not None
    assert job["created_at"].endswith("Z")
    assert_counts(job, total=1, succeeded=1, failed=0)
    assert job["recipients"][0]["position"] == 0
    with Session(client.app.state.engine) as session:
        stored = session.get(Job, job["id"])
        assert stored is not None
        assert len(stored.recipients) == 1
        assert stored.recipients[0].job_id == job["id"]


@pytest.mark.parametrize("field,value", [
    ("recipients", []),
    ("recipients", [{}] * 101),
    ("recipients", "wrong type"),
    ("event_name", " "),
    ("event_name", "a" * 161),
    ("event_name", "event\nnewline"),
    ("event_name", "कार्यक्रम"),
    ("issuer", 123),
    ("issue_date", "2026-02-30"),
    ("issue_date", "2026-10-07T00:00:00"),
    ("issue_date", "2026-W41-3"),
    ("issue_date", 1791331200),
    ("unexpected", "value"),
])
def test_invalid_batch_is_rejected_without_creating_job(client, field, value):
    body = payload()
    body[field] = value
    assert client.post("/jobs", json=body).status_code == 422
    with Session(client.app.state.engine) as session:
        assert session.scalar(select(Job)) is None
    assert not list(client.app.state.pdf_dir.iterdir())


@pytest.mark.parametrize("invalid", [
    None, 42, "not an object", [], {},
    {"name": " ", "email": "person@example.com"},
    {"name": "Valid", "email": "invalid-email"},
    {"name": 123, "email": "person@example.com"},
    {"name": "a" * 121, "email": "person@example.com"},
    {"name": "Invisible\u200bName", "email": "person@example.com"},
    {"name": "अनन्या", "email": "person@example.com"},
    {"name": "Valid", "email": "person@example.com", "extra": True},
])
def test_invalid_recipient_does_not_reject_valid_peers(client, invalid):
    body = payload(
        {"name": "First", "email": "first@example.com"},
        invalid,
        {"name": "Last", "email": "last@example.com"},
    )
    response = client.post("/jobs", json=body)
    assert response.status_code == 201
    job = response.json()
    assert job["status"] == "completed_with_errors"
    assert_counts(job, total=3, succeeded=2, failed=1)
    invalid_result = job["recipients"][1]
    assert invalid_result["error_code"] == "invalid_recipient"
    assert invalid_result["error"]
    assert invalid_result["download_url"] is None
    assert [item["position"] for item in job["recipients"]] == [0, 1, 2]


def test_pdf_contains_recipient_and_certificate_information(client):
    body = payload({"name": "José <Example> & Co.", "email": "jose@example.com"})
    job = client.post("/jobs", json=body).json()
    item = job["recipients"][0]
    response = client.get(item["download_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-")
    pdf = PdfReader(BytesIO(response.content))
    assert len(pdf.pages) == 1
    assert float(pdf.pages[0].mediabox.width) > float(pdf.pages[0].mediabox.height)
    text = pdf.pages[0].extract_text()
    for expected in ("José <Example> & Co.", "jose@example.com", body["event_name"], body["issuer"], body["issue_date"], item["id"]):
        assert expected in text
    assert not list(client.app.state.pdf_dir.glob("*.tmp"))


def test_status_matches_creation_result(client):
    job = client.post("/jobs", json=payload()).json()
    response = client.get(f"/jobs/{job['id']}")
    assert response.status_code == 200
    assert response.json() == job


def test_processing_progress_is_committed_before_next_pdf(client, monkeypatch):
    original = certificates.generate_certificate
    snapshots = []

    def observe_progress(target, **kwargs):
        with Session(client.app.state.engine) as session:
            job_id = session.scalar(select(Job.id))
        snapshots.append(client.get(f"/jobs/{job_id}").json())
        original(target, **kwargs)

    monkeypatch.setattr(certificates, "generate_certificate", observe_progress)
    response = client.post("/jobs", json=payload(
        {"name": "First", "email": "first@example.com"},
        {"name": "Second", "email": "second@example.com"},
    ))
    assert response.status_code == 201
    assert snapshots[0]["status"] == snapshots[1]["status"] == "processing"
    assert snapshots[0]["finished_at"] is None
    assert_counts(snapshots[0], total=2, succeeded=0, failed=0, pending=2)
    assert_counts(snapshots[1], total=2, succeeded=1, failed=0, pending=1)


def test_individual_generation_failure_is_recorded_and_processing_continues(client, monkeypatch):
    original = certificates.generate_certificate

    def fail_one(target, **kwargs):
        if kwargs["name"] == "Broken":
            target.write_bytes(b"partial PDF")
            raise RuntimeError("internal server path and implementation details")
        original(target, **kwargs)

    monkeypatch.setattr(certificates, "generate_certificate", fail_one)
    response = client.post("/jobs", json=payload(
        {"name": "First", "email": "first@example.com"},
        {"name": "Broken", "email": "broken@example.com"},
        {"name": "Last", "email": "last@example.com"},
    ))
    assert response.status_code == 201
    job = response.json()
    assert job["status"] == "completed_with_errors"
    assert_counts(job, total=3, succeeded=2, failed=1)
    failed = job["recipients"][1]
    assert failed["error_code"] == "generation_failed"
    assert "internal server path" not in failed["error"]
    assert not (client.app.state.pdf_dir / f"{failed['id']}.pdf").exists()
    assert client.get(job["recipients"][2]["download_url"]).status_code == 200
    assert client.get(f"/jobs/{job['id']}/certificates/{failed['id']}").status_code == 409


def test_all_invalid_recipients_produce_failed_job(client):
    job = client.post("/jobs", json=payload({}, None)).json()
    assert job["status"] == "failed"
    assert job["finished_at"] is not None
    assert_counts(job, total=2, succeeded=0, failed=2)


def test_all_generation_failures_produce_failed_job(client, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk is full")

    monkeypatch.setattr(certificates, "generate_certificate", fail)
    job = client.post("/jobs", json=payload()).json()
    assert job["status"] == "failed"
    assert_counts(job, total=1, succeeded=0, failed=1)
    assert job["recipients"][0]["error_code"] == "generation_failed"


def test_renderer_failure_removes_temporary_file_and_continues(client, monkeypatch):
    original_save = certificates.Canvas.save
    calls = 0

    def fail_first_save(canvas):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated PDF write failure")
        original_save(canvas)

    monkeypatch.setattr(certificates.Canvas, "save", fail_first_save)
    job = client.post("/jobs", json=payload(
        {"name": "First", "email": "first@example.com"},
        {"name": "Second", "email": "second@example.com"},
    )).json()
    assert_counts(job, total=2, succeeded=1, failed=1)
    assert not list(client.app.state.pdf_dir.glob("*.tmp"))
    assert len(list(client.app.state.pdf_dir.glob("*.pdf"))) == 1
    assert client.get(job["recipients"][1]["download_url"]).status_code == 200


def test_unknown_ids_and_cross_job_downloads(client):
    first = client.post("/jobs", json=payload()).json()
    second = client.post("/jobs", json=payload()).json()
    assert client.get(f"/jobs/{uuid4()}").status_code == 404
    assert client.get("/jobs/not-a-uuid").status_code == 422
    assert client.get(f"/jobs/{first['id']}/certificates/{uuid4()}").status_code == 404
    assert client.get(f"/jobs/{second['id']}/certificates/{first['recipients'][0]['id']}").status_code == 404


def test_missing_generated_file_returns_gone(client):
    job = client.post("/jobs", json=payload()).json()
    item = job["recipients"][0]
    (client.app.state.pdf_dir / f"{item['id']}.pdf").unlink()
    assert client.get(item["download_url"]).status_code == 410


def test_maximum_batch_and_duplicate_recipients_remain_distinct(client):
    body = payload(*[{"name": "Same Name", "email": "same@example.com"}] * 100)
    response = client.post("/jobs", json=body)
    assert response.status_code == 201
    job = response.json()
    assert_counts(job, total=100, succeeded=100, failed=0)
    assert len({item["id"] for item in job["recipients"]}) == 100
    assert len(list(client.app.state.pdf_dir.glob("*.pdf"))) == 100


def test_restart_preserves_results_and_finalizes_interrupted_job(tmp_path):
    application = create_app(tmp_path)
    with TestClient(application) as first_client:
        completed = first_client.post("/jobs", json=payload()).json()
        job_id, succeeded_id, pending_id = str(uuid4()), str(uuid4()), str(uuid4())
        with Session(application.state.engine) as session:
            job = Job(id=job_id, event_name="Interrupted Event", issuer="Example Academy",
                      issue_date=date(2026, 10, 7), total=2, status="processing")
            job.recipients = [
                RecipientResult(id=succeeded_id, position=0, name="Saved", email="saved@example.com", status="succeeded"),
                RecipientResult(id=pending_id, position=1, name="Pending", email="pending@example.com", status="pending"),
            ]
            session.add(job)
            session.commit()
        certificates.generate_certificate(
            application.state.pdf_dir / f"{succeeded_id}.pdf", name="Saved", email="saved@example.com",
            event_name="Interrupted Event", issuer="Example Academy", issue_date="2026-10-07", certificate_id=succeeded_id,
        )
        (application.state.pdf_dir / f"{pending_id}.pdf").write_bytes(b"uncommitted file")

    with TestClient(create_app(tmp_path)) as restarted:
        assert restarted.get(completed["recipients"][0]["download_url"]).status_code == 200
        recovered = restarted.get(f"/jobs/{job_id}").json()
        assert recovered["status"] == "completed_with_errors"
        assert recovered["finished_at"] is not None
        assert_counts(recovered, total=2, succeeded=1, failed=1)
        assert recovered["recipients"][1]["error_code"] == "processing_interrupted"
        assert not (restarted.app.state.pdf_dir / f"{pending_id}.pdf").exists()
        assert restarted.get(recovered["recipients"][0]["download_url"]).status_code == 200
