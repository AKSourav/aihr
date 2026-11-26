from core.schemas import ShortlistRules
from core.shortlist import qualifies


def rules(**kwargs) -> ShortlistRules:
    return ShortlistRules.model_validate(kwargs)


def test_an_empty_rule_set_qualifies_nobody():
    assert qualifies(rules(), rank=1, overall=0.99) is False


def test_top_k_alone_qualifies_the_first_k_ranks():
    rule = rules(top_k=2)

    assert qualifies(rule, rank=1, overall=0.1) is True
    assert qualifies(rule, rank=2, overall=None) is True
    assert qualifies(rule, rank=3, overall=0.99) is False


def test_min_score_alone_needs_a_score_at_or_above_the_threshold():
    rule = rules(min_score=0.7)

    assert qualifies(rule, rank=99, overall=0.7) is True
    assert qualifies(rule, rank=1, overall=0.69) is False


def test_an_unscored_candidate_never_meets_a_threshold():
    assert qualifies(rules(min_score=0.0), rank=1, overall=None) is False


def test_combining_with_all_requires_every_rule_to_match():
    rule = rules(top_k=2, min_score=0.8, combine="all")

    assert qualifies(rule, rank=1, overall=0.9) is True
    assert qualifies(rule, rank=1, overall=0.5) is False
    assert qualifies(rule, rank=5, overall=0.9) is False


def test_combining_with_any_requires_only_one_rule_to_match():
    rule = rules(top_k=2, min_score=0.8, combine="any")

    assert qualifies(rule, rank=1, overall=0.5) is True
    assert qualifies(rule, rank=5, overall=0.9) is True
    assert qualifies(rule, rank=5, overall=0.5) is False


def test_rules_default_to_advancing():
    assert rules(top_k=3).auto_advance is True
