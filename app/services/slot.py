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


def reserve_slot_uncommitted(db: Session, slot_id: int) -> bool:
    """The atomic UPDATE itself, without committing.

    Split out from reserve_slot() so the scheduling saga's Activity (task
    2.9) can combine it with its own slot_reservations insert in one
    transaction -- committing here separately would leave a crash window
    between "slot flipped" and "reservation recorded" that defeats the
    whole point of that table.
    """
    result = db.execute(
        update(Slot)
        .where(Slot.id == slot_id, Slot.status == SlotStatus.AVAILABLE)
        .values(status=SlotStatus.RESERVED, updated_at=func.now())
        .returning(Slot.id)
    )
    return result.first() is not None


def reserve_slot(db: Session, slot_id: int) -> bool:
    """Atomically move one slot from AVAILABLE to RESERVED, and commit.

    The entry point for anything that just wants a slot held with no
    further bookkeeping -- see reserve_slot_uncommitted's docstring for
    why the saga's Activity uses that one directly instead.
    """
    won = reserve_slot_uncommitted(db, slot_id)
    db.commit()
    return won
