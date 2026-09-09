"""Visit response schema"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import VisitStatus


class VisitResponse(BaseModel):
    """One visit's current state.

    There is no request schema: each transition is a bare POST to its own
    route with nothing for the caller to supply. The appointment id is in
    the path, and both timestamps are the server's to set -- letting a
    client send checked_in_at would let it forge the wait-time metric
    Week 3 computes from it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    appointment_id: int
    status: VisitStatus
    checked_in_at: datetime
    completed_at: datetime | None
