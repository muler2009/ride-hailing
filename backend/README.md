# Ride-Hailing Platform — Backend (Phases 1–14)

Phase 1 (Identity & Users), Phase 2 (Driver Onboarding & Vehicles), Phase 3
(Ride Creation & the Ride State Machine), Phase 4 (Driver Availability &
Dispatch), Phase 5 (Real-Time Location Tracking), Phase 6 (Maps &
Routing), Phase 7 (Pricing), Phase 8 (Payments), Phase 9 (Driver
Earnings & Wallet), Phase 10 (Notifications), Phase 11 (Ratings &
Reviews), Phase 12 (Admin Dashboard), Phase 13 (Analytics & Reporting),
and Phase 14 (Production Deployment & Kubernetes) from the roadmap — the
full roadmap, start to finish. Each phase built as separate Django apps
under `apps/`, per the domain boundaries in the architecture doc; Phase 14
is the exception, since deployment isn't a bounded context inside the
application — see `../infrastructure/` (a sibling of this `backend/`
directory) for everything it added.

**Authentication is delegated to Keycloak (OIDC).** Authorization (RBAC)
remains entirely local. **Requesting a ride does not require an
account** (Phase 3). **Matching a ride to a driver is fully automatic**
(Phase 4). **Riders and drivers see each other move in real time over
WebSocket** (Phase 5). **Ride distance/duration is a real routing
calculation** (Phase 6) **with a real, itemized, Decimal-exact fare**
(Phase 7). **A ride can actually be paid for** — cash, card, or mobile
money — idempotently (Phase 8). **The moment a ride is paid, the driver
is automatically credited** into a ledger-backed wallet (Phase 9).
**Every significant event now notifies the people involved**, delivered
asynchronously via Celery so nothing blocks the request that triggered
it (Phase 10). **Both parties can rate each other afterwards**, which
finally drives the driver reputation score dispatch has been reading
since Phase 4 (Phase 11). **Operators get aggregate dashboards, a live
ride map, and an immutable audit trail** of every administrative action
(Phase 12). **A daily Celery job turns closed days into permanent
historical rollups**, so a trend chart over months reads a handful of
pre-summed rows instead of re-scanning raw tables on every request
(Phase 13). **The whole thing now runs as a horizontally-scaled,
zero-downtime-deployable Kubernetes service** — structured logging,
Prometheus metrics, and the liveness/readiness/graceful-shutdown chain
that makes a rolling deploy safe (Phase 14).

## What's implemented

### Phase 1 — Identity & Users
- **Keycloak-backed authentication**: registration provisions a user in
  Keycloak (Admin API) and mirrors a local profile row; login proxies
  email+password to Keycloak's token endpoint; every authenticated request
  is verified against Keycloak's public signing keys
  (`apps/identity/keycloak_client.py`, `apps/identity/authentication.py`)
- **Local RBAC, unchanged in shape**: `Role`, `Permission`, `UserRole`,
  `RolePermission` models, with the seven default roles and ten
  permissions from the SRS seeded via a data migration
  (`apps/identity/migrations/0003_seed_rbac.py`) — also re-runnable via
  `python manage.py seed_rbac`
- `apps/identity/services.py` — the single function (`user_has_permission`)
  that answers "can this user do X"; `HasPermission` is the only DRF
  permission class views use
- **JIT (just-in-time) user provisioning**: the first time a verified
  Keycloak token is seen for a given Keycloak user id, a local `User` row
  is created automatically and any realm roles present in the token that
  match a local Role name are assigned (additively)
- Registration, login, token refresh, `/users/me/`, and a permission-gated
  admin `/users/` list endpoint (requires `user.manage`)

### Phase 2 — Driver Onboarding & Vehicles
- **Driver applications**: a user with the `Driver` role submits license
  details; the application starts `PENDING_REVIEW`
- **KYC document upload**: driver's license, background check, and
  profile photo documents, one per type (re-uploading replaces the prior
  file and resets it to pending review)
- **Enforced approval state machine** (`apps/drivers/services.py`) —
  `PENDING_REVIEW → APPROVED | REJECTED`, `APPROVED → SUSPENDED`,
  `SUSPENDED → APPROVED`, `REJECTED → PENDING_REVIEW` (on resubmission).
  No other transition is possible; invalid ones raise a clean `400`, never
  a server error
- **Admin approval workflow**, gated on the same `driver.approve` /
  `driver.suspend` permissions seeded in Phase 1 — approve, reject (with
  reason), suspend (with reason), reactivate, and a filterable review
  queue
- **Vehicle registration**: `VehicleType` reference data (Moto, Sedan,
  SUV, Van — seeded via migration), vehicle registration with a unique
  license plate, and compliance document upload (registration, insurance,
  inspection)
- **Single-active-vehicle rule**: a driver's first vehicle is active by
  default; registering more leaves them inactive until explicitly
  activated, which deactivates all others for that driver
  (`apps/vehicles/services.py:set_active_vehicle`) — this is a multi-row
  invariant enforced in the service layer, not by a field default alone

### Platform-wide conventions (both phases)
- Consistent error envelope, pagination, and `/api/v1/` URL versioning
- 56 automated tests: RBAC, real JWT signature/issuer/expiry verification
  (against a self-signed token standing in for a Keycloak-issued one),
  claims-sync/JIT-provisioning, registration, login, refresh, permission
  allow/deny, the driver approval state machine (valid and invalid
  transitions), and the vehicle single-active rule — all runnable without
  a live Keycloak server (see "Testing without a live Keycloak" below)

### Phase 3 — Ride Creation & the Ride State Machine
- **No account required to request a ride.** An unauthenticated caller
  supplies a phone number (`guest_phone_number`) and gets back a
  `tracking_token` — a bearer credential for checking status or cancelling
  afterwards, the same pattern as a package-tracking or guest-checkout
  number. A registered rider (with the `ride.request` permission) rides as
  themselves instead; the two paths share one endpoint, one serializer,
  and one `Ride` table, distinguished by a DB `CheckConstraint` that
  enforces "exactly one of `rider` or `guest_phone_number`, never both,
  never neither"
- **The full ride state machine** (`apps/rides/services.py`) — every
  status from the architecture doc's diagram, one explicit transition
  table, one enforcement point (`_transition()`). Invalid transitions
  (starting a trip before arrival, completing before starting, assigning
  an unapproved driver, a second driver trying to act on someone else's
  assigned ride, cancelling after the trip has started, ...) all raise a
  clean `400`/`403`, never corrupt state or 500
- **Append-only status history** — every transition, including the two
  automatic "system" ones (`REQUESTED → SEARCHING_DRIVER` at creation,
  `TRIP_COMPLETED → PAYMENT_PENDING` at trip end), gets its own
  `RideStatusHistory` row
- **Manual dispatch stand-in**: real matching is Phase 4, so an Operations
  Manager (`ride.assign` permission) manually assigns an *approved* driver
  to a `SEARCHING_DRIVER` ride. Every downstream driver action (accept,
  decline, arriving, arrived, start, complete) is unaffected by how the
  assignment happened, and will work identically once Phase 4 replaces
  this manual step with real matching
- **Ownership enforced everywhere, not just state**: a driver can only act
  on a ride they're assigned to; a rider can only cancel their own ride; a
  guest can only act via the exact `tracking_token` issued to them
- **`ride.manage`** (Operations Manager, Support Agent, Super Admin) for
  read-only monitoring of any ride — the `tracking_token` is never
  included in that view, since it's the guest's own access credential, not
  general ride metadata
- 41 new automated tests: guest and registered-rider creation, the full
  happy-path lifecycle, every documented invalid transition, ownership
  checks (wrong driver, wrong rider, wrong guest phone), and permission
  boundaries on the admin/driver action endpoints

### Phase 4 — Driver Availability & Dispatch
- **Real geographic matching**, not the Phase 3 manual stand-in: an
  online/offline toggle (`POST /drivers/me/availability/`) and a location
  feed (`POST /drivers/me/location/`) write to **Redis** — a GEO set for
  proximity queries plus a short-TTL freshness key per driver, so a driver
  who stops sending updates silently ages out of dispatch candidacy
  without needing an explicit "went offline" signal. Current location is
  never written to Postgres, per the architecture doc's rule against
  persisting every GPS-adjacent update
- **Candidate filtering**: online, approved, not already offered this
  ride, not already on another active ride, with an active vehicle
  matching the requested type, within a 5km radius with a fresh location —
  ranked by distance first, then rating and acceptance-rate as tie-breakers
- **Simultaneous multi-offer dispatch with a real race condition, and a
  real fix**: up to 3 nearby candidates get offered a ride at once; the
  first to accept wins, and every other pending offer for that ride is
  immediately superseded. The winning accept and the ride's
  `SEARCHING_DRIVER → DRIVER_ASSIGNED → DRIVER_ACCEPTED` transition happen
  in one `select_for_update`-locked step, so a second driver's accept
  request — even arriving at nearly the same instant — finds the ride no
  longer available and fails cleanly. This is verified with a genuine
  multi-threaded test (`DispatchOfferAndAcceptRaceTests`, using real
  Python threads and separate DB connections), not just a sequential
  simulation
- **Dispatch proposes, Ride Management decides**: `apps/dispatch` only
  ever creates/updates `DispatchOffer` rows; the actual `Ride.status`
  write on acceptance still happens inside `apps/rides/services.py`,
  preserving the same single-enforcement-point rule Phase 3 established
- **The periodic dispatch cycle** (`python manage.py run_dispatch_cycle`,
  also callable via `POST /admin/dispatch/run-cycle/`) expires overdue
  offers, (re)dispatches any `SEARCHING_DRIVER` ride with no live offers,
  and marks a ride `NO_DRIVER_FOUND` once it's been searching longer than
  the configured window with no eligible candidates left. This stands in
  for a Celery-beat periodic task, which Phase 5+ can wire up without
  changing any of this logic — see the note in that management command
- **The Phase 3 manual `assign-driver` admin action still exists**, now
  as an ops override that supersedes any of automatic dispatch's pending
  offers when used — useful for edge cases, not the normal path anymore
- 28 new automated tests, run against a **real Redis instance** (not
  mocked) and including one genuinely concurrent multi-threaded test:
  geo store correctness, candidate filtering (each exclusion rule tested
  individually), the offer/accept/decline lifecycle, the periodic cycle's
  two outcomes, and a full guest-ride-to-driver-acceptance test with no
  admin involved anywhere in the path

### Phase 5 — Real-Time Location Tracking
- **The `apps/dispatch`-owned Redis geo store moved to a new `apps/locations`
  app**, matching the architecture doc's separate "Location Tracking"
  bounded context — dispatch now depends on locations for candidate
  search, not the other way around
