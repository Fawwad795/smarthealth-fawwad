"""Domain events: the event catalogue, the envelope, and the outbox.

Nothing in this package talks to Kafka -- not as a convention but
structurally: no module here imports a broker client. Recording an event
is a database write, and app/kafka/ is the half that turns those rows
into published messages. That is what keeps the seven emit points in
app/services/ and app/temporal/ free of broker concerns -- see
docs/events.md.
"""
