"""The assistant's answer cache (task 5.12): a question asked again gets the
reply already written for it, with no model call.

Only replies that depend on nothing about the patient are cached: answers
to navigation and other questions, refusals, and "we don't offer that". A
preparation or availability answer is built from the caller's own
appointments and the slots free right now, so it is never stored. The cache
is shared by every patient: the same question about the same services gets
the same reply.

- key: ai-answer:{sha256 of everything the reply depends on}: the question,
  normalised; each service found, with what the prompt shows of it; the
  prompt versions; the model; and the clinic name.
- TTL: settings.ai_answer_cache_ttl_seconds, an hour. A hit does not extend
  it, so a cached reply is never more than an hour old.
- invalidated by: a change to anything in the key, which gives a new key.
  An unpublished service drops out of retrieval's results, and an edited
  one shows the prompt something new, so the old entry is never found
  again and expires.

A Redis failure never fails a question: the lookup is logged, counted and
treated as a miss, and a failed store is logged and dropped.
"""

import hashlib
import json
import logging
from dataclasses import asdict, dataclass

from redis import Redis, RedisError

from app.ai.prompts import (
    NO_MATCH_V1,
    PROMPT_NAV_V1,
    PROMPT_SAFETY_V1,
    REFUSAL_NO_SERVICE_V1,
    REFUSAL_V1,
    ServiceContext,
)
from app.ai.retrieval import RetrievedService
from app.core.config import settings
from app.core.metrics import ai_answer_cache
from app.models.enums import AssistantIntent, InteractionOutcome

logger = logging.getLogger(__name__)

_KEY_PREFIX = "ai-answer:"

# Every prompt and fixed reply a cached reply can come from. They are part
# of the key, so a new version of one starts a fresh cache. A test fails if
# the code moves to a version not listed here.
CACHED_PROMPT_VERSIONS = (
    PROMPT_SAFETY_V1.version,
    PROMPT_NAV_V1.version,
    REFUSAL_V1.version,
    REFUSAL_NO_SERVICE_V1.version,
    NO_MATCH_V1.version,
)


class CacheUnavailable(Exception):
    """Redis could not be reached for a lookup. Raised so the caller skips
    the store too: each attempt waits out the same failure."""


@dataclass(frozen=True)
class CachedAnswer:
    """A reply as cached: its text, how it ended, and what wrote it, so a
    hit records the same intent, versions and model as the first answer."""

    text: str
    outcome: InteractionOutcome
    intent: AssistantIntent | None
    model: str
    safety_prompt_version: str
    prompt_version: str

    def to_json(self) -> str:
        """The entry as Redis stores it."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "CachedAnswer":
        """An entry read back from Redis."""
        data = json.loads(raw)
        return cls(
            text=data["text"],
            outcome=InteractionOutcome(data["outcome"]),
            intent=AssistantIntent(data["intent"]) if data["intent"] else None,
            model=data["model"],
            safety_prompt_version=data["safety_prompt_version"],
            prompt_version=data["prompt_version"],
        )


def normalise(question: str) -> str:
    """The question as the key sees it: letter case, spacing and a closing
    "?", "!" or "." do not change what is being asked."""
    return " ".join(question.casefold().split()).rstrip("?!. ")


def answer_key(
    question: str,
    found: list[tuple[RetrievedService, ServiceContext]],
    model: str,
) -> str:
    """The cache key for `question`, given the services retrieval found.

    Hashed, so the key holds none of the question's words. sort_keys makes
    the same content always give the same JSON, and so the same hash.
    """
    content = {
        "question": normalise(question),
        "services": [
            [
                service.service_id,
                context.name,
                context.department,
                context.description,
                context.preparation,
            ]
            for service, context in found
        ],
        "prompts": CACHED_PROMPT_VERSIONS,
        "model": model,
        "clinic": settings.assistant_clinic_name,
    }
    digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
    return _KEY_PREFIX + digest


def lookup(redis_client: Redis, key: str) -> CachedAnswer | None:
    """The cached reply for `key`, or None.

    Raises CacheUnavailable, after logging and counting it, if Redis cannot
    be reached.
    """
    try:
        raw = redis_client.get(key)
    except RedisError as err:
        # The error's type only: its message can carry the Redis URL.
        logger.warning(
            "answer cache lookup failed: redis unavailable (%s)", type(err).__name__
        )
        ai_answer_cache.labels("error").inc()
        raise CacheUnavailable from None
    if raw is None:
        ai_answer_cache.labels("miss").inc()
        return None
    ai_answer_cache.labels("hit").inc()
    return CachedAnswer.from_json(raw)


def store(redis_client: Redis, key: str, answer: CachedAnswer) -> None:
    """Cache `answer` under `key` for the TTL.

    nx: an entry already there is left alone, with its TTL. Two patients
    asking at once both miss and both store the same reply; the second
    store must not restart the clock.
    """
    try:
        redis_client.set(
            key, answer.to_json(), ex=settings.ai_answer_cache_ttl_seconds, nx=True
        )
    except RedisError as err:
        logger.warning(
            "answer cache store failed: redis unavailable (%s)", type(err).__name__
        )