- **WebSocket ride tracking** via Django Channels + Daphne, backed by the
  same Redis instance as a channel layer. Two routes share one consumer:
  `ws/rides/<id>/track/` for a registered rider/driver/admin, and
  `ws/rides/track/<tracking_token>/` for a guest — no login, same as the
  REST guest endpoints. Authorization mirrors the REST rules exactly (own
  rider, assigned driver, or `ride.manage`)
- **Custom Keycloak WebSocket auth** (`apps/locations/ws_auth.py`):
  browsers can't attach a custom `Authorization` header to a WebSocket
  handshake, so the access token travels as a query parameter
  (`?token=...`) instead — verified through the exact same
  `verify_access_token`/JIT-provisioning path as every REST request
- **Throttled ingestion**: `POST /drivers/me/location/` now rejects a
  second update from the same driver within 3 seconds (`429 Too Many
  Requests`) using an atomic Redis `SET NX EX`, not a read-then-write
  check that could itself race
- **Live broadcast, not just storage**: every accepted location update
  is pushed immediately to any WebSocket client tracking that driver's
  active ride. Every ride status transition is *also* pushed, hooked
  directly into `apps/rides/services.py`'s `_transition()` — the single
  enforcement point from Phase 3 — via `transaction.on_commit`, so a
  broadcast never fires for a transition that ends up rolling back
- **Sampled persistence, not a raw log**: a `DriverLocation` snapshot is
  saved roughly every 30 seconds per ride (not on every 3-5 second ping),
  for trip playback and dispute review — a new `GET /rides/{id}/locations/`
  endpoint exposes that history to the ride's own rider, driver, or an
  admin
- 15 new automated tests: throttling, sampled-vs-skipped persistence
  decisions, the location-history endpoint's access control, and four
  WebSocket consumer tests run against a **real Redis-backed channel
  layer** using Channels' `WebsocketCommunicator` — including one that
  drives a real `mark_arriving()` call and asserts the status broadcast
  actually arrives over the socket
- Also verified with a genuine live server: `manage.py runserver` (now
  Daphne-backed) serving both a real HTTP request and a real WebSocket
  connection — including a correct `403` rejection for an unknown
  tracking token — not just through the Django test client

### Phase 6 — Maps & Routing
- **The `MapProvider` abstraction** from the architecture doc: geocode,
  reverse-geocode, and get-route, each provider hiding its own API shape
  entirely. No calling code anywhere imports a specific provider — only
  `apps/maps/factory.py:get_map_provider()`
- **Two real implementations, not stubs**: `OSRMNominatimProvider` (the
  default — free, no API key, using the public Nominatim + OSRM services)
  and `GoogleMapsProvider` (requires `GOOGLE_MAPS_API_KEY`). Adding Mapbox
  is a third file following the same three-method interface — nothing
  else in the codebase would need to change
- **Public geocode/reverse-geocode/route endpoints** (`AllowAny`),
  matching the "no account required" philosophy already established for
  ride creation — a guest needs to search addresses and see an ETA before
  they can even submit a ride request
- **Ride creation now computes a real distance/duration estimate**,
  best-effort, the same tolerance pattern as Phase 4's dispatch attempt:
  if the map provider is down or unreachable, the ride is still created
  successfully with `estimated_distance_km`/`estimated_duration_minutes`
  left `null` rather than failing the request
- **Verified against a genuine network failure, not just a mock**: this
  sandbox's network egress proxy actually blocks the real Nominatim
  host, and hitting `/maps/geocode/` against a live server here returns a
  clean `422` with a readable message — proving the error-handling path
  works against a real failure, not just an assumed one
- 20 new automated tests: URL construction and response parsing for both
  providers against realistic fixture payloads (mocked at the `requests`
  boundary, since this sandbox can't reach either provider's real API),
  factory provider-selection, the public endpoints' auth-free access and
  input validation, and the ride-creation integration's success and
  failure-tolerance paths

### Phase 7 — Pricing
- **The full fare formula from the architecture doc**, computed with
  Decimal arithmetic end-to-end and never a float: base fare + distance +
  duration, surge applied only to that metered subtotal, waiting time and
  tolls added unsurged, a visible minimum-fare top-up if needed, tax, then
  discount — subtracted last and capped so a total can never go negative
- **A defensive Decimal boundary** (`_as_decimal()`): a `Ride`'s
  `estimated_distance_km` can still be a raw Python `float` in memory
  immediately after Phase 6 assigns it (before any DB round-trip converts
  it), and constructing a `Decimal` directly from a `float` reintroduces
  exactly the binary-representation imprecision the "never use
  floating-point for money" rule exists to prevent. Every pricing entry
  point coerces through `str()` first — verified by a test that
  deliberately leaves a raw float on the ride and confirms the fare still
  comes out exact
- **Every line item is quantized before the total is computed**, and the
  total is the sum of those already-quantized items — never a
  separately-rounded number that could drift by a cent from what the
  line items themselves add up to. Verified directly: `sum(line_items) ==
  total_amount`, exactly, on every calculation
