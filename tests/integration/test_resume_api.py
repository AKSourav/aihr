from sqlalchemy import func, select
from test_read_api import (  # noqa: F401
    PDF_BYTES,
    auth_header,
    client,
    extraction,
    make_member,
    resume_file,
)

from core.models import Resume
from core.resumes import MAX_RESUME_FILES


def many_files(count: int) -> list:
    return [("files", (f"cv-{i}.pdf", PDF_BYTES, "application/pdf")) for i in range(count)]


async def test_adding_a_candidate_requires_a_resume_pdf(client, session, two_tenants, extraction):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates",
        data={"first_name": "Nina", "last_name": "Iyer", "email": "nina@candidates.test"},
        headers=auth_header(sub),
    )

    assert response.status_code == 422


async def test_a_file_that_is_not_a_pdf_is_rejected(client, session, two_tenants, extraction):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates",
        data={"first_name": "Nina", "last_name": "Iyer", "email": "nina@candidates.test"},
        files={"file": ("cv.pdf", b"first_name,last_name\nNina,Iyer\n", "application/pdf")},
        headers=auth_header(sub),
    )

    assert response.status_code == 422
    assert "not a PDF" in response.json()["detail"]


async def test_a_bulk_upload_creates_one_pending_resume_per_file(
    client, session, two_tenants, extraction
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/resumes",
        data={"job_id": str(two_tenants["job_a"])},
        files=many_files(3),
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 3
    assert body["rejected"] == 0
    assert [r["status"] for r in body["resumes"]] == ["extracting"] * 3
    assert len(extraction.started) == 3

    count = await session.scalar(
        select(func.count())
        .select_from(Resume)
        .where(Resume.org_id == two_tenants["org_a"], Resume.job_id == two_tenants["job_a"])
    )
    assert count == 3


async def test_a_bad_file_in_a_bulk_upload_is_reported_not_fatal(
    client, session, two_tenants, extraction
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    files = many_files(1) + [("files", ("notes.txt", b"hello", "application/pdf"))]

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/resumes",
        data={"job_id": str(two_tenants["job_a"])},
        files=files,
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["accepted"], body["rejected"]) == (1, 1)
    assert body["resumes"][1]["status"] == "rejected"
    assert body["resumes"][1]["resume_id"] is None


async def test_too_many_resumes_in_one_request_are_rejected(
    client, session, two_tenants, extraction
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/resumes",
        data={"job_id": str(two_tenants["job_a"])},
        files=many_files(MAX_RESUME_FILES + 1),
        headers=auth_header(sub),
    )

    assert response.status_code == 413


async def test_a_bulk_upload_against_another_tenants_job_is_404(
    client, session, two_tenants, extraction
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/resumes",
        data={"job_id": str(two_tenants["job_b"])},
        files=many_files(1),
        headers=auth_header(sub),
    )

    assert response.status_code == 404


async def test_a_viewer_may_not_upload_a_resume(client, session, two_tenants, extraction):
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/resumes",
        data={"job_id": str(two_tenants["job_a"])},
        files=many_files(1),
        headers=auth_header(sub),
    )

    assert response.status_code == 403


async def test_the_recruiter_can_read_the_resume_and_download_the_pdf(
    client, session, two_tenants, extraction
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    attach = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/{two_tenants['candidate_a']}/resume",
        files=resume_file("asha.pdf"),
        headers=auth_header(sub),
    )
    assert attach.status_code == 201

    meta = await client.get(
        f"/tenants/{two_tenants['org_a']}/candidates/{two_tenants['candidate_a']}/resume",
        headers=auth_header(sub),
    )
    assert meta.status_code == 200
    assert meta.json()["filename"] == "asha.pdf"
    assert meta.json()["status"] == "extracting"

    pdf = await client.get(
        f"/tenants/{two_tenants['org_a']}/candidates/{two_tenants['candidate_a']}/resume/file",
        headers=auth_header(sub),
    )
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content == PDF_BYTES


async def test_the_download_filename_is_derived_not_the_uploaded_one(
    client, session, two_tenants, extraction
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/{two_tenants['candidate_a']}/resume",
        files=resume_file('evil";\r\nX-Injected: 1.pdf'),
        headers=auth_header(sub),
    )

    pdf = await client.get(
        f"/tenants/{two_tenants['org_a']}/candidates/{two_tenants['candidate_a']}/resume/file",
        headers=auth_header(sub),
    )

    assert pdf.status_code == 200
    assert "X-Injected" not in pdf.headers
    assert str(two_tenants["candidate_a"]) in pdf.headers["content-disposition"]


async def test_a_candidate_with_no_resume_is_404(client, session, two_tenants, extraction):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/candidates/{two_tenants['candidate_a']}/resume",
        headers=auth_header(sub),
    )

    assert response.status_code == 404


async def test_a_resume_from_another_tenant_is_invisible(
    client, session, two_tenants, extraction
):
    sub = await make_member(session, two_tenants["org_b"], "recruiter")

    response = await client.get(
        f"/tenants/{two_tenants['org_b']}/candidates/{two_tenants['candidate_a']}/resume",
        headers=auth_header(sub),
    )

    assert response.status_code == 404


async def test_the_job_resume_list_reports_status(client, session, two_tenants, extraction):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/resumes",
        data={"job_id": str(two_tenants["job_a"])},
        files=many_files(2),
        headers=auth_header(sub),
    )

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/resumes",
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2
    assert {r["status"] for r in rows} == {"extracting"}
