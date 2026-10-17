"""Tests for the chunk text format in app/ai/chunking.py.

These need no database. The format matters twice over: its words are what
a patient's query is matched against, and its exact bytes are hashed, so a
text that changes without the service changing would be re-embedded for
nothing (task 4.9).
"""

from app.ai.chunking import build_service_chunk_text

KNEE_XRAY = {
    "department": "Orthopaedics",
    "name": "Knee X-Ray",
    "description": "Standard imaging of the knee joint.",
    "prep_instructions": "Wear loose clothing.",
}


def test_one_specialty_sits_between_department_and_name() -> None:
    """The brief's shape: "{department} · {specialty}: {name}. ..."."""
    text = build_service_chunk_text(specialties=["Orthopedics"], **KNEE_XRAY)

    assert text == (
        "Orthopaedics · Orthopedics: Knee X-Ray. "
        "Standard imaging of the knee joint. Preparation: Wear loose clothing."
    )


def test_several_specialties_are_listed_once_in_a_fixed_order() -> None:
    """Two providers sharing a specialty must not list it twice, and the
    database's row order must not leak into the text -- either would change
    the hash of a service that has not changed."""
    one_order = build_service_chunk_text(
        specialties=["Sports Medicine", "Orthopedics", "Sports Medicine"], **KNEE_XRAY
    )
    other_order = build_service_chunk_text(
        specialties=["Orthopedics", "Sports Medicine"], **KNEE_XRAY
    )

    assert one_order == other_order
    assert one_order.startswith("Orthopaedics · Orthopedics, Sports Medicine: ")


def test_no_specialty_drops_the_separator() -> None:
    """A service no provider is linked to yet must read cleanly, not as
    "Orthopaedics · : Knee X-Ray"."""
    text = build_service_chunk_text(specialties=[], **KNEE_XRAY)

    assert text.startswith("Orthopaedics: Knee X-Ray. ")
    assert "·" not in text


def test_a_specialty_repeating_the_department_is_dropped() -> None:
    """The seed data has a Cardiology department whose providers' specialty
    is also Cardiology. Repeating it adds no word a patient might search
    for, whatever its capitalisation."""
    text = build_service_chunk_text(
        department="Cardiology",
        specialties=["cardiology"],
        name="Echocardiogram",
        description="Ultrasound imaging of the heart.",
        prep_instructions="Arrive 10 minutes early.",
    )

    assert text.startswith("Cardiology: Echocardiogram. ")


def test_a_missing_full_stop_is_added_and_an_existing_one_is_not_doubled() -> None:
    """Descriptions are typed by staff, with or without a final full stop.
    Either way each part must end exactly once before the next begins."""
    text = build_service_chunk_text(
        specialties=[],
        department="Orthopaedics",
        name="Knee X-Ray",
        description="Standard imaging of the knee joint",
        prep_instructions="Wear loose clothing.  ",
    )

    assert text == (
        "Orthopaedics: Knee X-Ray. Standard imaging of the knee joint. "
        "Preparation: Wear loose clothing."
    )
