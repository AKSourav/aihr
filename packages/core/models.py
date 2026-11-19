import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from core.db import Base


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def uuid_col() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True))


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (UniqueConstraint("account_id", "name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    keycloak_sub: Mapped[str] = mapped_column(String, unique=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    account_role: Mapped[str] = mapped_column(String, default="member")
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "org_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    role: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("id", "org_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    title: Mapped[str] = mapped_column(String)
    rubric: Mapped[list] = mapped_column(JSON)
    question_plan: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("id", "org_id"),
        UniqueConstraint("org_id", "email"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    first_name: Mapped[str] = mapped_column(String)
    last_name: Mapped[str] = mapped_column(String)
    email: Mapped[str] = mapped_column(String)
    phone_e164: Mapped[str | None] = mapped_column(String, nullable=True)
    consent_ai_call_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_recording_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[uuid.UUID] = uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id"), unique=True)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("id", "org_id"),
        ForeignKeyConstraint(["candidate_id", "org_id"], ["candidates.id", "candidates.org_id"]),
        ForeignKeyConstraint(["job_id", "org_id"], ["jobs.id", "jobs.org_id"]),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    candidate_id: Mapped[uuid.UUID] = uuid_col()
    job_id: Mapped[uuid.UUID] = uuid_col()
    stage: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    candidate: Mapped["Candidate"] = relationship(lazy="joined", viewonly=True)
    job: Mapped["Job"] = relationship(lazy="joined", viewonly=True)


class Score(Base):
    __tablename__ = "scores"
    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "org_id"], ["applications.id", "applications.org_id"]
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    application_id: Mapped[uuid.UUID] = uuid_col()
    stage: Mapped[str] = mapped_column(String)
    breakdown: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (
        UniqueConstraint("id", "org_id"),
        ForeignKeyConstraint(
            ["application_id", "org_id"], ["applications.id", "applications.org_id"]
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    application_id: Mapped[uuid.UUID] = uuid_col()
    attempt_no: Mapped[int] = mapped_column(default=1)
    call_mode: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    room_name: Mapped[str | None] = mapped_column(String, nullable=True)
    transcript_url: Mapped[str | None] = mapped_column(String, nullable=True)
    callback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    application: Mapped["Application"] = relationship(lazy="joined", viewonly=True)


class CallSummary(Base):
    __tablename__ = "call_summaries"
    __table_args__ = (ForeignKeyConstraint(["call_id", "org_id"], ["calls.id", "calls.org_id"]),)

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    call_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), unique=True)
    summary: Mapped[str] = mapped_column(Text)
    rubric_scores: Mapped[list] = mapped_column(JSON)
    facts: Mapped[dict] = mapped_column(JSON)
    red_flags: Mapped[list] = mapped_column(JSON)
    candidate_questions: Mapped[list] = mapped_column(JSON)
    confidence: Mapped[str] = mapped_column(String)
    model_version: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    __table_args__ = (ForeignKeyConstraint(["call_id", "org_id"], ["calls.id", "calls.org_id"]),)

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    call_id: Mapped[uuid.UUID] = uuid_col()
    model: Mapped[str] = mapped_column(String)
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BreakGlassScope(Base):
    __tablename__ = "break_glass_scopes"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    entity_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
