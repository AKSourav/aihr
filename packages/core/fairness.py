import re

PROTECTED_TERMS = [
    "age",
    "aged",
    "years old",
    "young",
    "elderly",
    "male",
    "female",
    "gender",
    "pregnant",
    "pregnancy",
    "maternity",
    "paternity",
    "married",
    "marital",
    "spouse",
    "children",
    "kids",
    "religion",
    "religious",
    "caste",
    "race",
    "ethnicity",
    "nationality",
    "disability",
    "disabled",
    "handicap",
    "illness",
    "medical condition",
    "mental health",
]

_PROTECTED_RE = re.compile(r"\b(" + "|".join(re.escape(term) for term in PROTECTED_TERMS) + r")\b", re.IGNORECASE)


def protected_terms(text: str) -> list[str]:
    return sorted({match.lower() for match in _PROTECTED_RE.findall(text)})
