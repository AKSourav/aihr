import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Application, AuditEvent, Candidate, Job, Organization, Resume
from core.providers import resolve_providers
from core.resume_extractor import PROMPT_VERSION, extract_resume
from core.resume_parser import ResumeParseError, extract_text
from core.schemas import EMAIL_RE
from core.settings import get_settings
from core.storage import Storage

logger = logging.getLogger(__name__)

MAX_RESUME_BYTES = 10 * 1024 * 1024
MAX_RESUME_FILES = 20
MAX_UPLOAD_BYTES = 60 * 1024 * 1024
PDF_MAGIC = b"%PDF-"
PDF_CONTENT_TYPE = "application/pdf"

AWAITING = "awaiting_resume"
EXTRACTING = "extracting"
READY = "ready"
FAILED = "failed"


def resume_key(org_id: UUID, resume_id: UUID) -> str:
    return f"resumes/{org_id}/{resume_id}.pdf"


def safe_filename(name: str | None) -> str:
    base = PurePosixPath((name or "").replace("\\", "/")).name
    cleaned = "".join(ch for ch in base if ch.isprintable() and ch not in '"\r\n').strip()
    return cleaned[:255] or "resume.pdf"


def rejection(data: bytes) -> str | None:
    if not data:
        return "the file is empty"
    if len(data) > MAX_RESUME_BYTES:
        return f"the file is larger than {MAX_RESUME_BYTES // (1024 * 1024)} MB"
    if not data.startswith(PDF_MAGIC):
        return "the file is not a PDF"
    return None


async def store_resume(
    session: AsyncSession,
    org_id: UUID,
    data: bytes,
    filename: str | None,
    candidate_id: UUID | None = None,
    job_id: UUID | None = None,
) -> Resume:
    resume_id = uuid.uuid4()
    key = resume_key(org_id, resume_id)
    pdf_url = await asyncio.to_thread(
        Storage().upload_bytes, key, data, PDF_CONTENT_TYPE
    )

    resume = Resume(
        id=resume_id,
        org_id=org_id,
        candidate_id=candidate_id,
        job_id=job_id,
        pdf_url=pdf_url,
        filename=safe_filename(filename),
        byte_size=len(data),
        status=EXTRACTING,
        created_at=datetime.now(timezone.utc),
    )
    session.add(resume)
    await session.flush()
    return resume


async def current_resume(
    session: AsyncSession, org_id: UUID, candidate_id: UUID
) -> Resume | None:
    return await session.scalar(
        select(Resume)
        .where(Resume.org_id == org_id, Resume.candidate_id == candidate_id)
        .order_by(Resume.created_at.desc(), Resume.id.desc())
        .limit(1)
    )


async def resume_status(session: AsyncSession, org_id: UUID, candidate_id: UUID) -> str:
    resume = await current_resume(session, org_id, candidate_id)
    return resume.status if resume is not None else AWAITING


async def resume_statuses(
    session: AsyncSession, org_id: UUID, candidate_ids: list[UUID]
) -> dict[UUID, str]:
    if not candidate_ids:
        return {}

    rows = (
        await session.scalars(
            select(Resume)
            .where(Resume.org_id == org_id, Resume.candidate_id.in_(candidate_ids))
            .order_by(Resume.created_at.desc(), Resume.id.desc())
        )
    ).all()

    latest: dict[UUID, str] = {}
    for row in rows:
        if row.candidate_id is not None:
            latest.setdefault(row.candidate_id, row.status)
    return {candidate_id: latest.get(candidate_id, AWAITING) for candidate_id in candidate_ids}


async def fail_resume(session: AsyncSession, resume_id: UUID, reason: str) -> None:
    resume = await session.get(Resume, resume_id)
    if resume is None or resume.status != EXTRACTING:
        return
    resume.status = FAILED
    resume.error_detail = reason
    await session.commit()


