"""The platform's published rate limits for API 2.0, in one place.

These numbers are facts about the platform, stated in its public rate
limit documentation, so they live here rather than inside the fake or the
pacer: both sides of the contract (the fake that enforces them and the
pacer that respects them) import the same values, and a documentation
change is a one-line edit that both sides inherit.

The RUNBOOK carries the step that confirms them against a live workspace's
response headers, because a published number and a deployed number are two
different claims.
"""

from __future__ import annotations

BURST_LIMIT = 100
"""Requests per burst window per resource (location), as published."""

BURST_WINDOW_SECONDS = 10.0
"""The burst window length, as published: 100 requests per 10 seconds."""

DAILY_LIMIT = 200_000
"""Requests per day per resource (location), as published."""
