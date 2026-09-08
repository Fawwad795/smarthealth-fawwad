"""Domain events: the envelope, the event catalogue, and the outbox.

Nothing in this package talks to Kafka. Publishing happens from the outbox
table, which is what keeps the six emit points free of broker concerns --
see docs/events.md.
"""
