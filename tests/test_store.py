"""The durability seam itself: small, and the contract is the point."""

from __future__ import annotations

from ghl_bridge import InMemoryKeyStore


def test_an_unknown_key_answers_none() -> None:
    assert InMemoryKeyStore().get("ghost") is None


def test_a_set_key_answers_its_value() -> None:
    store = InMemoryKeyStore()
    store.set("id:evt-1", "dlv-1")
    assert store.get("id:evt-1") == "dlv-1"


def test_set_overwrites() -> None:
    store = InMemoryKeyStore()
    store.set("k", "first")
    store.set("k", "second")
    assert store.get("k") == "second"


def test_two_stores_share_nothing() -> None:
    a = InMemoryKeyStore()
    b = InMemoryKeyStore()
    a.set("k", "v")
    assert b.get("k") is None
