"""The durability seam for the two facts that must survive a restart.

Two small sets of keys decide whether this bridge repeats itself: the
webhook intake's seen-event keys (forget one and a sender retry re-runs an
already-applied event) and the sender's spent-approval ids (forget one and
a replayed approval sends the same message twice). Both live behind
:class:`KeyStore`, injected, with :class:`InMemoryKeyStore` as the
default.

The default is honest about what it is: process-local and gone on
restart. That is correct for the suite, the demo, and a single process
that accepts redelivery-on-restart; it is not correct for real traffic.
The README's "What this is not" section and the RUNBOOK both carry the
instruction: back these two seams with something durable (a database
table, a key-value service) before a customer is on the other end. The
protocol is two methods precisely so that takes minutes, not a redesign.
"""

from __future__ import annotations

from typing import Protocol


class KeyStore(Protocol):
    """Remember a value under a key; answer what was remembered.

    ``get`` returns None for an unknown key. ``set`` overwrites. That is
    the whole contract, and both callers in this package treat a stored
    key as the fact ("this event completed", "this approval was spent")
    and the value as its context (the delivery id, the message id).
    """

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...


class InMemoryKeyStore:
    """The default store: a dict, process-local, gone on restart."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value
