import logging
import os
from collections import Counter
from contextlib import asynccontextmanager
from datetime import timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app import certificates
from app.database import Base, Job, RecipientResult, utc_now
from app.schemas import JobInput, JobOutput, RecipientInput

logger = logging.getLogger(__name__)


def finish_job(job: Job) -> None:
    successes = sum(item.status == "succeeded" for item in job.recipients)
    job.status = "completed" if successes == job.total else "completed_with_errors" if successes else "failed"
    job.finished_at = utc_now()


def job_output(job: Job) -> dict:
    counts = Counter(item.status for item in job.recipients)
    return {
        "id": job.id,
        "event_name": job.event_name,
        "issuer": job.issuer,
        "issue_date": job.issue_date,
        "status": job.status,
        "total": job.total,
        "processed": counts["succeeded"] + counts["failed"],
        "succeeded": counts["succeeded"],
        "failed": counts["failed"],
        "pending": counts["pending"],
        # SQLite drops timezone information; these timestamps are always stored in UTC.
        "created_at": job.created_at.replace(tzinfo=timezone.utc),
        "finished_at": job.finished_at.replace(tzinfo=timezone.utc) if job.finished_at else None,
        "recipients": [
            {
                "id": item.id,
                "position": item.position,
                "name": item.name,
                "email": item.email,
                "status": item.status,
                "error_code": item.error_code,
                "error": item.error,
                "download_url": f"/jobs/{job.id}/certificates/{item.id}" if item.status == "succeeded" else None,
            }
            for item in job.recipients
        ],
    }


def create_app(data_dir: Path | None = None) -> FastAPI:
    storage = (data_dir or Path(os.environ.get("CERTIFICATE_DATA_DIR", "data"))).resolve()
    pdf_dir = storage / "certificates"
    engine = create_engine(
        f"sqlite:///{storage / 'certificates.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        pdf_dir.mkdir(parents=True, exist_ok=True)
        Base.metadata.create_all(engine)
        # ponytail: single server process; use worker leases before sharing this DB across processes.
        with Session(engine) as session:
            for job in session.scalars(select(Job).where(Job.status == "processing")):
                for item in job.recipients:
                    if item.status == "pending":
                        # A crash can happen after file publication but before the DB commit.
                        (pdf_dir / f"{item.id}.pdf").unlink(missing_ok=True)
                        item.status = "failed"
                        item.error_code = "processing_interrupted"
                        item.error = "Server stopped before this result was committed; submit a new job to retry."
                finish_job(job)
            session.commit()
        try:
            yield
        finally:
            engine.dispose()

    api = FastAPI(
        title="CertForge — Bulk Certificate Generator",
        version="1.0.0",
        description="Generate up to 100 certificates per synchronous request and retrieve individual results.",
        lifespan=lifespan,
    )
    api.state.engine = engine
    api.state.pdf_dir = pdf_dir

    @api.post("/jobs", status_code=201, response_model=JobOutput)
    def create_job(payload: JobInput):
        with Session(engine, expire_on_commit=False) as session:
            job = Job(
                id=str(uuid4()), event_name=payload.event_name, issuer=payload.issuer,
                issue_date=payload.issue_date, total=len(payload.recipients), status="processing",
            )
            for position, raw in enumerate(payload.recipients):
                item = RecipientResult(id=str(uuid4()), position=position, status="pending")
                try:
                    recipient = RecipientInput.model_validate(raw)
                    item.name, item.email = recipient.name, str(recipient.email)
                except ValidationError as exc:
                    item.status = "failed"
                    item.error_code = "invalid_recipient"
                    item.error = "; ".join(
                        f"{'.'.join(map(str, error['loc'])) or 'recipient'}: {error['msg']}"
                        for error in exc.errors(include_input=False, include_context=False, include_url=False)
                    )[:1000]
                job.recipients.append(item)
            session.add(job)
            session.commit()

            for item in job.recipients:
                if item.status != "pending":
                    continue
                target = pdf_dir / f"{item.id}.pdf"
                try:
                    certificates.generate_certificate(
                        target, name=item.name, email=item.email, event_name=job.event_name,
                        issuer=job.issuer, issue_date=job.issue_date.isoformat(), certificate_id=item.id,
                    )
                except Exception:
                    logger.exception("Certificate generation failed: job=%s recipient=%s", job.id, item.id)
                    try:
                        target.unlink(missing_ok=True)
                    except OSError:
                        logger.exception("Could not remove failed certificate: %s", item.id)
                    item.status = "failed"
                    item.error_code = "generation_failed"
                    item.error = "Certificate generation failed; submit a new job to retry."
                else:
                    item.status = "succeeded"
                # Persist each result; a later interruption cannot erase completed recipients.
                session.commit()

            finish_job(job)
            session.commit()
            return job_output(job)

    @api.get("/jobs/{job_id}", response_model=JobOutput)
    def get_job(job_id: UUID):
        with Session(engine) as session:
            job = session.get(Job, str(job_id))
            if job is None:
                raise HTTPException(404, "Job not found")
            return job_output(job)

    @api.get(
        "/jobs/{job_id}/certificates/{certificate_id}",
        response_class=FileResponse,
        responses={200: {"content": {"application/pdf": {}}}, 404: {"description": "Unknown job or certificate"},
                   409: {"description": "Recipient did not succeed"}, 410: {"description": "PDF missing from storage"}},
    )
    def download_certificate(job_id: UUID, certificate_id: UUID):
        with Session(engine) as session:
            item = session.scalar(select(RecipientResult).where(
                RecipientResult.id == str(certificate_id), RecipientResult.job_id == str(job_id)
            ))
            if item is None:
                raise HTTPException(404, "Certificate not found in this job")
            if item.status != "succeeded":
                raise HTTPException(409, "No successful certificate is available for this recipient")
        target = pdf_dir / f"{certificate_id}.pdf"
        if not target.is_file():
            raise HTTPException(410, "Generated PDF is missing from storage")
        return FileResponse(
            target, media_type="application/pdf", filename=f"certificate-{certificate_id}.pdf",
            headers={"Cache-Control": "no-store"},
        )

    return api


app = create_app()
