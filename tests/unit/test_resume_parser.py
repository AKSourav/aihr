from pathlib import Path

import pytest

from core.resume_parser import MAX_TEXT_CHARS, ResumeParseError, extract_text

FIXTURE = Path(__file__).parent.parent / "fixtures" / "resumes" / "sample_resume.pdf"


@pytest.fixture
def sample() -> bytes:
    return FIXTURE.read_bytes()


def test_text_is_extracted_from_a_pdf(sample):
    text = extract_text(sample)

    assert "Asha Rao" in text
    assert "asha.rao@example.com" in text
    assert "PostgreSQL" in text


def test_a_file_that_is_not_a_pdf_is_rejected():
    with pytest.raises(ResumeParseError, match="not a PDF"):
        extract_text(b"first_name,last_name\nAsha,Rao\n")


def test_an_empty_file_is_rejected():
    with pytest.raises(ResumeParseError, match="not a PDF"):
        extract_text(b"")


def test_a_pdf_without_selectable_text_is_rejected():
    blank = (FIXTURE.parent / "blank_resume.pdf").read_bytes()

    with pytest.raises(ResumeParseError, match="no selectable text"):
        extract_text(blank)


def test_a_truncated_pdf_is_rejected_rather_than_crashing():
    with pytest.raises(ResumeParseError):
        extract_text(b"%PDF-1.4\n" + b"\x00" * 64)


def test_extracted_text_is_capped(sample):
    text = extract_text(sample)

    assert len(text) <= MAX_TEXT_CHARS
