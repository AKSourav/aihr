import asyncio
import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.exceptions import WorkflowAlreadyStartedError

from core.db import get_session
from core.models import Candidate, Job, Resume
from core.permissions import Permission
from core.resumes import (
    EXTRACTING,
    MAX_RESUME_FILES,
    current_resume,
    rejection,
    store_resume,
)
from core.settings import get_settings
from core.storage import Storage
from services.api.authz import TenantContext, require
from services.api.temporal import TASK_QUEUE, resume_workflow_id, temporal_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenants/{org_id}", tags=["resumes"])


class ResumeUploadOut(BaseModel):
    resume_id: UUID | None
    filename: str
    status: str
    reason: str | None = None


class BulkResumeOut(BaseModel):
    job_id: UUID
    accepted: int
    rejected: int
    resumes: list[ResumeUploadOut]


class ResumeOut(BaseModel):
    resume_id: UUID
    candidate_id: UUID | None
    job_id: UUID | None
    filename: str
    byte_size: int
    status: str
    error_detail: str | None
    data: dict | None
    text: str | None
    created_at: datetime


class JobResumeOut(BaseModel):
    resume_id: UUID
    candidate_id: UUID | None
    filename: str
    status: str
    error_detail: str | None
    created_at: datetime


async def start_extraction(resume_ids: list[UUID]) -> None:
    if not resume_ids:
        return
    client = await temporal_client()
    for resume_id in resume_ids:
        try:
            await client.start_workflow(
                "ExtractResumeWorkflow",
                {"resume_id": str(resume_id)},
                id=resume_workflow_id(resume_id),
                task_queue=TASK_QUEUE,
            )
        except WorkflowAlreadyStartedError:
            continue


async def job_in_org(session: AsyncSession, org_id: UUID, job_id: UUID) -> Job:
    job = await session.scalar(select(Job).where(Job.id == job_id, Job.org_id == org_id))
    if job is None:
        raise HTTPException(status_code=404, detail="not found")
    return job


async def candidate_in_org(
    session: AsyncSession, org_id: UUID, candidate_id: UUID
) -> Candidate:
    candidate = await session.scalar(
        select(Candidate).where(Candidate.id == candidate_id, Candidate.org_id == org_id)
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="not found")
    return candidate


@router.post("/candidates/resumes", response_model=BulkResumeOut)
async def upload_resumes(
    job_id: UUID = Form(...),
    files: list[UploadFile] = File(...),
    context: TenantContext = Depends(require(Permission.CANDIDATE_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> BulkResumeOut:
    job = await job_in_org(session, context.org_id, job_id)
    if len(files) > MAX_RESUME_FILES:
        raise HTTPException(
            status_code=413, detail=f"at most {MAX_RESUME_FILES} resumes per upload"
        )

    results: list[ResumeUploadOut] = []
    started: list[UUID] = []
    for upload in files:
        data = await upload.read()
        reason = rejection(data)
        if reason is not None:
            results.append(
                ResumeUploadOut(
                    resume_id=None,
                    filename=upload.filename or "resume.pdf",
                    status="rejected",
                    reason=reason,
                )
            )
            continue

        resume = await store_resume(
            session, context.org_id, data, upload.filename, job_id=job.id
        )
        started.append(resume.id)
        results.append(
            ResumeUploadOut(
                resume_id=resume.id, filename=resume.filename, status=EXTRACTING
            )
        )

    await session.commit()
    await start_extraction(started)

    return BulkResumeOut(
        job_id=job.id,
        accepted=len(started),
        rejected=sum(1 for r in results if r.status == "rejected"),
        resumes=results,
    )


@router.post("/candidates/{candidate_id}/resume", response_model=ResumeUploadOut, status_code=201)
async def attach_resume(
    candidate_id: UUID,
    file: UploadFile = File(...),
    job_id: UUID | None = Form(default=None),
    context: TenantContext = Depends(require(Permission.CANDIDATE_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> ResumeUploadOut:
    candidate = await candidate_in_org(session, context.org_id, candidate_id)
    if job_id is not None:
        await job_in_org(session, context.org_id, job_id)

    data = await file.read()
    reason = rejection(data)
    if reason is not None:
        raise HTTPException(status_code=422, detail=reason)

    resume = await store_resume(
        session, context.org_id, data, file.filename, candidate_id=candidate.id, job_id=job_id
    )
    await session.commit()
    await start_extraction([resume.id])

    return ResumeUploadOut(
        resume_id=resume.id, filename=resume.filename, status=EXTRACTING
    )


@router.get("/candidates/{candidate_id}/resume", response_model=ResumeOut)
async def read_resume(
    candidate_id: UUID,
    context: TenantContext = Depends(require(Permission.CANDIDATE_READ)),
    session: AsyncSession = Depends(get_session),
) -> ResumeOut:
    await candidate_in_org(session, context.org_id, candidate_id)
    resume = await current_resume(session, context.org_id, candidate_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="not found")

    return ResumeOut(
        resume_id=resume.id,
        candidate_id=resume.candidate_id,
        job_id=resume.job_id,
        filename=resume.filename,
        byte_size=resume.byte_size,
        status=resume.status,
        error_detail=resume.error_detail,
        data=resume.data,
        text=resume.text,
        created_at=resume.created_at,
    )


@router.get("/candidates/{candidate_id}/resume/file")
async def read_resume_file(
    candidate_id: UUID,
    context: TenantContext = Depends(require(Permission.CANDIDATE_READ)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await candidate_in_org(session, context.org_id, candidate_id)
    resume = await current_resume(session, context.org_id, candidate_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="not found")

    key = resume.pdf_url.removeprefix(f"s3://{get_settings().s3_bucket}/")
    data = await asyncio.to_thread(Storage().download_bytes, key)

    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="resume-{candidate_id}.pdf"'},
    )


@router.get("/jobs/{job_id}/resumes", response_model=list[JobResumeOut])
async def list_job_resumes(
    job_id: UUID,
    context: TenantContext = Depends(require(Permission.CANDIDATE_READ)),
    session: AsyncSession = Depends(get_session),
) -> list[JobResumeOut]:
    job = await job_in_org(session, context.org_id, job_id)

    rows = (
        await session.scalars(
            select(Resume)
            .where(Resume.org_id == context.org_id, Resume.job_id == job.id)
            .order_by(Resume.created_at.desc())
        )
    ).all()

    return [
        JobResumeOut(
            resume_id=row.id,
            candidate_id=row.candidate_id,
            filename=row.filename,
            status=row.status,
            error_detail=row.error_detail,
            created_at=row.created_at,
        )
        for row in rows
    ]