- **A ride now gets a fare estimate automatically at creation** (chained
  onto Phase 6's route estimate, same best-effort tolerance — no pricing
  rule for that vehicle type doesn't block ride creation), **a final
  fare at trip completion**, and **a cancellation fee** if the rider
  cancels after the driver has already accepted (not before — declining
  an offer that hasn't been accepted yet costs nothing)
- **`PricingRule` management reuses the `pricing.manage` permission**
  seeded all the way back in Phase 3 for exactly this purpose — Operations
  Manager and Finance Admin can already update rates with no new RBAC
  wiring needed
- **Public pricing-rule and per-ride-fare endpoints**, continuing the
  established "no login needed to see what things cost" pattern; `PUT`
  to update a rule is the only piece that's permission-gated
- 26 new automated tests: the calculation engine's exactness and every
  documented rule (minimum-fare bump, surge scope, tax ordering, discount
  floor, zero-valued items omitted), persistence rules (ESTIMATE
  recalculable, FINAL/CANCELLATION immutable), the float-boundary guard,
  and the full ride-lifecycle integration (creation → estimate,
  completion → final fare, late cancellation → fee, early cancellation →
  no fee) — plus a live HTTP smoke test confirming the pricing-rules
  endpoint serializes Decimal values as exact strings (`"1.00"`), not
  floats

### Phase 8 — Payments
- **The `PaymentProvider` abstraction** from the architecture doc, same
  shape as Phase 6's `MapProvider`: one interface, real implementations
  behind it (`StripeProvider`, `ChapaProvider` — genuinely different
  provider models, Stripe charges synchronously while Chapa is a
  hosted-checkout gateway that completes later via webhook), never
  imported directly by calling code. Adding Telebirr is a third provider
  file; nothing else changes
- **Payment status is never set from anything a client asserts.** It
  changes in exactly three places: a signature-verified webhook, a driver
  explicitly confirming cash received, or an admin issuing a refund —
  never from a request body claiming "I paid"
- **Webhook signature verification is real, tested cryptography, not a
  mock.** Both providers' HMAC schemes are implemented from their public
  documentation (Stripe's `t=...,v1=...` timestamped scheme; Chapa's
  simpler payload HMAC) and tested by computing a genuine signature with
  a test secret and confirming it validates — and that a tampered payload
  or wrong secret is rejected. No network call is involved in verifying a
  signature, so this needed no mocking at all
- **Duplicate webhook delivery provably can't double-charge or
  double-record** — the exact race the architecture doc calls out by
  name. The guarantee isn't a check-then-act read (which itself races
  under concurrent delivery); it's a unique constraint on
  `PaymentTransaction.idempotency_key` that the database enforces, with
  the second delivery's `IntegrityError` caught and treated as an
  already-handled no-op. Directly tested: two identical webhook
  deliveries in a row produce exactly one `PaymentTransaction`, one ride
  transition to `PAID`, no duplicates
- **A real bug caught by the tests, not written correctly the first
  time**: the original `initiate_payment` wrapped the entire function
  (including a failed charge's status update) in one `@transaction.atomic`
  block — so when a charge failed, the rollback undid the very audit
  record meant to explain why. Fixed by giving the failure-path write its
  own transaction boundary, separate from the charge attempt that failed;
  a test asserting the `Payment` row exists with `status=FAILED` after a
  declined charge is what caught this
- **A payment's own initiation is idempotent too**, not just webhook
  processing: `Payment.ride` is one-to-one, so calling the initiate
  endpoint twice for the same ride returns the existing `Payment` rather
  than erroring or creating a second one
- **`PAYMENT_PENDING → PAID` is finally reachable.** That transition
  existed in the state machine's table since Phase 3 with a comment
  saying "not reachable through any endpoint yet — Payments (Phase 8)
  hasn't been built." It's `apps/payments` that now calls it, and only
  after a real, verified capture — never speculatively
- **Guest parity, as always**: a guest can initiate and check on their
  payment via `tracking_token`, the same as every other guest-facing
  action since Phase 3
- **`payment.refund` reuses the permission seeded all the way back in
  Phase 3** — Finance Admin can already issue refunds with no new RBAC
  wiring, full or partial, with a required reason for the audit trail
- 37 new automated tests: real signature verification and rejection for
  both providers, provider REST calls mocked (Stripe/Chapa APIs aren't
  reachable from this sandbox), the full cash and card lifecycle, the
  duplicate-webhook guarantee directly, refund rules (can't exceed the
  original amount, can't refund a payment that was never captured), and
  a live HTTP smoke test confirming a bad webhook signature and an
  unconfigured/unknown provider both fail cleanly with `400` rather than
  crashing

### Phase 9 — Driver Earnings & Wallet
- **`WalletLedgerEntry` is append-only, and the balance is never
  stored.** `get_wallet_balance()` is always `sum(ledger_entries.amount)`,
  computed fresh — exactly FR-EAR-03 ("derive a wallet balance as the sum
  of its ledger entries rather than storing a directly mutable balance
  field"), directly tested against a manually-computed sum
- **Crediting a driver's earning is idempotent against a retried
  trigger** (FR-EAR-05), the same database-level guarantee Phase 8 used
  for webhooks: `DriverEarning.ride` is one-to-one and
  `WalletLedgerEntry.idempotency_key` is unique, so calling
  `credit_driver_earning()` twice for the same ride returns the existing
  record rather than double-crediting — directly tested
- **The commission split is snapshotted per ride, not looked up fresh
  later.** `DriverEarning.commission_rate` captures the rate at the
  moment of the split, so a later change to `PLATFORM_COMMISSION_RATE`
  never retroactively changes what an already-completed ride's record
  says it paid out
- **A payout's debit reserves funds at request time**, under a
  `select_for_update` lock on the driver's `Wallet` — the same locking
  discipline used for ride assignment (Phase 4) and dispatch offers,
  applied here to prevent two concurrent payout requests from both
  draining the same balance. Verified with a genuine multi-threaded test
  (`ConcurrentPayoutRequestTests`, real threads and separate DB
  connections via `TransactionTestCase`, the same technique Phase 4's
  dispatch race test used), not a sequential simulation
- **A failed payout is reversed with a new credit entry, never by editing
  or deleting the original debit** — the append-only ledger rule applied
  to the one place in this phase where money conceptually needs to "come
  back"
- **A shared `apps/common/money.py` utility was extracted from Phase 7's
  pricing engine** once Phase 9 needed the exact same Decimal-quantization
  and float-boundary discipline — duplicating it a second time would have
  been the wrong call once two apps needed it identically; both
  `apps/pricing` and `apps/earnings` now import from the same place
- **`payout.manage` is a new permission** (Finance Admin, Super Admin by
  default), following the same migration-plus-management-command pattern
  every earlier phase's new permissions used
- **The `WALLET` payment method Phase 8 deliberately left out** of its
  `PaymentMethod` choices now has a real balance behind it — though
  actually wiring wallet balance as a payment source is still future
  work; Phase 9 only builds the earning/payout side of the wallet, not a
  "pay with wallet balance" flow yet
- 19 new automated tests: the commission split's exactness, the
  idempotent-credit guarantee, ledger-balance correctness, payout
  validation rules (minimum amount, insufficient balance, one pending
  payout at a time), the reversal-on-failure flow, and the genuine
  concurrent-payout race condition

### Phase 10 — Notifications
- **Genuinely asynchronous delivery via Celery** (FR-NOT-03), not
  fire-and-hope: `notify()` does only fast DB writes and enqueues, while
  the actual SMS/email/push provider calls happen in a worker process.
  Verified end to end for real — notifications created as `PENDING`,
  published to a live Redis broker, consumed by an actual `celery -A
  config worker` process, and confirmed `SENT` with timestamps in the
  database afterwards
- **Four channels with per-channel status tracking**: an event fanning
  out to email + SMS + in-app is three `Notification` rows, not one row
  with three flags — so an email bounce never hides that the SMS
  succeeded
- **Channel selection honors both preference and capability**: a channel
  is used only if the recipient has it enabled *and* has the contact
  info it needs (email address, phone number, or registered push token).
  A guest with no account only ever gets SMS, matching the
  registered-vs-guest split established back in Phase 3
- **SMS and push follow the same provider-abstraction pattern** as Maps
  (Phase 6) and Payments (Phase 8) — `TwilioProvider` and `FCMProvider`
  behind interfaces, reached only through a factory. Email deliberately
  has *no* custom abstraction: Django's own `EMAIL_BACKEND` setting
  already solves "swap the email transport," so reinventing it would
  have been redundant
- **Notification creation is idempotent** per (recipient, ride, event
  type, channel) — the same unique-constraint approach used for webhooks
  (Phase 8) and earnings (Phase 9), so a retried trigger never duplicates
- **Hooks wired into every significant existing event**: ride requested,
  driver assigned, arriving, arrived, trip started, trip completed,
  payment completed/failed, driver approved/suspended, payout completed
  — all best-effort, so a notification hiccup never unwinds the business
  event that triggered it
- **A real bug fixed, found by the full-suite run hanging**: with Celery
  wired in, every ride/payment/driver test across the whole suite began
  publishing to a live Redis broker, which made the suite both slow and
  dependent on broker availability (it timed out at 300s). Tests now
  default to eager mode (`CELERY_TASK_ALWAYS_EAGER` when `test` is in
  `sys.argv`) — standard Celery practice — bringing the full suite to
  **~5 seconds** and removing the broker dependency entirely. The suite
  now passes even with Redis down
- **The same `pagination_class = None` bug caught in Phase 9 was caught
  again here proactively** on `MyNotificationsListView`, before writing
  a single test against it
- 23 new automated tests: channel resolution under every
  preference/capability combination, idempotency, real email delivery
  through Django's locmem backend, SMS/push delivery success and failure
  paths (providers mocked — unreachable from this sandbox), the
  Celery-eager end-to-end path, and the ride/driver event hooks actually
  firing

### Phase 11 — Ratings & Reviews
- **`PAID → RATED` is finally reachable.** That was the last entry in the
  Phase 3 state machine table with no endpoint behind it — the transition
  has been defined and tested-as-invalid for eight phases, and
  `apps/ratings` is what now legitimately triggers it
- **`Driver.average_rating` is finally written.** Phase 4's dispatch
  ranking has read that field as a tie-breaker since it was written, but
  nothing ever populated it — every driver scored `None`. Ratings now
  recompute it, so the ranking logic built back then actually does
  something
- **RATED means "at least one party rated," not "both."** Waiting for
  both would strand rides at `PAID` forever whenever one side doesn't
  bother — so the transition fires on the first rating, and the second
  direction's rating is still accepted afterwards without re-transitioning
- **Direction is derived from who the caller is, never from the request
  body.** A driver POSTing `{"direction": "RIDER_TO_DRIVER"}` still gets
  `DRIVER_TO_RIDER` — the server decides which side of the ride you're
  on, consistent with the "never trust client-asserted state" rule applied
  to payments in Phase 8. There's a test that attempts exactly that
  override and asserts it's ignored
- **Averages are recomputed, not incrementally adjusted.** An exact
  aggregate over all a driver's ratings is cheap at this scale, and a
  running average maintained by hand is precisely the derived-state drift
  the ledger discipline in Phases 8–9 exists to avoid
- **Guest parity, with an honest asymmetry**: a guest rider can rate their
  driver via `tracking_token`, but a driver *cannot* rate a guest back —
  there's no account to attach that reputation to. That's enforced in the
  service layer as a business rule rather than pretended around
- **Reviewer identity is never exposed** on the public driver summary —
  only scores and review text, so a rider can't work out which specific
  passenger left which comment
- 21 new automated tests: submission rules (too early, wrong party, twice
  in the same direction), the state transition and its idempotency, the
  average recomputation across multiple rides, the direction-override
  attempt, guest rating paths, and the reviewer-anonymity guarantee — all
  passing on the first run

### Phase 12 — Admin Dashboard
- **Mostly aggregation, because the CRUD already existed.** Driver
  approval, refunds, payout processing, pricing changes, and ride
  monitoring were all built and permission-gated across Phases 2–11.
  Phase 12 adds what an operator actually lacked: summary views, a live
  map, and accountability
- **FR-ADM-06's audit log — the one genuinely missing requirement.**
  Every administrative action across five apps (driver approve / reject /
  suspend / reactivate, refunds, payout completion and failure, pricing
  changes, manual ride assignment) now writes an immutable `AuditLog`
  entry
- **The audit log is append-only in the strong sense**: no update path in
  the service layer, and registered in Django admin with `has_add`,
  `has_change`, and `has_delete` all returning `False`. An audit log an
  administrator can edit is not an audit log
- **Audit entries outlive what they describe.** `actor_email` is
  snapshotted at write time and `target_type`/`target_id` are plain
  strings rather than a `GenericForeignKey` — so an entry still reads
  correctly after the acting admin's account or the target record is
  deleted. Both cases are directly tested, including a hard-delete of the
  acting admin
- **Permission gating is capability-specific, not role-tiered** —
  continuing the Phase 1 RBAC philosophy. A Support Agent can watch the
  live ride map (`ride.manage`) but *cannot* see revenue
  (`payment.refund`); Operations Managers can run the dashboard but
  *cannot* read the audit log (`user.manage`), because the log exists to
  hold operational roles accountable
- **The live map reads driver positions from Redis, not Postgres** — the
  same ephemeral store dispatch queries, so operators see where drivers
  actually are rather than the last sampled snapshot. If Redis is
  unavailable the board still renders, just without live pins
- **Aggregation windows are clamped** (max 30 days hourly / 365 days
  daily) so a caller can't request an unbounded scan, with garbage input
  falling back to the default rather than erroring
- **Revenue figures reconcile by construction**: a test asserts
  `gross_revenue == platform_commission + driver_earnings` rather than
  just checking each is non-zero
- 30 new automated tests, passing on the first run: dashboard aggregates
  (including the empty-platform case returning zeroes, not nulls), live
  map contents and exclusions, revenue reconciliation, dispatch-health
  divide-by-zero safety, the audit trail for each action type, entry
  survival after deletion, and the full permission matrix across five
  roles

### Phase 14 — Production Deployment & Kubernetes

Full detail lives in `../infrastructure/` (Kubernetes manifests, CI/CD,
Docker, monitoring — see `infrastructure/README.md` as the entry point);
this section covers what changed in the Django project itself to make
that possible.

- **This app is deployment-target-agnostic by construction.** Nothing in
  `apps/` or `config/` knows it's running in Kubernetes specifically —
  it's all environment variables (`config/settings.py`, already
  env-driven since Phase 1) and two plain HTTP endpoints
  (`apps/common/health.py`). `infrastructure/` is what actually points
  this at a cluster; the application would run identically on any
  container platform that can supply the same env vars and probe the
  same two URLs
- **`GET /healthz/` and `GET /readyz/`** — liveness vs. readiness, with
  genuinely different failure semantics: liveness checks nothing but "can
  this process respond at all" (a failure gets the pod killed and
  restarted — only useful if restarting fixes something); readiness
  checks Postgres and Redis (a failure just pulls the pod out of traffic
  until it recovers on its own — restarting wouldn't bring a dependency
  back). This distinction is most of the mechanism behind zero-downtime
  rolling deploys — see `infrastructure/kubernetes/README.md`'s
  walkthrough
- **Structured JSON logging in production**
  (`config/logging_formatters.py`), console-formatted in dev — every line
  tagged with a request ID (`apps.common.middleware.RequestIDMiddleware`)
  that survives all the way through Django's own post-middleware error
  logging, which a first version of this middleware's contextvar
  handling initially lost — caught by testing the actual log output, not
  just the HTTP response, and fixed before it shipped
- **Prometheus metrics at `/metrics`** (`django-prometheus`): HTTP
  request counts/latency by view and status, and — via the
  `django_prometheus.db.backends.postgresql` engine — database query
  duration, all with zero apps/ code changes. `PROMETHEUS_EXPORT_MIGRATIONS
  = False` so a 15-second scrape interval doesn't mean a migration-state
  query that often
- **A cache backend that's finally safe for more than one process.**
  `CACHE_BACKEND=redis` (Django's built-in Redis backend, no new
  dependency) replaces the process-local `LocMemCache` the RBAC
  permission lookup (`apps/identity/services.py`) has used since Phase 1
  — which was silently wrong the moment there was more than one web
  process, since a role change made via one pod would leave every *other*
  pod's cache stale with no invalidation path. Local dev keeps
  `LocMemCache` (nothing else needs to be running for `runserver`); this
  was a real, long-standing gap the settings comments have flagged since
  Phase 1, not a new problem introduced by this phase
- **WhiteNoise + `collectstatic`** serve admin/DRF static assets straight
  from the app process — no separate nginx/CDN needed for a modest admin
  surface
- **The full `SECURE_*`/reverse-proxy settings** for running correctly
  behind a TLS-terminating Ingress: `SECURE_PROXY_SSL_HEADER` (trust
  `X-Forwarded-Proto`, since Django itself never sees TLS directly), HSTS
  ramped conservatively (1 day by default, raised to 30 in the production
  overlay, `include_subdomains`/`preload` left off until HSTS itself has
  run safely for a while — an HSTS promise is hard to take back), and
  `SECURE_REDIRECT_EXEMPT` for `/healthz/`/`/readyz/` specifically —
  without it, `SECURE_SSL_REDIRECT` would 301 every kubelet probe, since
  kubelet hits the pod directly over plain HTTP and never sends
  `X-Forwarded-Proto`. Verified concretely, not just reasoned about: ran
  gunicorn locally with these exact settings and confirmed a probe
  request succeeds unencrypted while a real API request still redirects
  unless it arrives with the trusted proxy header
- **`python manage.py check --deploy` passes clean** (the two remaining
  default warnings — `SECURE_HSTS_INCLUDE_SUBDOMAINS`,
  `SECURE_HSTS_PRELOAD` — are deliberately deferred, per the HSTS
  reasoning above, not missed)
- **One Dockerfile, four Kubernetes workloads.** Multi-stage build (every
  dependency here ships a pre-built wheel, so this is about keeping pip's
  cache out of the final image, not about compiling anything), a
  dedicated non-root user, and `collectstatic` baked in at build time
  against placeholder settings so no real secret or database connection
  is needed just to hash and copy static assets
- **Migrations run as their own step, never on container startup** —
  `docker-entrypoint.sh` is deliberately minimal specifically to *not*
  auto-migrate, because several pods starting at once during a rolling
  deploy must never race to `ALTER` the same tables concurrently. See
  `infrastructure/kubernetes/base/migrate-job.yaml`
- 10 new automated tests (`apps/common/tests.py`) for the health
  endpoints and request-ID middleware, including the specific 503-with-
  per-dependency-detail behavior when Redis is unreachable and the
  contextvar-timing bug mentioned above. Full suite: 349 tests, still
  passing
- **Autoscaling, CI/CD, and monitoring are genuinely infrastructure, not
  Django settings** — covered in `infrastructure/README.md` rather than
  duplicated here: per-tier HPA policy (api/websocket/worker each tuned
  differently, not copy-pasted), the GitHub Actions pipeline (test →
  build → push → deploy-staging → deploy-production), and Prometheus
  alerts + a Grafana dashboard, both as code

## Requirements

- Python 3.11+
- **Redis** (any recent version) — required to run the app at all now:
  dispatch's driver-location store and Django Channels' channel layer
  both connect to it directly. Install locally with
  `apt-get install redis-server` (or any package manager) and start it
  with `redis-server --daemonize yes`; the test suite also talks to a real
  Redis instance rather than mocking it.
- A running Keycloak instance (18+) for actual login — not required to run
  the automated test suite, which mocks that network boundary
- (Optional for local dev; required for any real deployment) PostgreSQL 14+
  — `psycopg[binary]` is a hard requirement in `requirements.txt` as of
  Phase 14, since it's what every Kubernetes deployment actually uses
  (`DATABASE_ENGINE=postgres`); local dev/test still default to SQLite
  and need nothing extra installed for that path

Note: this codebase was built and tested in a network-restricted sandbox
that can reach PyPI/GitHub but not Keycloak or any mapping provider — both
of those integrations are implemented as real HTTP-calling code, verified
with mocked responses, and (for Keycloak) a live local instance is
documented for manual end-to-end testing. If your environment has open
internet access, `MAP_PROVIDER=osrm_nominatim` (the default) works against
the real public Nominatim/OSRM services with no code changes.

Note: `python manage.py runserver` now serves both HTTP and WebSocket
traffic via Daphne (an ASGI server) automatically, once `daphne` is
installed and listed first in `INSTALLED_APPS` — no separate process or
`runworker` command needed for local development.

## Keycloak setup

The backend expects a realm configured like this. A minimal local setup
with Docker:

```bash
docker run -d --name keycloak -p 8080:8080 \
  -e KEYCLOAK_ADMIN=admin -e KEYCLOAK_ADMIN_PASSWORD=admin \
  quay.io/keycloak/keycloak:latest start-dev
```

Then, in the Keycloak admin console (`http://localhost:8080`):

1. **Create a realm** named `ride-hailing` (or whatever you set
   `KEYCLOAK_REALM` to).
2. **Create a confidential client** (e.g. `ride-hailing-backend`):
   - Client authentication: On
   - Authentication flow: enable **Direct access grants**
     (this is what lets `/auth/login/` exchange a password for a token)
   - Copy the client secret into `KEYCLOAK_CLIENT_SECRET`
3. **Create realm roles** matching the local role names exactly:
   `Rider`, `Driver`, `Fleet Manager`, `Support Agent`,
   `Operations Manager`, `Finance Admin`, `Super Admin`. Role assignment at
   registration (`assign_realm_role`) looks up a role by this exact name —
   if it's missing, registration still succeeds (the local role is still
   granted), but the role claim won't appear in issued tokens until the
   realm role exists.
4. Leave the **master realm's default `admin` user/password** as-is for
   local dev (used for Admin API calls per `.env.example`), or create a
   dedicated service-account client with `manage-users` for production.

### Testing without a live Keycloak

`apps/identity/keycloak_test_utils.py` generates a real RSA keypair and
signs tokens shaped exactly like Keycloak's, so the test suite exercises
the actual signature/issuer/expiry verification code
(`keycloak_client.verify_access_token`) for real — only the network calls
(fetching the JWKS, hitting the token/admin endpoints) are mocked via
`unittest.mock`. This means `python manage.py test` needs no Keycloak
instance running, while still giving real coverage of the verification
logic itself. Manual end-to-end testing (an actual browser/Postman login)
does need the realm set up as above.

## Setup

```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env            # fill in your Keycloak client secret

redis-server --daemonize yes    # or run it however your platform prefers

python manage.py migrate        # applies schema + seeds default roles/permissions/vehicle types
python manage.py createsuperuser  # optional, for /admin/ (Django-admin staff only — see note below)

python manage.py runserver      # now serves HTTP and WebSocket together (via Daphne)
```

The API is now available at `http://127.0.0.1:8000/api/v1/`.

> **Note on `createsuperuser`:** this creates a *Django-admin-only* staff
> account authenticated with a local password against `/admin/` — it is
> not a platform user and has no Keycloak identity or RBAC role. Regular
> platform users (including admin-permission holders like Super Admin)
> are created through `/api/v1/auth/register/`, which provisions them in
> Keycloak.

### Re-seeding reference data

```bash
python manage.py seed_rbac            # default roles and permissions
python manage.py seed_vehicle_types   # default vehicle types (Moto, Sedan, SUV, Van)
```

Both are idempotent — safe to run any number of times. Both are also
applied automatically the first time you run `migrate`, via data
migrations — you only need these commands to refresh/reset the seed data
later.

### Running tests

```bash
python manage.py test apps
```

## Switching to PostgreSQL

Local development and the test suite default to SQLite for zero-setup
convenience. For staging/production, set in `.env`:

```
DATABASE_ENGINE=postgres
DATABASE_NAME=ride_hailing
DATABASE_USER=postgres
DATABASE_PASSWORD=<your-password>
DATABASE_HOST=localhost
DATABASE_PORT=5432
```

...and uncomment `psycopg[binary]` in `requirements.txt`, then
`pip install -r requirements.txt` again and re-run `python manage.py migrate`.

## API Reference (Phase 1)

All endpoints are under `/api/v1/`. Errors follow this shape:

```json
{ "error": { "code": "...", "message": "...", "details": null } }
```

### `POST /api/v1/auth/register/`
Public. Creates a user in Keycloak and mirrors a local profile row with an
initial role.

Request:
```json
{
  "email": "rider@example.com",
  "password": "StrongPass123!",
  "first_name": "Ada",
  "last_name": "Lovelace",
  "role": "Rider"
}
```
`role` accepts `"Rider"` or `"Driver"` (default `"Rider"`). Staff/admin
roles are assigned by an administrator, never at self-registration.

Response `201`:
```json
{
  "id": "…", "email": "rider@example.com", "phone_number": null,
  "first_name": "Ada", "last_name": "Lovelace",
  "is_active": true, "is_suspended": false,
  "roles": ["Rider"], "permissions": ["ride.cancel", "ride.request"],
  "created_at": "…"
}
```

### `POST /api/v1/auth/login/`
Public. Proxies email+password to Keycloak's token endpoint; the
request/response shape is unchanged from a typical JWT login endpoint —
only the token issuer is now Keycloak.

Request: `{ "email": "...", "password": "..." }`

Response `200`:
```json
{
  "access": "…", "refresh": "…",
  "user": { "id": "…", "email": "…", "roles": ["Rider"] }
}
```
Returns `401` for wrong credentials or a locally suspended account (a
platform-level flag Keycloak has no notion of).

### `POST /api/v1/auth/refresh/`
Public. Request: `{ "refresh": "..." }` → Response: `{ "access": "...", "refresh": "..." }`

### `GET /api/v1/users/me/`
Authenticated (`Authorization: Bearer <access token>`). Returns the
caller's own profile, roles, and permissions.

### `GET /api/v1/users/`
Authenticated **and** requires the `user.manage` permission (granted to
Super Admin by default). Returns `403` for any caller without it, `401` if
unauthenticated or the token is invalid/expired. Paginated
(`?page=&page_size=`).

## API Reference (Phase 2)

### `GET /api/v1/vehicle-types/`
Public. Reference data for registration forms:
`[{ "id": "…", "name": "Sedan", "description": "...", "passenger_capacity": 4 }, ...]`

### `GET /api/v1/drivers/me/`
Authenticated. Returns the caller's driver profile (application status,
documents, vehicles), or `404` if they haven't applied yet.

