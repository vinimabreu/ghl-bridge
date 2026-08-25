# RUNBOOK: connecting ghl-bridge to a live workspace

Everything in this repository runs and proves itself offline. This file is
the honest remainder: the exact steps that connect the same code to a real
GoHighLevel sub-account on the day one exists, and the checks that confirm
the offline model against the live platform before anything touches a real
customer. Work through it top to bottom; each step names what it verifies.

## 1. Issue a Private Integration token

1. In the sub-account (location), open Settings, then Private Integrations.
2. Create an integration and select the scopes this bridge uses:
   contacts (read and write), opportunities (read and write), conversations
   (read and send), calendars (read and write events).
3. Copy the token once and store it in a secret manager. It is scoped to
   this one location; that scoping is exactly what `FakeHighLevel` models
   and what `CrossLocationDenied` reports.

Record: the location id (Settings, Business Profile) and the token.

## 2. Smoke the read path

```python
from ghl_bridge.live import LiveHighLevel, RequestsTransport

transport = RequestsTransport(token="<the token>")
adapter = LiveHighLevel(location_id="<the location id>", transport=transport)
print(adapter.list_pipelines("<the location id>"))
```

Confirms: the base URL, the bearer header, the `Version` header for the
opportunities family, and the pipeline response shape
(`pipelines[].stages[].id/name/position`). If the stage list comes back
empty or misshapen, compare the raw response against
`ghl_bridge.live.mapping.parse_pipelines` before touching anything else.

## 3. Verify the three thin spots

These are the places the public docs left room for doubt; each has the
doubt written into its docstring.

1. **Contact lookup** (`plan_search_contacts`): the adapter encodes
   `GET /contacts/` with `locationId` and `query`. If the workspace
   answers 404 or deprecation, switch the single function to the
   documented `POST /contacts/search` filter body and rerun
   `tests/test_live_mapping.py` after updating the recorded shapes.
2. **Free slots** (`parse_free_slots`): the docs show slots grouped under
   date keys with ISO starts and no slot length. Fetch one real calendar
   day and diff the grouping; the slot length comes from the calendar's
   configured duration.
3. **Webhook signature**: the marketplace documents a platform-signed
   header for app webhooks. Capture one real delivery, confirm the header
   name and scheme, and implement it as a `SignatureScheme` beside
   `HmacSha256Scheme`; the intake takes whichever is injected. For a
   private setup fronted by a relay that signs with a shared secret, the
   shipped HMAC scheme is already the right one.

## 4. Smoke the write path, on synthetic data only

Use an obviously synthetic contact (a `.example` email, a reserved
555-01xx number). In order:

1. `upsert_contact` twice with case-variant emails; confirm one contact.
2. `create_opportunity` on a real pipeline and stage id from step 2;
   confirm placement in the UI.
3. `move_opportunity` to a second stage; then attempt a fabricated stage
   id and confirm the platform's error maps to `StageNotInPipeline` or
   surfaces as a clean typed error worth adding to the mapping.
4. `send_message` to the synthetic contact only after confirming the
   sub-account has no live phone number attached, or the channel is a
   test channel. A real send to a real person is not a smoke test.

## 5. Confirm the published rate limits

Read the rate headers on any successful response and compare against
`ghl_bridge.limits`. If the deployed numbers differ from the published
ones, change that one module; the fake and the pacer both inherit it.

## 6. Point the bridge at the live port

The wiring is the same as the demo with two lines changed: the fake port
becomes `LiveHighLevel` behind `PacedPort`, and the webhook intake moves
behind whatever HTTPS endpoint receives the deliveries. Keep the ledger.
The first week's most valuable output is `explain_message` on every send
a human asks about.

## 7. Before real traffic

- Run the full offline suite against the exact commit being deployed.
- Set the gate's covered intents deliberately; the default set is the
  demo's, not a recommendation.
- Decide who the named human approvers are. The release path requires a
  name and the ledger keeps it; that is a feature to preserve, not
  friction to remove.
