from datetime import date, datetime, timezone

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("status IN ('processing', 'completed', 'completed_with_errors', 'failed')"),
        CheckConstraint("total BETWEEN 1 AND 100"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_name: Mapped[str] = mapped_column(String(160))
    issuer: Mapped[str] = mapped_column(String(120))
    issue_date: Mapped[date] = mapped_column(Date)
    total: Mapped[int]
    status: Mapped[str] = mapped_column(String(24), default="processing")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recipients: Mapped[list["RecipientResult"]] = relationship(
        back_populates="job", order_by="RecipientResult.position", cascade="all, delete-orphan"
    )


class RecipientResult(Base):
    __tablename__ = "recipient_results"
    __table_args__ = (
        UniqueConstraint("job_id", "position"),
        CheckConstraint("position >= 0"),
        CheckConstraint("status IN ('pending', 'succeeded', 'failed')"),
        CheckConstraint(
            "(status = 'failed' AND error_code IS NOT NULL AND error IS NOT NULL) OR "
            "(status != 'failed' AND error_code IS NULL AND error IS NULL)"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    position: Mapped[int]
    name: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(254))
    status: Mapped[str] = mapped_column(String(12), default="pending")
    error_code: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(String(1000))
    job: Mapped[Job] = relationship(back_populates="recipients")