### `POST /api/v1/drivers/me/`
Authenticated. Submits a new driver application, or resubmits after a
rejection (moves the application back to `PENDING_REVIEW`).

Request:
```json
{ "license_number": "D12345", "license_expiry": "2030-01-01", "date_of_birth": "1990-05-12" }
```
Response `200`: the driver profile, including `approval_status`.

### `POST /api/v1/drivers/me/documents/`
Authenticated, `multipart/form-data`. Uploads or replaces a KYC document.

Fields: `document_type` (`DRIVERS_LICENSE` | `BACKGROUND_CHECK` |
`PROFILE_PHOTO`), `file`, optional `expires_at`.

### `GET /api/v1/admin/drivers/?status=PENDING_REVIEW`
Requires `driver.approve`. Lists driver applications, optionally filtered
by `approval_status`.

### `POST /api/v1/admin/drivers/{id}/approve/`
Requires `driver.approve`. `PENDING_REVIEW → APPROVED`. Returns `400` if
the driver isn't currently `PENDING_REVIEW`.

### `POST /api/v1/admin/drivers/{id}/reject/`
Requires `driver.approve`. Body: `{ "reason": "..." }`.
`PENDING_REVIEW → REJECTED`.

### `POST /api/v1/admin/drivers/{id}/suspend/`
Requires `driver.suspend`. Body: `{ "reason": "..." }`.
`APPROVED → SUSPENDED`.

