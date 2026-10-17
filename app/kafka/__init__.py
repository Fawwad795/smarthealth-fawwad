"""Everything that talks to a broker.

The boundary this package draws is deliberate. app/events/ knows how to
*name* an event and write it into outbox_events inside the transaction
that caused it; it imports no Kafka client at all. This package is the
other half: the relay drains that table and publishes, the consumer reads
back, dedupes and applies. Nothing in app/services/ or app/temporal/
imports from here -- they record an event and are done, which is the
whole point of the outbox.

Named for the transport rather than for a role, because the transport is
what these six modules have in common: swap Kafka out and this is the
package that changes.
"""
