"""Stand-ins for confluent_kafka's Consumer and Message.

The consumer loop is the first infinite loop in this project, and testing
it against a real broker would make the suite need Kafka running -- which
is both slow and the network dependency this morning's work removed. These
fakes hand the loop a fixed list of messages and then stop it, so a test
asserts on what the loop *did* (which offsets it committed, which it
rewound) rather than on what a broker happened to deliver.
"""

import threading
from typing import Any


class FakeMessage:
    """One Kafka message. Method-style accessors, as confluent_kafka has.

    confluent_kafka's Message exposes value()/topic()/offset() as calls,
    not attributes, so the fake must too -- an attribute-based fake would
    pass tests that the real client would break.
    """

    def __init__(
        self,
        value: bytes | None = None,
        *,
        topic: str = "app.visits",
        partition: int = 0,
        offset: int = 0,
        error: Any = None,
    ) -> None:
        self._value = value
        self._topic = topic
        self._partition = partition
        self._offset = offset
        self._error = error

    def value(self) -> bytes | None:
        """The raw message body, as produced."""
        return self._value

    def topic(self) -> str:
        """The topic it arrived on."""
        return self._topic

    def partition(self) -> int:
        """The partition within that topic."""
        return self._partition

    def offset(self) -> int:
        """This message's position in the partition."""
        return self._offset

    def error(self) -> Any:
        """A broker-level notice, or None for a real message."""
        return self._error


class FakeConsumer:
    """Serves a fixed list of messages, then stops the loop.

    Records every commit() and seek() instead of performing them: those
    two calls are the entire externally visible behaviour of the loop, so
    recording them is what makes "did this message advance the offset?"
    an assertable question.
    """

    def __init__(self, messages: list[FakeMessage], stop: threading.Event) -> None:
        self._messages = list(messages)
        self._stop = stop
        self.committed: list[int] = []
        self.seeks: list[int] = []

    def poll(self, timeout: float) -> FakeMessage | None:
        """Hand over the next message, or stop the loop once they run out."""
        if not self._messages:
            self._stop.set()
            return None
        return self._messages.pop(0)

    def commit(self, msg: FakeMessage) -> None:
        """Record that the loop advanced the bookmark past this message."""
        self.committed.append(msg.offset())

    def seek(self, partition: Any) -> None:
        """Record that the loop rewound to re-read a message."""
        self.seeks.append(partition.offset)
