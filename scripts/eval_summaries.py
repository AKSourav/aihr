import asyncio
import json
import re
from pathlib import Path

from core.schemas import Turn
from core.summarizer import summarize_transcript
from scripts.seed import SENIOR_BACKEND_JOB

FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures" / "transcripts"
JOB_TITLE = SENIOR_BACKEND_JOB["title"]
RUBRIC = SENIOR_BACKEND_JOB["rubric"]
QUESTION_PLAN = SENIOR_BACKEND_JOB["question_plan"]
EVIDENCE_MATCH_THRESHOLD = 0.4

EXPECTED_FACTS = {
    "strong_fit": {
        "notice_period_days": 60,
        "current_ctc": "28 LPA",
        "expected_ctc": "35 LPA",
        "open_to_location": True,
    },
    "weak_fit": {
        "current_ctc": "8 LPA",
        "expected_ctc": "12 LPA",
    },
    "busy_now": {},
    "chatty": {
        "notice_period_days": 45,
        "current_ctc": "20 LPA",
        "expected_ctc": "26 LPA",
        "open_to_location": True,
    },
    "no_consent": {
        "notice_period_days": 90,
        "current_ctc": "32 LPA",
        "expected_ctc": "38 LPA",
        "open_to_location": True,
    },
    "off_topic": {
        "notice_period_days": 30,
        "current_ctc": "15 LPA",
        "expected_ctc": "20 LPA",
        "open_to_location": False,
    },
    "dropped_halfway": {},
}


def _facts_match(expected, actual) -> bool:
    if isinstance(expected, bool):
        if isinstance(actual, bool):
            return expected == actual
        if isinstance(actual, str) and actual.strip().lower() in ("true", "false", "yes", "no"):
            return expected == (actual.strip().lower() in ("true", "yes"))
        return False
    expected_str = str(expected).strip().lower()
    actual_str = str(actual).strip().lower()
    return expected_str in actual_str or actual_str in expected_str


def _evidence_grounded(evidence: str, transcript_text: str) -> bool:
    words = [w for w in re.findall(r"[a-z0-9']+", evidence.lower()) if len(w) > 4]
    if not words:
        return True
    matched = sum(1 for w in words if w in transcript_text)
    return (matched / len(words)) >= EVIDENCE_MATCH_THRESHOLD


async def evaluate_fixture(path: Path) -> bool:
    persona = path.stem
    transcript = [Turn.model_validate(turn) for turn in json.loads(path.read_text())]
    summary = await summarize_transcript(JOB_TITLE, RUBRIC, QUESTION_PLAN, transcript)

    ok = True
    expected = EXPECTED_FACTS.get(persona, {})
    for key, expected_value in expected.items():
        actual = summary.facts.get(key)
        if actual is None:
            print(f"[{persona}] MISSING fact '{key}' (expected {expected_value!r})")
            ok = False
        elif not _facts_match(expected_value, actual):
            print(f"[{persona}] fact '{key}' mismatch: expected {expected_value!r}, got {actual!r}")
            ok = False

    transcript_text = " ".join(turn.text.lower() for turn in transcript)
    for rs in summary.rubric_scores:
        # "communication" is a holistic style judgment, not a quotable fact, so word-overlap grounding doesn't apply.
        if rs.dimension == "communication":
            continue
        if rs.score >= 0.5 and not _evidence_grounded(rs.evidence, transcript_text):
            print(f"[{persona}] rubric '{rs.dimension}' evidence may be unsupported: {rs.evidence!r}")
            ok = False

    print(f"[{persona}] {'PASS' if ok else 'FAIL'}  (confidence={summary.confidence})")
    return ok


async def main() -> None:
    fixtures = sorted(FIXTURES_DIR.glob("*.json"))
    results = await asyncio.gather(*(evaluate_fixture(path) for path in fixtures))
    passed = sum(results)
    print(f"\n{passed}/{len(results)} fixtures passed")


if __name__ == "__main__":
    asyncio.run(main())