async def _candidate_for(
    session: AsyncSession, org_id: UUID, email: str, first_name: str, last_name: str
) -> Candidate:
    existing = await session.scalar(
        select(Candidate).where(Candidate.org_id == org_id, Candidate.email == email)
    )
    if existing is not None:
        return existing

    candidate = Candidate(
        org_id=org_id,
        first_name=first_name or email.split("@")[0],
        last_name=last_name,
        email=email,
        phone_e164=None,
    )
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
    except IntegrityError:
        found = await session.scalar(
            select(Candidate).where(Candidate.org_id == org_id, Candidate.email == email)
        )
        if found is None:
            raise
        return found
    return candidate


async def _application_for(
    session: AsyncSession, org_id: UUID, candidate_id: UUID, job_id: UUID
) -> Application | None:
    def lookup():
        return session.scalar(
            select(Application).where(
                Application.org_id == org_id,
                Application.candidate_id == candidate_id,
                Application.job_id == job_id,
            )
        )

    existing = await lookup()
    if existing is not None:
        return existing

    application = Application(
        org_id=org_id, candidate_id=candidate_id, job_id=job_id, stage="shortlisted"
    )
    try:
        async with session.begin_nested():
            session.add(application)
            await session.flush()
    except IntegrityError:
        return await lookup()
    return application


def _links_of(extract) -> dict | None:
    links = extract.links.model_dump(exclude_none=True)
    if not links.get("other"):
        links.pop("other", None)
    return links or None


async def ingest_resume(session: AsyncSession, resume_id: UUID) -> str | None:
    """Extract one stored resume. Returns the application id worth scoring, if there is one."""
    settings = get_settings()

    resume = await session.get(Resume, resume_id)
    if resume is None or resume.status != EXTRACTING:
        return None

    key = resume.pdf_url.removeprefix(f"s3://{settings.s3_bucket}/")
    data = await asyncio.to_thread(Storage().download_bytes, key)

    try:
        text = extract_text(data)
    except ResumeParseError as exc:
        resume.status = FAILED
        resume.error_detail = str(exc)
        await session.commit()
        logger.info("resume could not be parsed", extra={"resume_id": str(resume_id)})
        return None

    organization = await session.get(Organization, resume.org_id)
    job = await session.get(Job, resume.job_id) if resume.job_id else None
    llm = None
    if organization is not None:
        try:
            llm = (await resolve_providers(session, organization, job)).llm_summary
        except Exception as exc:
            logger.warning(
                "falling back to platform extraction model",
                extra={"resume_id": str(resume_id), "error": str(exc)},
            )

    extract, usage = await extract_resume(text, llm)

    candidate_id = resume.candidate_id
    if candidate_id is None:
        email = extract.email.strip().lower()
        if not email or EMAIL_RE.match(email) is None:
            resume.status = FAILED
            resume.error_detail = "no email address found in this resume"
            await session.commit()
            return None

        candidate = await _candidate_for(
            session, resume.org_id, email, extract.first_name.strip(), extract.last_name.strip()
        )
        candidate_id = candidate.id
        resume.candidate_id = candidate_id
    else:
        candidate = await session.get(Candidate, candidate_id)

    if candidate is not None:
        if candidate.phone_e164 is None and extract.phone_e164:
            candidate.phone_e164 = extract.phone_e164
        if candidate.links is None:
            candidate.links = _links_of(extract)

    application = None
    if resume.job_id is not None and candidate_id is not None:
        application = await _application_for(
            session, resume.org_id, candidate_id, resume.job_id
        )

    resume.text = text
    resume.data = extract.model_dump(mode="json")
    resume.model_version = (llm.model if llm else "") or settings.llm_summary_model
    resume.prompt_version = PROMPT_VERSION
    resume.status = READY

    session.add(
        AuditEvent(
            org_id=resume.org_id,
            entity_type="resume",
            entity_id=resume.id,
            event_type="resume_extracted",
            payload={
                "candidate_id": str(candidate_id),
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
            },
        )
    )
    await session.commit()
    logger.info("resume extracted", extra={"resume_id": str(resume_id)})
    return str(application.id) if application is not None else None