### `POST /api/v1/admin/drivers/{id}/reactivate/`
Requires `driver.suspend`. `SUSPENDED → APPROVED`.

### `GET /api/v1/vehicles/me/` / `POST /api/v1/vehicles/me/`
Authenticated, caller must have a driver profile (any approval status —
vehicles can be registered ahead of approval). `POST` request:
```json
{ "vehicle_type_id": "…", "make": "Toyota", "model": "Corolla", "year": 2022, "color": "Blue", "license_plate": "ABC-123" }
```
The first vehicle registered becomes active automatically.

### `POST /api/v1/vehicles/{id}/activate/`
Authenticated, must own the vehicle. Makes it the driver's active vehicle,
deactivating all others belonging to that driver.

### `POST /api/v1/vehicles/{id}/documents/`
Authenticated, `multipart/form-data`, must own the vehicle. Fields:
`document_type` (`REGISTRATION` | `INSURANCE` | `INSPECTION`), `file`,
optional `expires_at`.

## API Reference (Phase 3)

### `POST /api/v1/rides/`
**No authentication required.** Requests a ride. Body:
```json
{
  "vehicle_type_id": "…",
  "pickup_address": "123 Main St", "pickup_latitude": "9.0300", "pickup_longitude": "38.7400",
  "destination_address": "456 Side St", "destination_latitude": "9.0100", "destination_longitude": "38.7600",
  "guest_phone_number": "+15551234567",
  "guest_name": "Sam"
}
```
- **Unauthenticated caller**: `guest_phone_number` is required (E.164-ish
  format, e.g. `+15551234567`); `guest_name` is optional.
- **Authenticated caller**: must hold the `ride.request` permission
  (granted to `Rider` by default); rides as themselves — any
  `guest_phone_number`/`guest_name` in the body is ignored.

Response `201` includes `tracking_token` — save it if you're a guest, it's
the only way to check on or cancel this ride afterwards.

### `GET /api/v1/rides/me/`
Authenticated registered riders only. Their own ride history, paginated.

### `GET /api/v1/rides/{id}/`
Authenticated. Visible to the ride's own rider, its assigned driver, or
anyone with `ride.manage`. `tracking_token` is only included when the
caller is the ride's own rider.

### `POST /api/v1/rides/{id}/cancel/`
Authenticated, requires `ride.cancel`. Works for either the ride's rider
or its assigned driver — which one applies is derived from the ride
itself, not asserted by the caller. Body: `{ "reason": "..." }` (optional).

### `GET /api/v1/rides/track/{tracking_token}/`
**No authentication required.** Guest self-service status check.

### `POST /api/v1/rides/track/{tracking_token}/cancel/`
**No authentication required.** Guest self-service cancellation. Body:
`{ "reason": "..." }` (optional).

### Driver lifecycle actions
All require the caller to be a driver with an active KYC approval and to
be the specific driver assigned to that ride:

| Endpoint | Permission | Transition |
|---|---|---|
| `POST /api/v1/rides/{id}/accept/` | `ride.accept` | `DRIVER_ASSIGNED → DRIVER_ACCEPTED` |
| `POST /api/v1/rides/{id}/decline/` | `ride.accept` | `DRIVER_ASSIGNED → SEARCHING_DRIVER` |
| `POST /api/v1/rides/{id}/arriving/` | `ride.accept` | `DRIVER_ACCEPTED → DRIVER_ARRIVING` |
| `POST /api/v1/rides/{id}/arrived/` | `ride.accept` | `DRIVER_ARRIVING → DRIVER_ARRIVED` |
| `POST /api/v1/rides/{id}/start/` | `ride.start` | `DRIVER_ARRIVED → TRIP_STARTED` |
| `POST /api/v1/rides/{id}/complete/` | `ride.complete` | `TRIP_STARTED → TRIP_COMPLETED → PAYMENT_PENDING` (auto) |

### Admin / operations (dispatch stand-in)

| Endpoint | Permission | Effect |
|---|---|---|
| `GET /api/v1/admin/rides/?status=SEARCHING_DRIVER` | `ride.manage` | List/filter all rides |
| `POST /api/v1/admin/rides/{id}/assign-driver/` | `ride.assign` | `SEARCHING_DRIVER → DRIVER_ASSIGNED`; body `{ "driver_id": "..." }`, driver must be `APPROVED` |
| `POST /api/v1/admin/rides/{id}/mark-no-driver-found/` | `ride.assign` | `SEARCHING_DRIVER → NO_DRIVER_FOUND` |
| `POST /api/v1/admin/rides/{id}/expire/` | `ride.assign` | `DRIVER_ARRIVED → EXPIRED` (rider no-show) |

## API Reference (Phase 4)

### `POST /api/v1/drivers/me/availability/`
Authenticated driver. Body: `{ "available": true }`. Going online requires
`APPROVED` status. Going offline immediately clears the driver's Redis
location (they stop being a dispatch candidate right away, not after the
location TTL expires).

### `POST /api/v1/drivers/me/location/`
Authenticated driver, must currently be online (`is_available=True`).
Body: `{ "latitude": "9.0300", "longitude": "38.7400" }`. Writes to Redis
only — `204 No Content` on success.

### `GET /api/v1/drivers/me/offers/`
Authenticated driver. Lists this driver's currently pending (unexpired)
ride offers — poll this to find out about new offers until Phase 5 adds
push delivery over WebSocket.

### `POST /api/v1/admin/dispatch/run-cycle/`
Requires `ride.assign`. Manually triggers one dispatch cycle (see below).
Returns a summary: `{ "expired_offers": 2, "dispatched": 1, "no_driver_found": 0, "still_searching": 3 }`.

### The dispatch cycle
```bash
python manage.py run_dispatch_cycle
```
Run this on a schedule (cron, or Celery beat once that's wired up in a
later phase) to keep searching rides moving: it expires overdue offers,
sends new offers for any `SEARCHING_DRIVER` ride with none currently
pending, and marks a ride `NO_DRIVER_FOUND` once its search window
(120 seconds by default — see `apps/dispatch/services.py:MAX_SEARCH_SECONDS`)
has elapsed with no eligible candidates. A new ride also gets one
immediate dispatch attempt at creation time (best-effort, from
`RideCreateView`) — the cycle is what retries and eventually gives up.

## API Reference (Phase 5)

### `POST /api/v1/drivers/me/location/`
Authenticated driver, must be online. Body now also accepts optional
`heading` (0-360), `speed`, and `accuracy`. Returns `429 Too Many Requests`
if called again within 3 seconds of the driver's last update.

### `GET /api/v1/rides/{id}/locations/`
Authenticated. Same access rule as the ride detail view (own rider,
assigned driver, or `ride.manage`). Returns the sampled location history
for that ride, oldest first:
```json
[{ "latitude": "9.030000", "longitude": "38.740000", "heading": null, "speed": null, "accuracy": null, "recorded_at": "…" }, ...]
```

### `WS /ws/rides/{ride_id}/track/?token=<keycloak_access_token>`
Registered rider/driver/admin. The access token travels as a query
parameter — browsers can't set a custom header on a WebSocket handshake.
Connection is rejected (HTTP 403 during the handshake) unless the token
resolves to the ride's own rider, its assigned driver, or a caller with
`ride.manage`.

### `WS /ws/rides/track/{tracking_token}/`
Guest, no token needed — the tracking_token from ride creation is itself
the credential, exactly like the REST guest endpoints.

**Messages pushed to a connected client:**
```json
{ "event": "location_update", "latitude": "9.031000", "longitude": "38.741000", "heading": null, "speed": null, "recorded_at": "…" }
{ "event": "status_update", "status": "DRIVER_ARRIVING", "driver_id": "…" }
```
No messages are sent by the client after connecting — this is a
server-push-only channel for now.

## API Reference (Phase 6)

### `POST /api/v1/maps/geocode/`
Public. Body: `{ "address": "Bole Road, Addis Ababa" }`. Response:
```json
{ "formatted_address": "Bole Road, Addis Ababa, Ethiopia", "latitude": 9.03, "longitude": 38.74 }
```
Returns `422` (not `500`) if the provider can't resolve the address or is
unreachable — `{ "error": { "code": "map_provider_error", "message": "...", "details": null } }`.

### `POST /api/v1/maps/reverse-geocode/`
Public. Body: `{ "latitude": 9.03, "longitude": 38.74 }`. Response:
`{ "formatted_address": "..." }`.

