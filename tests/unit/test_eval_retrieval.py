"""Tests for the retrieval eval's scoring in scripts/eval_retrieval.py (4.10).

These need no database: each result is built by hand with the ranking a
search would have returned. The scoring is what the threshold is chosen
from, so a miscount here would move the chosen value without anyone
noticing.
"""

from scripts.eval_retrieval import (
    EVAL_SET,
    CaseResult,
    EvalCase,
    hit_rate,
    missing_services,
    sweep,
)
from scripts.seed import SEED_SERVICES


def _offered(expected: str, *ranking: tuple[str, float]) -> CaseResult:
    return CaseResult(case=EvalCase("a query", expected), ranking=list(ranking))


def _not_offered(*ranking: tuple[str, float]) -> CaseResult:
    return CaseResult(case=EvalCase("a query", None), ranking=list(ranking))


def test_rank_counts_from_one_and_carries_the_expected_services_score() -> None:
    result = _offered("Knee MRI Scan", ("Knee X-Ray", 0.6), ("Knee MRI Scan", 0.5))

    assert result.rank == 2
    assert result.expected_similarity == 0.5
    assert result.best_similarity == 0.6


def test_a_not_offered_query_has_no_rank() -> None:
    result = _not_offered(("Knee X-Ray", 0.4))

    assert result.rank is None
    assert result.expected_similarity is None


def test_hit_rate_counts_offered_queries_within_k_and_ignores_not_offered() -> None:
    results = [
        _offered("A", ("A", 0.9), ("B", 0.1)),  # rank 1
        _offered("A", ("B", 0.9), ("C", 0.5), ("A", 0.1)),  # rank 3
        _not_offered(("A", 0.9)),
    ]

    assert hit_rate(results, 1) == (1, 2)
    assert hit_rate(results, 3) == (2, 2)


def test_sweep_finds_a_service_scoring_exactly_the_threshold() -> None:
    """POST /search keeps a score equal to the threshold (distance <=
    1 - t), so the sweep must too, or it would report a stricter
    threshold than the one it was given."""
    [row] = sweep([_offered("A", ("A", 0.4))], (0.4,), top_k=5)

    assert row.found == 1


def test_sweep_misses_a_service_below_the_threshold_or_outside_top_k() -> None:
    results = [
        _offered("A", ("A", 0.3)),  # below the threshold
        _offered("A", ("B", 0.9), ("C", 0.8), ("A", 0.7)),  # rank 3 of top_k 2
    ]

    [row] = sweep(results, (0.4,), top_k=2)

    assert row.found == 0


def test_sweep_refuses_a_query_only_when_its_best_match_is_below() -> None:
    results = [
        _not_offered(("A", 0.39), ("B", 0.1)),  # refused at 0.4
        _not_offered(("A", 0.4)),  # exactly the threshold: POST /search shows it
        _not_offered(("A", 0.41)),  # a weak match would be shown
        _not_offered(),  # nothing published at all
    ]

    [row] = sweep(results, (0.4,), top_k=5)

    assert row.refused == 2
    assert row.right == 2


def test_sweep_returns_one_row_per_threshold_in_order() -> None:
    results = [_offered("A", ("A", 0.5)), _not_offered(("A", 0.35))]

    rows = sweep(results, (0.3, 0.4, 0.6), top_k=5)

    assert [(row.threshold, row.found, row.refused) for row in rows] == [
        (0.3, 1, 0),
        (0.4, 1, 1),
        (0.6, 0, 1),
    ]


def test_missing_services_names_expected_services_that_are_not_published() -> None:
    eval_set = (
        EvalCase("x-ray of my knee", "Knee X-Ray"),
        EvalCase("MRI of my knee", "Knee MRI Scan"),
        EvalCase("dental cleaning", None),
    )

    assert missing_services(eval_set, {"Knee X-Ray"}) == ["Knee MRI Scan"]


def test_the_eval_set_has_at_least_ten_offered_pairs_and_some_not_offered() -> None:
    """The brief asks for at least ten query -> expected-service pairs; the
    not-offered queries are what the threshold is tested against."""
    offered = [case for case in EVAL_SET if case.expected is not None]
    not_offered = [case for case in EVAL_SET if case.expected is None]

    assert len(offered) >= 10
    assert len(not_offered) >= 5


def test_the_eval_set_expects_exactly_the_seeded_catalogue() -> None:
    """Every expected service is seeded, so a typo cannot pass for a miss,
    and every seeded service is expected at least once, so none goes
    unmeasured."""
    expected = {case.expected for case in EVAL_SET if case.expected is not None}

    assert expected == {service.name for service in SEED_SERVICES}


def test_the_eval_set_has_no_repeated_query() -> None:
    queries = [case.query for case in EVAL_SET]

    assert len(queries) == len(set(queries))
