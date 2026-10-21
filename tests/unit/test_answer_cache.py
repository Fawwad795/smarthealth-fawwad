"""The answer cache's key and entries (task 5.12). No Redis needed."""

import logging

import pytest
import redis

from app.ai.prompts import (
    ServiceContext,
    build_navigation_prompt,
    build_safety_prompt,
    no_match_reply,
    refusal_reply,
)
from app.ai.retrieval import RetrievedService
from app.models.enums import AssistantIntent, InteractionOutcome
from app.services.answer_cache import (
    CACHED_PROMPT_VERSIONS,
    CachedAnswer,
    answer_key,
    normalise,
    store,
)

KNEE = RetrievedService(
    service_id=3,
    name="Knee X-Ray",
    department="Radiology",
    specialties=["Radiology"],
    similarity=0.62,
)
KNEE_SHOWN = ServiceContext(
    name="Knee X-Ray",
    department="Radiology",
    description="Standard imaging of the knee joint.",
    preparation="Wear loose clothing.",
)


def _key(
    question: str = "knee x-ray", context: ServiceContext = KNEE_SHOWN, **kw
) -> str:
    found = [(kw.get("service", KNEE), context)]
    return answer_key(question, found, kw.get("model", "fake-llm-v1"))


def test_normalising_ignores_case_spacing_and_a_closing_mark() -> None:
    assert normalise("  Which   SERVICE for a Knee X-Ray?! ") == (
        "which service for a knee x-ray"
    )


def test_the_same_question_about_the_same_services_has_one_key() -> None:
    assert _key("knee x-ray") == _key("Knee  X-Ray?")


def test_the_key_changes_with_anything_the_reply_depends_on() -> None:
    """A different question, service, description, preparation note or
    model each give a new key, so the old reply is never found."""
    base = _key()
    other_service = RetrievedService(
        service_id=4,
        name="Knee X-Ray",
        department="Radiology",
        specialties=["Radiology"],
        similarity=0.62,
    )
    changed = [
        _key("knee mri"),
        _key(service=other_service),
        _key(
            context=ServiceContext(
                "Knee X-Ray", "Radiology", "Both knees.", "Wear loose clothing."
            )
        ),
        _key(
            context=ServiceContext(
                "Knee X-Ray", "Radiology", KNEE_SHOWN.description, "Fast first."
            )
        ),
        _key(model="another-model"),
        answer_key("knee x-ray", [], "fake-llm-v1"),
    ]
    assert base not in changed
    assert len(set(changed)) == len(changed)


def test_the_key_holds_none_of_the_questions_words() -> None:
    key = _key("which service for my knee x-ray")

    assert key.startswith("ai-answer:")
    assert "knee" not in key


def test_every_version_a_cached_reply_can_come_from_is_in_the_key() -> None:
    """If the code moves to a new prompt version, this fails until the
    version is listed, so old replies stop being served."""
    versions = {
        build_safety_prompt("q", clinic="MediNova").version,
        build_navigation_prompt("q", [KNEE_SHOWN], clinic="MediNova").version,
        refusal_reply([KNEE]).version,
        refusal_reply([]).version,
        no_match_reply(clinic="MediNova").version,
    }

    assert versions <= set(CACHED_PROMPT_VERSIONS)


def test_an_entry_reads_back_as_it_was_written() -> None:
    for intent in (AssistantIntent.NAVIGATION, None):
        entry = CachedAnswer(
            text="Book the Knee X-Ray.",
            outcome=InteractionOutcome.ANSWERED,
            intent=intent,
            model="fake-llm-v1",
            safety_prompt_version="PROMPT_SAFETY_V1",
            prompt_version="PROMPT_NAV_V1",
        )

        assert CachedAnswer.from_json(entry.to_json()) == entry


class _RefusingRedis:
    """A Redis client whose every write fails, with the URL in the error
    text, as redis-py's messages can carry it."""

    def set(self, *args: object, **kwargs: object) -> None:
        raise redis.ConnectionError(
            "could not connect to redis://app:hunter2@redis:6379/0"
        )


def test_a_failed_store_is_logged_by_type_and_never_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Redis failing after the lookup must not fail a question that has
    already been answered."""
    caplog.set_level(logging.WARNING, logger="app.services.answer_cache")
    entry = CachedAnswer(
        text="Book the Knee X-Ray.",
        outcome=InteractionOutcome.ANSWERED,
        intent=AssistantIntent.NAVIGATION,
        model="fake-llm-v1",
        safety_prompt_version="PROMPT_SAFETY_V1",
        prompt_version="PROMPT_NAV_V1",
    )

    store(_RefusingRedis(), "ai-answer:abc", entry)

    assert caplog.messages == [
        "answer cache store failed: redis unavailable (ConnectionError)"
    ]
    assert "hunter2" not in caplog.text