### `POST /api/v1/maps/route/`
Public. Body:
```json
{ "origin_latitude": 9.03, "origin_longitude": 38.74, "destination_latitude": 9.01, "destination_longitude": 38.76 }
```
Response: `{ "distance_km": 5.2, "duration_minutes": 12.0, "polyline": "..." }`
(`polyline` is `null` if the provider doesn't supply one).

## API Reference (Phase 7)

### `GET /api/v1/pricing-rules/`
Public. Every configured pricing rule, one per vehicle type:
```json
[{ "vehicle_type": { "name": "Sedan", ... }, "currency": "USD", "base_fare": "2.00", "per_km_rate": "0.50", "per_minute_rate": "0.10", "per_minute_waiting_rate": "0.10", "minimum_fare": "3.00", "cancellation_fee": "1.00", "surge_multiplier": "1.00", "tax_rate": "0.0000" }, ...]
```

### `GET /api/v1/pricing-rules/{vehicle_type_id}/`
Public. A single rule.

### `PUT /api/v1/pricing-rules/{vehicle_type_id}/`
Requires `pricing.manage` (Operations Manager, Finance Admin, Super Admin
by default). Full upsert — creates the rule if none exists yet for that
vehicle type, otherwise replaces it wholesale:
```json
{ "base_fare": "2.00", "per_km_rate": "0.50", "per_minute_rate": "0.10", "minimum_fare": "3.00", "cancellation_fee": "1.00", "surge_multiplier": "1.00", "tax_rate": "0.0000" }
```

### `GET /api/v1/rides/{id}/fares/`
Authenticated. Same access rule as the ride detail view (own rider,
assigned driver, or `ride.manage`). Every `Fare` recorded for the ride —
typically an `ESTIMATE` right after creation, later a `FINAL` once the
trip completes, and a `CANCELLATION` if applicable:
```json
[{
  "fare_type": "ESTIMATE", "currency": "USD", "distance_km": "5.00", "duration_minutes": "10.0",
  "total_amount": "5.50",
  "line_items": [
    { "item_type": "BASE_FARE", "label": "Base fare", "amount": "2.00", "sequence": 0 },
    { "item_type": "DISTANCE_FARE", "label": "Distance", "amount": "2.50", "sequence": 1 },
    { "item_type": "DURATION_FARE", "label": "Duration", "amount": "1.00", "sequence": 2 }
  ]
}]
```

## API Reference (Phase 8)

### `GET /api/v1/rides/{id}/payments/`
Authenticated. Same access rule as ride detail (own rider, assigned
driver, or `ride.manage`). Returns the ride's `Payment` plus its
transaction history:
```json
{
  "method": "CASH", "provider_name": "", "status": "CAPTURED", "currency": "USD", "amount": "5.50",
  "provider_reference": "", "failure_reason": "",
  "transactions": [{ "transaction_type": "CAPTURE", "amount": "5.50", "provider_reference": "", "created_at": "…" }]
}
```

### `POST /api/v1/rides/{id}/payments/`
Authenticated, only the ride's own registered rider. Body:
```json
{ "method": "CARD", "provider_name": "stripe", "payment_token": "tok_visa" }
```
`provider_name`/`payment_token` are omitted for `"method": "CASH"`. Idempotent
— calling this again for the same ride returns the existing payment.
Returns `400` (not `500`) if the ride isn't `PAYMENT_PENDING` yet, has no
final fare, or the provider declines the charge.

### `GET` / `POST /api/v1/rides/track/{tracking_token}/payments/`
Guest equivalents of the two endpoints above — same behavior, authorized
by the tracking token instead of a login session.

### `POST /api/v1/rides/{id}/payments/confirm-cash/`
Authenticated, only the ride's assigned driver. Marks a `CASH` payment
`CAPTURED` and the ride `PAID`. Idempotent — confirming twice is a no-op,
not an error.

### `POST /api/v1/payments/webhooks/{provider_name}/`
Public (`stripe` or `chapa`), but every request is signature-verified
before anything in it is trusted — an invalid signature returns `400`
with no further processing. Duplicate deliveries of the same event are
detected and safely ignored (`{ "processed": false }`), never
double-charged or double-recorded.

### `POST /api/v1/admin/payments/{id}/refund/`
Requires `payment.refund` (Finance Admin, Super Admin by default). Body:
`{ "amount": "2.00", "reason": "Rider complaint" }` — `amount` is optional
(defaults to a full refund). Returns `400` if the refund would exceed the
original payment or the payment was never captured.

## API Reference (Phase 9)

### `GET /api/v1/drivers/me/wallet/`
Authenticated driver. Current balance (derived, never stored) and full
ledger history:
```json
{
  "currency": "USD", "balance": "4.40",
  "ledger_entries": [{ "entry_type": "CREDIT_EARNING", "amount": "4.40", "description": "Earning for ride …", "ride_id": "…", "payout_id": null, "created_at": "…" }]
}
```

### `GET /api/v1/drivers/me/earnings/`
Authenticated driver. Per-ride earnings history with the full commission
breakdown:
```json
[{ "ride_id": "…", "currency": "USD", "fare_amount": "5.50", "commission_rate": "0.2000", "commission_amount": "1.10", "driver_earning_amount": "4.40", "created_at": "…" }]
```

### `GET /api/v1/drivers/me/payouts/`
Authenticated driver. Their own payout request history.

### `POST /api/v1/drivers/me/payouts/`
Authenticated driver. Body: `{ "amount": "10.00" }` — `amount` is optional
(defaults to the full available balance). Returns `400` if below the
configured minimum, above the available balance, or a payout is already
pending.

### `GET /api/v1/admin/payouts/?status=PENDING`
Requires `payout.manage` (Finance Admin, Super Admin by default).

### `POST /api/v1/admin/payouts/{id}/complete/`
Requires `payout.manage`. Marks a pending payout completed (the actual
funds transfer happens outside this API, the same way cash payments work).

### `POST /api/v1/admin/payouts/{id}/fail/`
Requires `payout.manage`. Body: `{ "reason": "Invalid bank details" }`.
Marks the payout failed and reverses its debit with a new credit entry.

## API Reference (Phase 10)

### `GET /api/v1/notifications/me/`
Authenticated. The caller's in-app notification feed (IN_APP channel only
— email/SMS/push rows are delivery records, not feed items):
```json
[{ "id": "…", "channel": "IN_APP", "event_type": "RIDE_DRIVER_ASSIGNED", "title": "Driver assigned", "body": "…", "status": "SENT", "ride_id": "…", "created_at": "…", "sent_at": "…" }]
```

### `POST /api/v1/users/me/push-token/`
Authenticated. Body: `{ "push_token": "fcm-device-token" }` — send an
empty string to clear it (e.g. on logout). Returns `204`.

### `GET` / `PATCH /api/v1/users/me/notification-preferences/`
Authenticated. `PATCH` accepts any subset of
`{ "notify_email": true, "notify_sms": false, "notify_push": true }`.
In-app notifications aren't gated by any preference (no external delivery
cost). Both return the full current preference set.

### Running the Celery worker
Notification delivery happens in a background worker, so one needs to be
running for anything to actually send:
```bash
celery -A config worker --loglevel=info
```
Without a worker, notifications are still created (status `PENDING`) and
queued — they just stay undelivered until a worker picks them up. For
quick local testing without a worker, set `CELERY_TASK_ALWAYS_EAGER=True`
in `.env` to run tasks synchronously in-process. The test suite already
does this automatically.

### Running the Celery beat scheduler (Phase 13)
The daily analytics rollup (`apps.analytics.tasks.compute_daily_rollup_task`)
is scheduled, not triggered by any request, so it needs Celery's
scheduler process running alongside a worker:
```bash
celery -A config beat --loglevel=info
```
It fires once a day at 00:15 UTC and rolls up *yesterday*. To populate
history without waiting for it — a fresh dataset, or a demo — run the
management command instead: `python manage.py backfill_daily_rollups --days 30`.

## API Reference (Phase 11)

### `GET` / `POST /api/v1/rides/{id}/ratings/`
Authenticated. `GET` returns both directions' ratings (rider, driver, or
`ride.manage`). `POST` submits one — body `{ "score": 5, "review": "..." }`
(`review` optional). **The direction is derived from who you are**, not
from the body: the ride's rider gets `RIDER_TO_DRIVER`, its assigned
driver gets `DRIVER_TO_RIDER`, anyone else gets `403`. Returns `400` if
the ride isn't paid yet or that direction is already rated.

### `POST /api/v1/rides/track/{tracking_token}/ratings/`
Public. A guest rider rating their driver, authorized by the tracking
token. Always `RIDER_TO_DRIVER`.

### `GET /api/v1/drivers/{id}/ratings/`
Public. A driver's reputation summary — reviewer identities are
deliberately omitted:
```json
{ "driver_id": "…", "average_rating": 4.75, "rating_count": 12, "recent_reviews": [{ "score": 5, "review": "Great trip", "created_at": "…" }] }
```

### `GET /api/v1/ratings/me/`
Authenticated. Ratings the caller has *received*, with their average.

## API Reference (Phase 12)

All Phase 12 endpoints are read-only aggregations — they create no new
capability, only compose data the caller's existing permissions already
allow.

### `GET /api/v1/admin/dashboard/?hours=24`
Requires `ride.manage`. Operational snapshot: active rides, online and
pending-review drivers, ride counts by outcome, gross revenue, platform
commission, driver earnings, pending payouts, payment failures. `hours`
is clamped to 720.

### `GET /api/v1/admin/live-rides/`
Requires `ride.manage`. Every in-flight ride with its driver's live
position pulled from Redis:
```json
[{ "ride_id": "…", "status": "TRIP_STARTED", "rider": "rider@example.com", "driver": "driver@example.com",
   "vehicle_type": "Sedan", "pickup_address": "…", "destination_address": "…",
   "driver_latitude": 9.032, "driver_longitude": 38.742, "created_at": "…" }]
```
Guest rides show as `"guest:+1555…"`. Positions are `null` if no driver
is assigned yet or Redis is unreachable.

### `GET /api/v1/admin/dispatch-health/?hours=24`
Requires `ride.manage`. Offers sent/accepted/declined/expired, acceptance
rate, and no-driver-found rate. Rates are `null` rather than `0` when
there's no activity to divide by.

### `GET /api/v1/admin/revenue/?days=30`
Requires **`payment.refund`** (Finance Admin, Super Admin) — deliberately
not `ride.manage`. Gross revenue, commission, driver share, average fare,
and a per-vehicle-type breakdown. `days` is clamped to 365.

### `GET /api/v1/admin/audit-log/?action=&target_type=&target_id=&actor_email=`
Requires **`user.manage`** (Super Admin) — the log records what other
administrators did, so operational roles can't read it. Paginated.

## API Reference (Phase 13)

All four endpoints read from the pre-computed rollup tables, never from
raw Ride/Payment/DriverEarning data directly — that live-scan job belongs
to the Phase 12 endpoints above. `days` is clamped to 365 everywhere
(same convention as Phase 12) and counts back from the most recent fully
**closed** day (yesterday), never today.

### `GET /api/v1/analytics/operations/daily/?days=30`
Requires `ride.manage`. Paginated, newest-first list of one row per
closed day: ride-outcome counts, dispatch offer counts, and the two
Phase-12-style null-safe rates.

