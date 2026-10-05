"""claim_event: first delivery wins, and the loser leaves no wreckage."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.events.dedupe import claim_event


def test_the_first_claim_succeeds_and_the_second_does_not(
    db_session: Session,
) -> None:
    """The whole contract, in two calls."""
    assert claim_event(db_session, "evt-a", "app-analytics") is True
    assert claim_event(db_session, "evt-a", "app-analytics") is False


def test_each_consumer_claims_the_same_event_independently(
    db_session: Session,
) -> None:
    """Idempotency is per-consumer -- the reason consumer is in the key."""
    assert claim_event(db_session, "evt-b", "app-analytics") is True
    assert claim_event(db_session, "evt-b", "app-reporting") is True


def test_a_rejected_claim_leaves_the_transaction_usable(
    db_session: Session,
) -> None:
    """The reason this is ON CONFLICT DO NOTHING and not a caught
    IntegrityError.

    A violated constraint aborts the transaction, and every later
    statement in it -- including the handler's own writes -- would be
    refused. This test is what would catch someone "simplifying" the
    implementation back to a try/except: the claim would still return
    False, and this statement would raise instead of returning 1.
    """
    claim_event(db_session, "evt-c", "app-analytics")
    claim_event(db_session, "evt-c", "app-analytics")

    assert db_session.execute(text("SELECT 1")).scalar_one() == 1
