from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Application, AuditEvent, Candidate, Job
from core.permissions import Permission
from core.resumes import rejection, store_resume
from core.schemas import EMAIL_RE
from services.api.authz import TenantContext, require
from services.api.routes.resumes import start_extraction
from services.api.routes.tenant import ApplicationOut, _as_application

router = APIRouter(prefix="/tenants/{org_id}", tags=["candidates"])

MAX_IMPORT_ROWS = 500
RowStatus = Literal["created", "matched", "duplicate", "invalid"]


class CandidateOut(BaseModel):
    candidate_id: UUID
    created: bool
    application_id: UUID | None
    resume_id: UUID


class ApplicationRequest(BaseModel):
    candidate_id: UUID
    job_id: UUID


class ImportRow(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone_e164: str | None = None


class ImportRequest(BaseModel):
    job_id: UUID
    rows: list[ImportRow] = Field(max_length=MAX_IMPORT_ROWS)


class ImportResult(BaseModel):
    row: int
    email: str
    status: RowStatus
    reason: str | None = None
    candidate_id: UUID | None = None
    application_id: UUID | None = None


class ImportOut(BaseModel):
    job_id: UUID
    created: int
    matched: int
    duplicates: int
    invalid: int
    results: list[ImportResult]


async def _job_in_org(session: AsyncSession, org_id: UUID, job_id: UUID) -> Job:
    job = await session.scalar(select(Job).where(Job.id == job_id, Job.org_id == org_id))
    if job is None:
        raise HTTPException(status_code=404, detail="not found")
    return job


async def _candidate_by_email(
    session: AsyncSession, org_id: UUID, email: str
) -> Candidate | None:
    return await session.scalar(
        select(Candidate).where(Candidate.org_id == org_id, Candidate.email == email)
    )


async def _application_for(
    session: AsyncSession, org_id: UUID, candidate_id: UUID, job_id: UUID
) -> Application | None:
    return await session.scalar(
        select(Application).where(
            Application.org_id == org_id,
            Application.candidate_id == candidate_id,
            Application.job_id == job_id,
        )
    )


def _rejection(first_name: str, last_name: str, email: str) -> str | None:
    if not first_name or not last_name:
        return "first_name and last_name are required"
    if len(email) > 320 or EMAIL_RE.match(email) is None:
        return "email is not a valid address"
    return None


async def _import_row(
    session: AsyncSession,
    org_id: UUID,
    job_id: UUID,
    row: int,
    first_name: str,
    last_name: str,
    email: str,
    phone: str | None,
) -> ImportResult:
    candidate = await _candidate_by_email(session, org_id, email)
    created = candidate is None
    if candidate is None:
        candidate = Candidate(
            org_id=org_id,
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone_e164=phone,
        )
        session.add(candidate)
        await session.flush()
    elif candidate.phone_e164 is None and phone is not None:
        candidate.phone_e164 = phone

    existing = await _application_for(session, org_id, candidate.id, job_id)
    if existing is not None:
        return ImportResult(
            row=row,
            email=email,
            status="duplicate",
            candidate_id=candidate.id,
            application_id=existing.id,
        )

    application = Application(
        org_id=org_id, candidate_id=candidate.id, job_id=job_id, stage="shortlisted"
    )
    session.add(application)
    await session.flush()

    return ImportResult(
        row=row,
        email=email,
        status="created" if created else "matched",
        candidate_id=candidate.id,
        application_id=application.id,
    )


@router.post("/candidates", response_model=CandidateOut, status_code=201)
async def add_candidate(
    file: UploadFile = File(...),
    first_name: str = Form(..., max_length=100),
    last_name: str = Form(..., max_length=100),
    email: str = Form(..., max_length=320),
    phone_e164: str | None = Form(default=None, max_length=20),
    job_id: UUID | None = Form(default=None),
    context: TenantContext = Depends(require(Permission.CANDIDATE_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> CandidateOut:
    first_name = first_name.strip()
    last_name = last_name.strip()
    email = email.strip().lower()
    phone = (phone_e164 or "").strip() or None

    reason = _rejection(first_name, last_name, email)
    if reason is not None:
        raise HTTPException(status_code=422, detail=reason)

    data = await file.read()
    file_reason = rejection(data)
    if file_reason is not None:
        raise HTTPException(status_code=422, detail=file_reason)

    job = None
    if job_id is not None:
        job = await _job_in_org(session, context.org_id, job_id)

    candidate = await _candidate_by_email(session, context.org_id, email)
    created = candidate is None
    if candidate is None:
        candidate = Candidate(
            org_id=context.org_id,
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone_e164=phone,
        )
        session.add(candidate)
        await session.flush()
    elif candidate.phone_e164 is None and phone is not None:
        candidate.phone_e164 = phone

    application = None
    if job is not None:
        application = await _application_for(session, context.org_id, candidate.id, job.id)
        if application is None:
            application = Application(
                org_id=context.org_id,
                candidate_id=candidate.id,
                job_id=job.id,
                stage="shortlisted",
            )
            session.add(application)
            await session.flush()

    resume = await store_resume(
        session,
        context.org_id,
        data,
        file.filename,
        candidate_id=candidate.id,
        job_id=job.id if job is not None else None,
    )
    await session.commit()
    await start_extraction([resume.id])

    return CandidateOut(
        candidate_id=candidate.id,
        created=created,
        application_id=application.id if application is not None else None,
        resume_id=resume.id,
    )


@router.post("/applications", response_model=ApplicationOut, status_code=201)
async def create_application(
    body: ApplicationRequest,
    context: TenantContext = Depends(require(Permission.CANDIDATE_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> ApplicationOut:
    candidate = await session.scalar(
        select(Candidate).where(
            Candidate.id == body.candidate_id, Candidate.org_id == context.org_id
        )
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="not found")
    job = await _job_in_org(session, context.org_id, body.job_id)

    if await _application_for(session, context.org_id, candidate.id, job.id) is not None:
        raise HTTPException(
            status_code=409, detail="an application already exists for this candidate and job"
        )

    application = Application(
        org_id=context.org_id, candidate_id=candidate.id, job_id=job.id, stage="shortlisted"
    )
    session.add(application)
    await session.commit()
    await session.refresh(application)

    return _as_application(application, None)


@router.post("/candidates/import", response_model=ImportOut)
async def import_candidates(
    body: ImportRequest,
    context: TenantContext = Depends(require(Permission.CANDIDATE_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> ImportOut:
    job = await _job_in_org(session, context.org_id, body.job_id)

    results: list[ImportResult] = []
    for index, row in enumerate(body.rows, start=1):
        first_name = row.first_name.strip()
        last_name = row.last_name.strip()
        email = row.email.strip().lower()
        phone = (row.phone_e164 or "").strip() or None

        reason = _rejection(first_name, last_name, email)
        if reason is not None:
            results.append(ImportResult(row=index, email=email, status="invalid", reason=reason))
            continue

        try:
            async with session.begin_nested():
                result = await _import_row(
                    session,
                    context.org_id,
                    job.id,
                    index,
                    first_name,
                    last_name,
                    email,
                    phone,
                )
        except IntegrityError:
            result = ImportResult(
                row=index, email=email, status="duplicate", reason="already imported"
            )
        results.append(result)

    counts = {
        status: sum(1 for result in results if result.status == status)
        for status in ("created", "matched", "duplicate", "invalid")
    }
    session.add(
        AuditEvent(
            org_id=context.org_id,
            actor_user_id=context.user.id,
            entity_type="job",
            entity_id=job.id,
            event_type="candidates_imported",
            payload={"rows": len(body.rows), **counts},
        )
    )
    await session.commit()

    return ImportOut(
        job_id=job.id,
        created=counts["created"],
        matched=counts["matched"],
        duplicates=counts["duplicate"],
        invalid=counts["invalid"],
        results=results,
    )
