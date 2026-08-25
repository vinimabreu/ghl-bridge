"""Deterministic stand-ins that ship as part of the product.

The fakes are not test scaffolding. They model the documented semantics of
the platform (location scoping, upsert matching, ordered pipeline stages,
double-booking refusal, the published rate limits) so the demo, the test
suite and any integrator's rehearsal can run the full bridge with no
account, no card and no key, then swap in the live adapter without touching
the calling code.
"""

from .clock import FakeClock
from .highlevel import FakeHighLevel

__all__ = [
    "FakeClock",
    "FakeHighLevel",
]
