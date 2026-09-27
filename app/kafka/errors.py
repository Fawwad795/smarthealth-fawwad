"""Errors shared by the event pipeline."""


class PermanentEventError(Exception):
    """A message that cannot succeed however many times it is retried.

    Separated from every other exception because the two need opposite
    handling. A transient failure must not advance the offset; a
    permanent one *must*, or one bad message blocks its partition
    forever. The offset is a position, not a checklist -- there is no way
    to accept the next message while leaving this one outstanding.
    """
