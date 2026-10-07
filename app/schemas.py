import unicodedata
from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

Name = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=120)]
EventName = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=160)]


def validate_printed_text(value: str) -> str:
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("Control and invisible formatting characters are not allowed")
    try:
        value.encode("cp1252")
    except UnicodeEncodeError:
        raise ValueError("The fixed PDF template supports Western European text only") from None
    return value


class RecipientInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    email: EmailStr = Field(max_length=254)

    _printable = field_validator("name")(validate_printed_text)

    @field_validator("email")
    @classmethod
    def printable_email(cls, value: EmailStr) -> str:
        return validate_printed_text(str(value))


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_name: EventName
    issuer: Name
    issue_date: date
    # Validate items separately so a malformed recipient does not reject valid peers.
    recipients: list[Any] = Field(min_length=1, max_length=100)

    _printable = field_validator("event_name", "issuer")(validate_printed_text)

    @field_validator("issue_date", mode="before")
    @classmethod
    def require_iso_date(cls, value: Any) -> Any:
        if isinstance(value, str) and len(value) == 10:
            try:
                if date.fromisoformat(value).isoformat() == value:
                    return value
            except ValueError:
                pass
        raise ValueError("Use a calendar date in YYYY-MM-DD format")


class RecipientOutput(BaseModel):
    id: UUID
    position: int
    name: str | None
    email: str | None
    status: Literal["pending", "succeeded", "failed"]
    error_code: str | None
    error: str | None
    download_url: str | None


class JobOutput(BaseModel):
    id: UUID
    event_name: str
    issuer: str
    issue_date: date
    status: Literal["processing", "completed", "completed_with_errors", "failed"]
    total: int
    processed: int
    succeeded: int
    failed: int
    pending: int
    created_at: datetime
    finished_at: datetime | None
    recipients: list[RecipientOutput]
