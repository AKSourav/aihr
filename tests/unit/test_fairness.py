import pytest

from core.fairness import protected_terms


@pytest.mark.parametrize(
    "text",
    [
        "Candidate seems quite young for a senior role.",
        "She mentioned she is pregnant and may need leave.",
        "Married with two kids, may not relocate.",
        "Has a medical condition that may affect availability.",
    ],
)
def test_protected_attributes_are_detected(text):
    assert protected_terms(text)


@pytest.mark.parametrize(
    "text",
    [
        "Gaps in distributed systems knowledge.",
        "Expected compensation is above the band.",
        "Average experience in incident response.",
        "Notice period of 90 days is long.",
    ],
)
def test_job_related_concerns_are_not_flagged(text):
    assert protected_terms(text) == []


def test_matching_is_word_bounded():
    assert protected_terms("manages a package registry") == []
    assert protected_terms("average throughput") == []
