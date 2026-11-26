import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core import resumes as resumes_module
from core.models import Application, AuditEvent, Candidate, Resume
from core.resume_parser import ResumeParseError
from core.schemas import ResumeExtract, TokenUsage

PDF = b"%PDF-1.4 fake body"


class FakeStorage:
    uploaded: dict[str, bytes] = {}

    def upload_bytes(self, key: str, body: bytes, content_type: str) -> str:
        FakeStorage.uploaded[key] = body
        return f"s3://test-bucket/{key}"

    def download_bytes(self, key: str) -> bytes:
        return FakeStorage.uploaded.get(key, PDF)


@pytest.fixture(autouse=True)
def fake_storage(monkeypatch):
    FakeStorage.uploaded = {}
    monkeypatch.setattr(resumes_module, "Storage", FakeStorage)
    monkeypatch.setattr(resumes_module, "extract_text", lambda data: "resume text")


def install_extract(monkeypatch, **overrides):
    payload = {
        "first_name": "Priya",
        "last_name": "Nair",
        "email": "Priya.Nair@Example.com",
        "phone_e164": "+919800000001",
        "links": {"linkedin": "https://linkedin.com/in/priya"},
        "skills": ["Python"],
    }
    payload.update(overrides)
    extract = ResumeExtract.model_validate(payload)

    async def fake(text, llm=None):
        return extract, TokenUsage(prompt_tokens=10, completion_tokens=5)

    monkeypatch.setattr(resumes_module, "extract_resume", fake)
    return extract


async def test_extraction_creates_a_candidate_and_application_from_the_resume(
    session: AsyncSession, two_tenants, monkeypatch
):
    install_extract(monkeypatch)
    resume = await resumes_module.store_resume(
        session, two_tenants["org_a"], PDF, "priya.pdf", job_id=two_tenants["job_a"]
    )
    await session.flush()

    await resumes_module.ingest_resume(session, resume.id)

    await session.refresh(resume)
    assert resume.status == resumes_module.READY
    candidate = await session.get(Candidate, resume.candidate_id)
    assert candidate is not None
    assert candidate.email == "priya.nair@example.com"
    assert candidate.phone_e164 == "+919800000001"
    assert candidate.links == {"linkedin": "https://linkedin.com/in/priya"}

    application = await session.scalar(
        select(Application).where(
            Application.org_id == two_tenants["org_a"],
            Application.candidate_id == candidate.id,
            Application.job_id == two_tenants["job_a"],
        )
    )
    assert application is not None
    assert application.stage == "shortlisted"


async def test_two_resumes_with_the_same_email_reuse_one_candidate(
    session: AsyncSession, two_tenants, monkeypatch
):
    install_extract(monkeypatch)
    first = await resumes_module.store_resume(
        session, two_tenants["org_a"], PDF, "a.pdf", job_id=two_tenants["job_a"]
    )
    second = await resumes_module.store_resume(
        session, two_tenants["org_a"], PDF, "b.pdf", job_id=two_tenants["job_a"]
    )
    await session.flush()

    await resumes_module.ingest_resume(session, first.id)
    await resumes_module.ingest_resume(session, second.id)

    await session.refresh(first)
    await session.refresh(second)
    assert first.candidate_id == second.candidate_id

    count = await session.scalar(
        select(func.count())
        .select_from(Candidate)
        .where(
            Candidate.org_id == two_tenants["org_a"],
            Candidate.email == "priya.nair@example.com",
        )
    )
    assert count == 1

    applications = await session.scalar(
        select(func.count())
        .select_from(Application)
        .where(
            Application.org_id == two_tenants["org_a"],
            Application.candidate_id == first.candidate_id,
        )
    )
    assert applications == 1


