"""Business rules for Slot availability.

One function today: reserving a slot without a race condition. Everything
else about slots (generation) already lives in provider_schedule.py --
this module is where slot *state transitions* go, starting with the one
Week 2 is built around.
"""

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.models import Slot
from app.models.enums import SlotStatus


def reserve_slot(db: Session, slot_id: int) -> bool:
    """Atomically move one slot from AVAILABLE to RESERVED.

    One UPDATE, not a SELECT followed by an UPDATE: the "is it still
    available" check and the write happen as a single statement Postgres
    treats as indivisible, so two concurrent callers can never both see
    AVAILABLE and both proceed. Whichever call reaches Postgres second
    finds the row no longer matches `status == AVAILABLE` and updates
    zero rows -- there is no gap between checking and acting for a second
    caller to land in.

    Returns True if this call won the reservation, False if it didn't
    (already taken, or slot_id doesn't exist -- callers that need to tell
    those apart should look the slot up first). updated_at is set
    explicitly rather than left to TimestampMixin's onupdate: this
    statement's correctness must not depend on that being applied, so it
    is set the same as any other column this UPDATE touches.
    """
    result = db.execute(
        update(Slot)
        .where(Slot.id == slot_id, Slot.status == SlotStatus.AVAILABLE)
        .values(status=SlotStatus.RESERVED, updated_at=func.now())
        .returning(Slot.id)
    )
    won = result.first() is not None
    db.commit()
    return won
