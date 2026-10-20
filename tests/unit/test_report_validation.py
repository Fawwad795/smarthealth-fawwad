"""parse_report() and describe_problem() (task 5.9): how a model's report
reply is checked before it becomes part of a utilisation report.

The model writes only the commentary. Every number in the report comes from
the figures the code read, so a reply with a digit in it is rejected like a
malformed one, and the repair prompt says why.
"""

from datetime import date

import pytest
from pydantic import ValidationError

from app.models.enums import AppointmentStatus
from app.schemas.analytics import AnalyticsSummaryResponse
from app.schemas.generation import DepartmentUtilisation, UtilisationFigures
from app.services.generation import STATUS_WORDS, describe_problem, parse_report

FIGURES = UtilisationFigures(
    analytics=AnalyticsSummaryResponse(
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
        total_patients=3,
        appointments_booked=5,
        completed_visits=1,
        cancellations=3,
        cancellation_rate=0.6,
        avg_wait_seconds=None,
        failed_jobs=0,
    ),
    departments=[
        DepartmentUtilisation(
            department_id=1,
            department="Cardiology",
            slots=10,
            booked_slots=4,
            utilisation=0.4,
        )
    ],
)

GOOD = (
    '{"summary": "Bookings were steady.", '
    '"observations": ["Cardiology was the busiest department."]}'
)


def test_a_valid_reply_becomes_a_report_with_the_codes_figures() -> None:
    report = parse_report(GOOD, FIGURES)

    assert report.figures == FIGURES
    assert report.summary == "Bookings were steady."
    assert report.observations == ["Cardiology was the busiest department."]


def test_a_code_fence_around_the_json_is_accepted() -> None:
    """Models add one even when told not to; it hides nothing."""
    report = parse_report(f"```json\n{GOOD}\n```", FIGURES)

    assert report.summary == "Bookings were steady."


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        ("Here is the report: none", "reply: Invalid JSON"),
        ('{"summary": "Fine."}', "observations: Field required"),
        (
            '{"summary": "Fine.", "observations": []}',
            "observations: List should have at least 1 item",
        ),
        (
            '{"summary": "Fine.", "observations": ["Ok."], "figures": {}}',
            "figures: Extra inputs are not permitted",
        ),
        (
            '{"summary": "Bookings rose to twelve, then 12.", "observations": ["Ok."]}',
            "summary: Value error, contains a digit",
        ),
        (
            '{"summary": "Fine.", "observations": ["Cardiology was 40% full."]}',
            "observations: Value error, contains a digit",
        ),
        ('{"summary": "Fine.", "observations": ["  "]}', "an observation is empty"),
        (
            '{"summary": "", "observations": ["Ok."]}',
            "summary: String should have at least 1 character",
        ),
    ],
    ids=[
        "not-json",
        "missing-field",
        "no-observations",
        "extra-key",
        "digit-in-summary",
        "digit-in-observation",
        "blank-observation",
        "empty-summary",
    ],
)
def test_an_unusable_reply_is_rejected_with_what_was_wrong(
    reply: str, problem: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_report(reply, FIGURES)

    assert problem in describe_problem(caught.value)


def test_too_many_observations_are_rejected() -> None:
    reply = '{"summary": "Fine.", "observations": ["A.", "B.", "C.", "D.", "E."]}'

    with pytest.raises(ValidationError) as caught:
        parse_report(reply, FIGURES)

    assert "observations: List should have at most 4 items" in describe_problem(
        caught.value
    )


def test_an_overlong_observation_is_rejected() -> None:
    reply = '{"summary": "Fine.", "observations": ["' + "a" * 301 + '"]}'

    with pytest.raises(ValidationError) as caught:
        parse_report(reply, FIGURES)

    assert "an observation is longer than 300 characters" in describe_problem(
        caught.value
    )


def test_the_problem_never_repeats_the_models_text() -> None:
    """It goes into the repair prompt, the retry event and the logs."""
    reply = '{"summary": "Zebulon booked 12 times.", "observations": ["Ok."]}'

    with pytest.raises(ValidationError) as caught:
        parse_report(reply, FIGURES)

    assert "Zebulon" not in describe_problem(caught.value)


def test_every_appointment_status_has_words_for_a_patient() -> None:
    """A status with no entry would raise KeyError mid-request."""
    assert set(STATUS_WORDS) == set(AppointmentStatus)