### `GET /api/v1/analytics/operations/summary/?days=30`
Requires `ride.manage`. Window totals summed across `days`, plus
`days_available` (how many of those days actually have a rollup yet).
`offer_acceptance_rate`/`no_driver_found_rate` are computed from the
summed counts, not averaged per day.

### `GET /api/v1/analytics/revenue/daily/?days=30`
Requires **`payment.refund`** (Finance Admin, Super Admin) — same
reasoning as Phase 12's live revenue report. Paginated list of one row
per closed day: gross revenue, commission, driver share, average fare,
and that day's per-vehicle-type breakdown.

### `GET /api/v1/analytics/revenue/summary/?days=30`
Requires `payment.refund`. Window financial totals plus a per-vehicle-type
breakdown aggregated across the whole window, both summed from the
rollup tables rather than scanned live.

## API Reference (Phase 14)

Unauthenticated by design — see each entry for why.

### `GET /healthz/`
Liveness. Always `200 {"status": "ok"}` if the process can respond at
all; checks no dependency. Not part of the versioned API namespace, and
not meant to be called by anything other than an orchestrator's liveness
probe.

### `GET /readyz/`
Readiness. `200 {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}`
when both are reachable; `503` with per-dependency detail
(`{"status": "unavailable", "checks": {"database": "ok", "redis": "error: ConnectionError"}}`)
when either isn't. No auth, same reasoning as `/healthz/` — access is
restricted at the network layer in the Kubernetes manifests
(`infrastructure/kubernetes/base/networkpolicy-*.yaml`), not with a
credential.

### `GET /metrics`
Prometheus exposition format (`django-prometheus`). Same no-auth-by-
network-policy reasoning as above.

## Project layout

```
backend/
  config/                  Django project settings, root URLs, ASGI/WSGI
                            entrypoints, and the Celery app (celery.py)
  apps/
    common/                Base models (UUID PK, timestamps, soft delete),
                            shared pagination and exception-handling
                            conventions, (money.py) the shared Decimal
                            quantization/float-boundary helpers apps.pricing
                            and apps.earnings both build on, and (Phase 14)
                            health.py — liveness/readiness endpoints — and
                            middleware.py/logging_filters.py — the
                            request-ID log-correlation pair
    identity/               RBAC: Role, Permission, UserRole, RolePermission,
                            the permission-check service, the seed_rbac command,
                            and the Keycloak integration:
                              keycloak_client.py    — token/admin API calls, JWKS verification
                              authentication.py     — DRF auth backend (KeycloakAuthentication)
                              keycloak_test_utils.py — self-signed test tokens (test-only)
    users/                  Custom User model (keycloak_id-linked), registration/
                            login/refresh views proxying to Keycloak
    drivers/                Driver model, KYC documents, the approval state
                            machine (services.py), and the admin approval workflow
    vehicles/               VehicleType, Vehicle, compliance documents, the
                            single-active-vehicle rule (services.py)
    rides/                  Ride, RideStatusHistory, the full ride state
                            machine (services.py), guest-vs-registered-rider
                            creation, driver lifecycle actions, and the
                            manual dispatch override (admin assign-driver)
    dispatch/               Real dispatch: candidate finding, DispatchOffer
                            bookkeeping, the multi-offer race resolution,
                            and the periodic dispatch cycle (services.py) —
                            depends on apps.locations for driver positions
    locations/              Redis-backed current-position store (geo.py),
                            throttled ingestion + sampled persistence
                            (services.py), the WebSocket consumer and
                            Keycloak WS auth middleware (consumers.py,
                            ws_auth.py), and the DriverLocation history model
    maps/                   The MapProvider abstraction (base.py), two real
                            implementations (providers/osrm_nominatim.py,
                            providers/google_maps.py), the provider factory,
                            and the public geocode/reverse-geocode/route endpoints
    pricing/                PricingRule, Fare, FareLineItem; the pure
                            Decimal-only calculate_fare() engine and its
                            persistence/immutability rules (services.py);
                            wired into ride creation, completion, and
                            late cancellation
    payments/               Payment, the append-only PaymentTransaction
                            ledger, the PaymentProvider abstraction
                            (providers/stripe_provider.py, providers/chapa_provider.py),
                            idempotent charge/webhook/refund logic
                            (services.py); wired into PAYMENT_PENDING -> PAID
    earnings/               Wallet, the append-only WalletLedgerEntry,
                            DriverEarning (per-ride commission-split audit),
                            Payout; idempotent crediting and row-locked
                            payout requests (services.py); wired into
                            the PAID transition
    notifications/          Notification model, the SMS/Push provider
                            abstractions (providers/twilio_provider.py,
                            providers/fcm_provider.py), channel resolution
                            and idempotent creation (services.py), and the
                            Celery delivery task (tasks.py); hooks fire from
                            apps.rides, apps.drivers, apps.payments, apps.earnings
    ratings/                Rating (bidirectional, one per ride per
                            direction); submission authorization, average
                            recomputation feeding Driver.average_rating,
                            and the PAID -> RATED transition (services.py)
    admin_api/              Operator dashboard: aggregate/monitoring
                            queries (services.py), the immutable AuditLog
                            model, and record_audit() — the single write
                            path, called from drivers, payments, earnings,
                            pricing, and rides
    analytics/              Historical daily rollups: DailyPlatformRollup,
                            DailyVehicleTypeRollup, the idempotent
                            compute_daily_rollup() (services.py), the
                            Celery beat task (tasks.py), and the
                            backfill_daily_rollups management command —
                            read-only Django admin, no new write path
  config/
    settings.py              (Phase 14 additions) structured logging,
                              Prometheus metrics, WhiteNoise static-file
                              serving, the Redis-backed cache option, and
                              the SECURE_*/proxy settings for running
                              behind a TLS-terminating Ingress
    logging_formatters.py     (Phase 14) the JSON log formatter used in
                              production; console-formatted in dev
  Dockerfile, docker-entrypoint.sh, .dockerignore   (Phase 14) — see
                              ../infrastructure/docker/README.md
  requirements.txt

../infrastructure/            (Phase 14, sibling of backend/) Kubernetes
                              manifests, CI/CD, Docker Compose, and
                              Prometheus/Grafana — see its own README.md
  .env.example
  manage.py
```

## Design decisions worth knowing about

- **Keycloak owns credentials; the platform owns authorization.** No
  password hash for a regular platform user is ever stored locally
  (`set_unusable_password()` is called at provisioning) — only Keycloak
  can verify a password. Every permission check still runs entirely
  against local `Role`/`Permission` tables.
- **UUID primary keys** everywhere, so no table's row count or growth rate
  leaks through the API.
- **Permission checks always go through `apps/identity/services.py`.** No
  view checks `request.user.role == "..."` directly.
- **JIT provisioning is additive-only.** A role present in a Keycloak
  token gets synced into the local `UserRole` table if missing; a role
  *removed* in Keycloak is not automatically stripped locally on the next
  request — de-provisioning is an explicit admin action, not something
  that happens silently mid-session.
- **Every state machine in the codebase (driver approval, ride lifecycle)
  follows the same shape**: one explicit transition table, one function
  that's the sole writer of the status field, invalid transitions raise a
  clean error rather than silently corrupting state. `apps/rides/services.py`
  is the fullest expression of this, matching the architecture doc's
  Section D exactly.
- **A ride's requester is a rider XOR a guest phone number, enforced at
  the database level** (`CheckConstraint`), not just in application code —
  a bug in a service function can't silently create an ownerless or
  double-owned ride.
- **A guest's `tracking_token` is scoped tightly**: it's generated at
  creation, never regenerated or rotated, only ever returned to the
  ride's own creator (never to the assigned driver or an admin viewing
  the same ride through `/admin/rides/`), and is the only thing that
  authorizes a guest's status-check or cancel calls — there is no
  "look up my ride by phone number" endpoint, which would let anyone who
  knows a phone number pull ride history for it.
- **"Dispatch proposes, Ride Management decides."** `apps/dispatch`
  creates and updates `DispatchOffer` rows but never writes `Ride.status`
  itself — the actual transition on acceptance happens inside
  `apps/rides/services.py`, the one place that's ever allowed to write
  ride state. Keeping this boundary meant Phase 4 could introduce
  simultaneous multi-driver offers (and the race condition that comes
  with them) without touching the state machine's enforcement point at
  all — only `accept_ride`/`decline_ride` gained a second code path.
- **Current driver location lives in Redis, never Postgres** — a GEO set
  for proximity queries plus a short-TTL freshness key per driver, so
  staleness is a query-time check rather than something a background job
  has to sweep for. The same principle the architecture doc states for
  the full real-time tracking pipeline (Phase 5); Phase 4 just needed a
  lightweight version of it to make candidate search possible at all.
- **The race condition is real, and tested with real threads.** Two
  drivers can both hold a valid, unexpired offer on the same ride at the
  same time. `DispatchOfferAndAcceptRaceTests` uses actual Python threads
  with separate DB connections (via `TransactionTestCase`, not `TestCase`)
  to prove `select_for_update` genuinely serializes the two accept
  attempts rather than just asserting it does in a sequential simulation.
