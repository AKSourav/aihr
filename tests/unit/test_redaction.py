import pytest

from core.redaction import redact


@pytest.mark.parametrize(
    "text",
    [
        "Call me on +91 98765 43210",
        "My number is 9876543210.",
        "Reach me at (080) 2345-6789",
        "ring 098-765-4321",
    ],
)
def test_phone_numbers_are_redacted(text):
    assert "[phone]" in redact(text)
    assert not any(char.isdigit() for char in redact(text))


def test_emails_are_redacted():
    assert redact("write to raj.kumar@example.co.in") == "write to [email]"


@pytest.mark.parametrize(
    "text",
    [
        "I'm currently at 28 LPA fixed, and I'm expecting around 35 LPA.",
        "My notice period is 60 days.",
        "I handled 2 million transactions a day.",
        "We had a 20 minute outage in 2024.",
        "I led a team of 12 engineers.",
    ],
)
def test_compensation_and_metrics_survive(text):
    assert redact(text) == text