async def test_a_resume_without_an_email_is_marked_failed(
    session: AsyncSession, two_tenants, monkeypatch
):
    install_extract(monkeypatch, email="")
    resume = await resumes_module.store_resume(
        session, two_tenants["org_a"], PDF, "noemail.pdf", job_id=two_tenants["job_a"]
    )
    await session.flush()

    await resumes_module.ingest_resume(session, resume.id)

    await session.refresh(resume)
    assert resume.status == resumes_module.FAILED
    assert "no email address" in (resume.error_detail or "")
    assert resume.candidate_id is None


async def test_a_resume_the_parser_rejects_is_marked_failed(
    session: AsyncSession, two_tenants, monkeypatch
):
    install_extract(monkeypatch)

    def boom(data):
        raise ResumeParseError("no selectable text found in this PDF")

    monkeypatch.setattr(resumes_module, "extract_text", boom)
    resume = await resumes_module.store_resume(
        session, two_tenants["org_a"], PDF, "scan.pdf", job_id=two_tenants["job_a"]
    )
    await session.flush()

    await resumes_module.ingest_resume(session, resume.id)

    await session.refresh(resume)
    assert resume.status == resumes_module.FAILED
    assert "no selectable text" in (resume.error_detail or "")


async def test_extraction_is_idempotent(session: AsyncSession, two_tenants, monkeypatch):
    install_extract(monkeypatch)
    resume = await resumes_module.store_resume(
        session, two_tenants["org_a"], PDF, "p.pdf", job_id=two_tenants["job_a"]
    )
    await session.flush()

    await resumes_module.ingest_resume(session, resume.id)
    await resumes_module.ingest_resume(session, resume.id)

    events = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.org_id == two_tenants["org_a"],
            AuditEvent.entity_id == resume.id,
            AuditEvent.event_type == "resume_extracted",
        )
    )
    assert events == 1


async def test_a_typed_identity_is_not_overwritten_by_the_resume(
    session: AsyncSession, two_tenants, monkeypatch
):
    install_extract(monkeypatch)
    resume = await resumes_module.store_resume(
        session,
        two_tenants["org_a"],
        PDF,
        "asha.pdf",
        candidate_id=two_tenants["candidate_a"],
        job_id=two_tenants["job_a"],
    )
    await session.flush()

    await resumes_module.ingest_resume(session, resume.id)

    candidate = await session.get(Candidate, two_tenants["candidate_a"])
    assert candidate is not None
    assert candidate.email == "asha@candidates.test"
    assert candidate.first_name == "Asha"
    assert candidate.phone_e164 == "+919800000001"


async def test_the_newest_resume_is_the_current_one(
    session: AsyncSession, two_tenants, monkeypatch
):
    install_extract(monkeypatch)
    older = await resumes_module.store_resume(
        session, two_tenants["org_a"], PDF, "old.pdf", candidate_id=two_tenants["candidate_a"]
    )
    await session.flush()
    await resumes_module.ingest_resume(session, older.id)

    newer = await resumes_module.store_resume(
        session, two_tenants["org_a"], PDF, "new.pdf", candidate_id=two_tenants["candidate_a"]
    )
    await session.flush()

    current = await resumes_module.current_resume(
        session, two_tenants["org_a"], two_tenants["candidate_a"]
    )
    assert current is not None and current.id == newer.id
    assert (
        await resumes_module.resume_status(
            session, two_tenants["org_a"], two_tenants["candidate_a"]
        )
        == resumes_module.EXTRACTING
    )


async def test_a_candidate_with_no_resume_is_awaiting_one(session: AsyncSession, two_tenants):
    status = await resumes_module.resume_status(
        session, two_tenants["org_a"], two_tenants["candidate_a"]
    )

    assert status == resumes_module.AWAITING


async def test_a_resume_cannot_point_at_another_tenants_candidate(
    session: AsyncSession, two_tenants
):
    session.add(
        Resume(
            id=uuid.uuid4(),
            org_id=two_tenants["org_b"],
            candidate_id=two_tenants["candidate_a"],
            pdf_url="s3://test-bucket/x.pdf",
            filename="x.pdf",
            byte_size=10,
            status=resumes_module.EXTRACTING,
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
