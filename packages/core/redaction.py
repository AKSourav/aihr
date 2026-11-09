import re

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE_RE = re.compile(r"(?<!\w)\(?\+?\d[\d\s().-]{7,}\d(?!\w)")

EMAIL_PLACEHOLDER = "[email]"
PHONE_PLACEHOLDER = "[phone]"


def redact(text: str) -> str:
    text = EMAIL_RE.sub(EMAIL_PLACEHOLDER, text)
    return PHONE_RE.sub(PHONE_PLACEHOLDER, text)
