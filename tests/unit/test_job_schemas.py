import pytest
from pydantic import ValidationError

from core.schemas import QuestionPlan, Rubric, RubricScore
from core.summarizer import _render_rubric
from scripts.seed import CUSTOMER_SUPPORT_JOB, SDE1_JOB, SENIOR_BACKEND_JOB
from services.temporal_worker.activities.summarize_call import _weighted_overall

VALID_DIMENSION = {"dimension": "depth", "weight": 0.5, "description": "technical depth"}


def plan(**overrides) -> dict:
    base = {
        "intro_pitch": "hello",
        "questions": [{"id": "q_one", "text": "tell me about yourself"}],
        "max_minutes": 15,
    }
    return {**base, **overrides}


@pytest.mark.parametrize(
    "rubric",
    [
        [{"dimension": "depth", "weight": 0, "description": "d"}],
        [{"dimension": "depth", "weight": -1, "description": "d"}],
        [{"dimension": "depth", "weight": 0.5}],
        [{"dimension": "depth", "weight": 0.5, "description": ""}],
        [{"dimension": "", "weight": 0.5, "description": "d"}],
        [VALID_DIMENSION, {"dimension": "depth", "weight": 0.2, "description": "other"}],
        [],
    ],
)
def test_invalid_rubrics_are_rejected(rubric):
    with pytest.raises(ValidationError):
        Rubric.model_validate(rubric)


def test_weights_need_not_sum_to_one():
    rubric = Rubric.model_validate(
        [
            {"dimension": "depth", "weight": 2, "description": "technical depth"},
            {"dimension": "communication", "weight": 3, "description": "clarity"},
        ]
    )
    assert [item.weight for item in rubric.root] == [2.0, 3.0]


def test_rubric_dumps_to_a_bare_list():
    assert Rubric.model_validate([VALID_DIMENSION]).model_dump() == [VALID_DIMENSION]


@pytest.mark.parametrize("spec", [SENIOR_BACKEND_JOB, CUSTOMER_SUPPORT_JOB, SDE1_JOB])
def test_seed_jobs_validate(spec):
    assert Rubric.model_validate(spec["rubric"]).model_dump() == spec["rubric"]
    QuestionPlan.model_validate(spec["question_plan"])


def test_dumped_rubric_works_with_summary_consumers():
    rubric = Rubric.model_validate(SENIOR_BACKEND_JOB["rubric"]).model_dump()
    scores = [RubricScore(dimension=item["dimension"], score=1.0, evidence="e") for item in rubric]

    assert _weighted_overall(rubric, scores) == 1.0
    assert "backend_depth (weight 0.35)" in _render_rubric(rubric)


def test_duplicate_question_ids_are_rejected():
    questions = [
        {"id": "q_one", "text": "first"},
        {"id": "q_one", "text": "second"},
    ]
    with pytest.raises(ValidationError):
        QuestionPlan.model_validate(plan(questions=questions))


def test_empty_question_list_is_rejected():
    with pytest.raises(ValidationError):
        QuestionPlan.model_validate(plan(questions=[]))


@pytest.mark.parametrize("max_minutes", [0, -1, 61])
def test_max_minutes_bounds_are_enforced(max_minutes):
    with pytest.raises(ValidationError):
        QuestionPlan.model_validate(plan(max_minutes=max_minutes))


@pytest.mark.parametrize("max_minutes", [1, 60])
def test_max_minutes_within_bounds_is_accepted(max_minutes):
    assert QuestionPlan.model_validate(plan(max_minutes=max_minutes)).max_minutes == max_minutes