- **A broadcast only fires after its transaction actually commits.**
  `_transition()` schedules its WebSocket status broadcast via
  `transaction.on_commit`, capturing the specific status/driver values at
  scheduling time rather than reading them off the (possibly further
  mutated) `Ride` object when the callback runs later — this matters
  because a single request can call `_transition` twice
  (`complete_trip`'s `TRIP_COMPLETED` then `PAYMENT_PENDING`), and each
  broadcast needs to report the status it was scheduled for.
- **`TransactionTestCase`'s per-test database flush wipes migration-seeded
  data** (RBAC roles/permissions, vehicle types) — a real gotcha this
  codebase hit directly once a second `TransactionTestCase` class existed
  in the suite (needed for both the dispatch race test and the WebSocket
  broadcast test, since Channels' async consumers and `transaction.on_commit`
  both need real commits, not `TestCase`'s rolled-back ones). The fix is
  explicit re-seeding in both `setUp()` and `tearDown()` — not Django's
  `serialized_rollback`, which turned out to collide with the ContentType
  framework once more than one such class exists.
- **Document re-uploads are upserts, not new rows.** `DriverDocument` and
  `VehicleDocument` have a unique constraint on `(owner, document_type)`;
  resubmitting replaces the file and resets status to pending review
  rather than accumulating an unbounded history of superseded uploads.
- **Soft delete** is opt-in per model (`SoftDeleteModel` mixin) and is
  intentionally *not* used for anything financial or history-related in
  later phases — those tables are append-only, per the architecture doc.
  `RideStatusHistory` is append-only by construction (no update/delete
  path exists in the service layer at all).
- **Business logic lives in `services.py` files**, not in serializers or
  views — this is the pattern every later phase's write operations should
  follow.
- **The `MapProvider` abstraction is real, not aspirational.** Two
  independent implementations exist (`OSRMNominatimProvider`,
  `GoogleMapsProvider`), each hiding a genuinely different API shape
  (Nominatim/OSRM's plain REST vs. Google's own response format) behind
  the same three-method interface — proof the abstraction would actually
  survive adding Mapbox later, not just a single provider dressed up
  behind an interface nothing else uses.
- **A provider failure is a `422`, not a `500` or a broken ride.** Both
  the public maps endpoints and the best-effort route estimate at ride
  creation catch `MapProviderError` explicitly — verified against this
  sandbox's own network restrictions actually rejecting the outbound
  call, not just a mocked exception.
- **Money is Decimal from the database field all the way through the
  calculation to the API response — with one deliberate, tested seam.**
  `Ride.estimated_distance_km` can hold a raw `float` in memory between
  Phase 6 assigning it and the next database read; `apps/pricing/services.py`
  treats that boundary explicitly (`_as_decimal()`) rather than assuming
  every caller already has a clean Decimal, because assuming that is
  exactly how float imprecision quietly reaches a money calculation in
  real codebases.
- **A Fare's line items are quantized individually, then summed** — the
  total is never computed by rounding some other running float and
  hoping it matches what the line items say. `sum(line_items) ==
  fare.total_amount` holds exactly, always, and is directly tested.
- **FINAL and CANCELLATION fares are immutable once created**; only
  ESTIMATE may be recalculated. This matters once Payments (Phase 8)
  exists — a fare a payment has been charged against must never silently
  change underneath it.
- **Payment status changes only from server-controlled events — never a
  client claim.** A verified webhook, a driver's explicit cash
  confirmation, or an admin's refund action are the only three writers of
  `Payment.status`. No endpoint anywhere accepts "mark this as paid" from
  a request body.
- **Idempotency is a database constraint, not a check-then-act read.**
  `PaymentTransaction.idempotency_key` is unique; a duplicate webhook
  delivery hits that constraint and is caught as `IntegrityError`, not
  prevented by a racy "does this already exist" query beforehand. This is
  the actual mechanism that makes the "duplicate webhook" guarantee true
  under real concurrent delivery, not just under a single-threaded test.
- **Transaction boundaries are drawn around what should survive a
  failure, not around "the whole function."** `initiate_payment`'s
  failure-path status update needed its own transaction boundary,
  separate from the charge attempt — wrapping everything in one
  `@transaction.atomic` (the first draft) meant a failed charge's
  rollback silently erased the very record meant to explain why it
  failed. A test asserting that record's existence is what caught it.
- **A wallet balance is a query, never a stored number.**
  `get_wallet_balance()` sums `WalletLedgerEntry.amount` fresh every call
  — there is no `Wallet.balance` field to accidentally get out of sync
  with the entries that are supposed to explain it.
- **Row-locking protects a balance the same way it protects a ride.**
  `request_payout()` locks the `Wallet` row with `select_for_update`
  before computing available balance and creating the debit — the exact
  same discipline `assign_driver`/`accept_ride` use on `Ride` (Phase 3/4),
  applied to money instead of ride state, and verified the same way: a
  real multi-threaded test, not an assumption.
- **Extracting a shared utility once two apps needed it identically** —
  `apps/common/money.py` didn't exist until Phase 9 needed the exact
  quantization and float-boundary logic Phase 7 had already written
  as private details inside `apps/pricing/services.py`. Waiting for the
  second real use before generalizing, rather than speculatively
  building a shared module in Phase 7 for a need that didn't exist yet.
- **Not every integration needs a custom abstraction.** SMS and push got
  `SMSProvider`/`PushProvider` interfaces because swapping Twilio for
  another gateway is a real scenario with no existing solution. Email did
  *not* — Django's `EMAIL_BACKEND` setting already is that abstraction,
  and wrapping it in another one would have been ceremony, not design.
- **Tests must not depend on a running message broker.** Wiring Celery in
  made every ride/payment/driver test start publishing to a live Redis
  broker, which turned a 5-second suite into a 300-second timeout. Tests
  now default to eager mode when `test` is in `sys.argv`, so the suite
  exercises the full `notify() → task → deliver_notification()` path
  in-process with no broker at all — and a separate, manual end-to-end
  check with a real `celery worker` process confirms the genuinely
  asynchronous path works too. Both matter; neither substitutes for the
  other.
- **Cross-context hooks are always best-effort.** Every notification
  trigger (in rides, drivers, payments, earnings) is wrapped so a
  notification failure logs and moves on, never unwinding the ride
  transition, payment capture, or payout that triggered it. This is the
  same tolerance pattern established for dispatch (Phase 4), routing
  (Phase 6), and pricing (Phase 7).

- **A field with a reader but no writer is a latent bug, not a
  placeholder.** `Driver.average_rating` was added in Phase 4 and read by
  dispatch ranking from that moment on — but nothing wrote it until Phase
  11, so for seven phases every driver silently ranked identically on
  that criterion. Worth noting because the code *looked* complete at
  every intermediate step; only wiring the writer revealed the ranking
  had been partly inert all along.
- **Derived reputation is recomputed, never incrementally patched** —
  same reasoning as the wallet balance in Phase 9. An exact aggregate is
  cheap; a hand-maintained running average is drift waiting to happen.

- **An audit log must outlive what it audits.** Storing
  `target_type`/`target_id` as plain strings rather than a
  `GenericForeignKey` is deliberate: a GFK would either cascade the log
  entry away with its target or leave a dangling reference. Same
  reasoning for snapshotting `actor_email` alongside the FK.
- **Recording an action must never be able to fail that action.**
  `record_audit()` logs and moves on rather than raising — an admin
  shouldn't be blocked from suspending a fraudulent driver because a log
  table is unavailable. The trade-off (a possible gap in the trail) is
  named explicitly in that module's docstring, along with what to change
  if a compliance context needs guaranteed completeness instead.

### Phase 13 — Analytics & Reporting
- **The distinction from Phase 12 is the whole point, not an incidental
  difference.** The dashboard (Phase 12) answers "what is happening right
  now" with live queries — deliberately, per that app's own docstring,
  which named this exact table as future work. Phase 13 answers "what
  happened over time": a `DailyPlatformRollup` row per UTC calendar day,
  computed once and never touched again, so a trend chart over months
  reads a handful of pre-summed rows instead of re-scanning
  Ride/DispatchOffer/Payment/DriverEarning for the same closed, unchanging
  days on every request
- **Two tables, both pure projections — no new source of truth.**
  `DailyPlatformRollup` (ride outcomes, dispatch health, revenue totals)
  and `DailyVehicleTypeRollup` (that day's revenue split by vehicle type,
  a separate table rather than a JSON blob so it stays independently
  queryable). Both are registered read-only in Django admin, same
  reasoning as Phase 12's `AuditLog`: a derived number an operator can
  hand-edit stops being trustworthy
- **A day isn't "historical" until it's over.** `compute_daily_rollup()`
  refuses to compute today or a future date — a rollup that could get
  silently smaller on a later rerun (because it was first computed
  mid-day) is a worse failure mode than simply not having today's row
  yet. Live "so far today" numbers are exactly what Phase 12 already
  provides
- **Idempotent by construction, not by convention.** `update_or_create`
  on the unique `date` for the parent row, and a delete-then-recreate of
  that date's vehicle-type rows, means computing the same day twice — a
  retried Celery task, a deliberate backfill rerun after fixing upstream
  data — produces exactly the same result as computing it once, never a
  double-counted one. A dedicated test recomputes a day after deleting
  one of its underlying `DriverEarning` rows and confirms the stale
  vehicle-type row disappears rather than lingering
- **Scheduled via plain Celery beat** (`config/celery.py`), not
  `django-celery-beat` — one fixed daily job (00:15 UTC, computing
  yesterday) doesn't need a database-editable schedule, so this adds the
  smallest amount of new machinery that does the job. The task is a thin
  wrapper around the same idempotent service function the management
  command uses, following Phase 10's `deliver_notification_task` pattern:
  retry only on genuine infrastructure errors, not on a `ValueError` from
  being asked to compute a day that isn't closed yet
- **`backfill_daily_rollups` management command** for standing the table
  up from a fresh dataset (`--days N`) or recomputing a specific range
  after fixing a bug upstream (`--start`/`--end`), with the same
  "only closed days" guard as the task
- **Deliberately reuses Phase 12's permissions (`ride.manage`,
  `payment.refund`) rather than inventing a new one.** The historical
  operations trend is gated the same as the live dashboard; the
  historical revenue trend is gated the same as the live revenue report
  — continuing the "permission-specific, not role-tiered" RBAC philosophy
  from Phase 1 instead of introducing a coarser `analytics.read` that
  would either over- or under-grant relative to what Phase 12 already
  established
- **Rates are computed from summed counts, not averaged per day.**
  `get_operations_summary()`'s window-level acceptance rate is
  `sum(accepted) / sum(sent)` across the window, not the mean of each
  day's own rate — the latter silently over-weights low-volume days. A
  dedicated test constructs a case where the two methods would disagree
  (25% vs. a naive 50%) and asserts the correct one
- **`days_available` is reported alongside every summary.** Asking for a
  30-day window on a platform that's only 3 days old returns honest
  3-day totals with `days_available: 3`, rather than silently implying a
  full 30 days of history exists
- **Independent of `apps.admin_api` by design**, per the architecture
  doc's bounded contexts — Admin ("composes other contexts") and
  Reporting & Analytics ("read-only projections") are two separate
  contexts that happen to answer similar operational questions on
  different time axes, so each reads directly from the source apps
  (`rides`, `dispatch`, `payments`, `earnings`) rather than one
  composing the other
- **The roadmap's own testable criterion, directly encoded as a test**:
  `test_revenue_matches_a_manual_query_against_raw_data` computes a day's
  rollup and asserts it against a hand-fetched `DriverEarning` row rather
  than another aggregate query
- 23 new automated tests, passing on the first run: rollup computation
  (empty-day zeroes, ride-outcome counts, window exclusion, revenue
  reconciliation, vehicle-type-breakdown reconciliation, distinct-driver
  counting, dispatch acceptance rate, idempotent recompute, stale-row
  cleanup), range backfill, both summary services (including the
  rate-weighting and `days_available` cases), and the full permission
  matrix across the four endpoints
