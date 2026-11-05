# Teams notification implementation: work in progress

This isolated development snapshot is based on fork `oracle` commit
`fa8de299e99171d40ca6e98082fa5d2effdc994e`. It is not a deployable release.

Latest verification (2026-11-05): source commit
`1deea99ecb5e0a15940ae4a370bde92b589caa82` passed 541 tests, with one existing
test-client warning and zero skips. All 24 native PostgreSQL 18 cases passed;
these include atomic schema creation, preservation/refusal of existing schemas,
rollback of a middle-DDL failure and hosted first-start/restart behavior.
Evidence: https://github.com/andyjsj011000-code/LTEANN38_project_CalendarSync/actions/runs/42001139649
The default bootstrap setting remains off. The new Render draft explicitly opts
in for the user's confirmed dedicated database. No real schema creation or
Render deployment has occurred. The browser form is prepared, but DATABASE_URL,
school tenant ID, the existing Entra client-secret value and the AI API key need
the user's private input. Generated data-encryption/push keys stay outside source
control and source archives. Original Oracle daemon files are unchanged.

Previous completed verification (2026-11-05): GitHub Actions on source commit
`4682f345af5f50deef7fe5a0dcc85114920e8c8e` passed all 504 web tests, with one
existing test-client deprecation warning and no skipped tests. The legacy
synthetic-launcher package tests were explicitly excluded. The job verified a
real disposable PostgreSQL 18 server (server_version_num 180006), then separately
reran the native modules: auth 1, adapter/connection factory 5, queue 5, real web
routes 6, personal tasks 3: 20 passed, zero skipped.
Evidence: https://github.com/andyjsj011000-code/LTEANN38_project_CalendarSync/actions/runs/41995178273
Real web-route checks use the actual PostgreSQL connection factory and adapter;
school identity, Graph, AI and push delivery providers remain simulated. These
results do not establish real Render TLS, live school sign-in, production
migrations, large-group throughput or external delivery. No actual Render or
Oracle resources were changed. Web source-package tests also passed in CI.

New first-start work: the user confirmed the Render database is new, beta-only
and unused by another service. `postgres_bootstrap.py` prepares a creation-only
transaction for an absent dedicated schema (35 tables, 4 indexes). The explicit
`WEB_BOOTSTRAP_NEW_SCHEMA=1` hosted option creates only an absent namespace and
otherwise leaves existing objects untouched while the adapter validates them.
The default is off. Config/schema preflight remains read-only. New bootstrap and
worker-recovery tests passed the CI run above; no real Render DDL was executed.

The hosted origin can use Render's own RENDER_EXTERNAL_URL when WEB_ORIGIN is
unset. The web worker now contains audit failures so temporary DB outages do
not terminate its retry loop. This preserves the existing polling cadence.

Historical local verification (2026-11-05): the restricted runner completed with
398 passed and 13 skipped. The skips require a real disposable PostgreSQL DB.
Packaging tests were explicitly excluded. Python-level external connection
and child-process restrictions reported zero denied operations; this does
not establish isolation of native extensions or the surrounding tool host.
An earlier run exposed silent legacy HTML-to-text truncation, which was fixed
for exact-target ingestion before the passing runs. The latest run includes
per-user fanout, actual SQLite storage integration with two simulated Graph users,
candidate-hint preservation, encrypted PostgreSQL device/cache candidates and the
new PostgreSQL sync candidate's SQLite-shim tests. The latest checks also exercise
the combined PostgreSQL adapter through the real web routes using a SQLite SQL
shim: login, refresh, individual tasks, review, override, reminders, push delivery,
revocation and full logout. These are local behavior checks,
not real PostgreSQL locking, Graph or Render tests. One existing FastAPI/Starlette
test-client deprecation warning remains.

The additional local run was blocked by automatic approval review and replaced
by the successful isolated CI above. `.github/workflows/postgres-beta.yml` uses an isolated
PostgreSQL 18 service on pushes to this fork's oracle branch. It uses only
disposable credentials and checks JUnit evidence that all native PostgreSQL
integration modules ran without skips. The linked Actions run records the result
for this source; writing a workflow alone does not establish a pass.

`postgres_connection.py` adds a lazily imported psycopg 3 transaction factory
with explicit schema, timeout, TLS and redacted diagnostics. `hosted.py` adds
configuration-only and read-only schema preflight plus a separate explicit beta
factory. The existing default hosted guard remains intact. Render environment
and Blueprint examples are review templates only; no live service was changed.
`tools/build_web_release.py` builds a deterministic allowlisted source ZIP,
excluding local credentials, databases, logs and the legacy daemon application.
The two missing PNG icons were restored unchanged from the original base commit.

Implemented and tested using simulated Graph responses: the Graph subscription
API adapter, webhook validation and receipt, a durable notification queue with
leases and retries, a channel-restricted worker that rechecks delegated access,
and a durable per-channel subscription registry. A runtime can be injected into
the test server: it mounts the webhook and processes jobs while suppressing the
old automatic direct-source polling. The normal entry point does not inject it.

The beta continues to target the previously selected five-minute polling approach.
The experimental subscription/fanout path below is optional future work; it is
not enabled by this storage integration. Timer cadence and AI/source budgets still
require load validation before promising a five-minute result to every student.

Work required before release:

* Complete Render-specific PostgreSQL provisioning and deployment validation.
  `web_service/postgresql_schema_draft.sql` is a non-deployable schema draft,
  not an automatic migration. `postgres_database.py` now combines the explicit
  PostgreSQL equivalents and can be supplied as the `database` argument of
  `create_test_server`. Its `initialize()` only validates schema columns/version
  and the single-school tenant boundary; it never provisions or migrates tables.
  The default hosted entry point does not select this experimental adapter.
  SQLite SQL-shim and the linked real PostgreSQL integration checks passed.
  Core conflicts, rollback and shared-user transactions are covered; exhaustive
  concurrency, load and deployed connection behavior remain pending.
  The Render startup guard intentionally rejects hosted runs until this is done.
  `postgres_notification_queue.py` is an isolated PostgreSQL queue candidate;
  its logic has been exercised with a SQLite shim and PostgreSQL 18. The opt-in
  candidate now sorts multi-subscription batches before locking to reduce
  reversed-order deadlocks; reverse-order batches and revision/target tests passed
  on the disposable PostgreSQL server.
  integration tests require psycopg 3 and an explicitly designated disposable
  database through `CALENDARSYNC_DISPOSABLE_POSTGRES_DSN`, plus explicit
  `CALENDARSYNC_ALLOW_TEMP_SCHEMA=yes`. Never use a real
  student or shared database. Core modules for personal tasks, preferences,
  channels, login/session flows, devices, source caching and sync are now composed
  by the experimental adapter. Personal-task/preferences operations check and lock
  the active user. Channel reads also enforce the user's own tenant. Full device
  logout is atomic under the user -> sorted devices lock order, with sessions,
  credentials, push, ACL and sync state revoked together. A fresh login ordered
  after logout remains allowed. Already authenticated requests do not gain a
  universal cancellation guarantee merely from the user row lock.
  `postgres_events.py` adds event/calendar views, per-user overrides and source
  review decisions with ACL-expiry checks and rollback. `postgres_operations.py`
  implements rate limits, audit, feedback, scheduled work, cursor and maintenance.
  Device mutations lock the user before the device and reject expired or
  replaced refresh leases. `postgres_source_cache.py` is a source-cache
  candidate: writes enforce the deterministic channel/message key, preserve source
  identity, and reject payloads from another channel. The cache schema still has no
  tenant column; only verified channels from the configured single-school tenant may
  be passed. The combined adapter supports the tested web-server contract but
  remains experimental; real Render schema provisioning, deployed connection
  configuration and optional subscription registry integration remain pending.
  `postgres_sync.py` is the sync/snapshot candidate. It uses an opaque
  random `lease_token` distinct from `lease_until`, fences same-second retries and
  expired leases, and rechecks user/device ownership, tenant, version, state and
  expiry under user -> device -> sync locks. Shared channels/events use sorted
  identity order and atomic identity guards. Snapshot failures roll back all ACL,
  event and review writes, including expiry after waiting on shared row locks.
  Token callers now reload the current device after refresh before fetching and
  committing source results. `postgres_push.py` requires opaque delivery tokens;
  `Notifications.dispatch` uses them when present and rejects stale completion.
  A final pre-send device check reduces logout races; storage leases cannot make
  external push sending exactly-once or retract a send already in progress.
  Both sync and delivery tables now have nullable lease_token columns in the
  draft. Existing databases require explicit migrations; none were applied.
  `requirements-postgres-experimental.txt` lists the optional driver dependencies;
  installation succeeded in CI; the local Windows installation was not changed.
  The adapter/connection factory and real web-route checks passed as part of the
  20 native integration cases above.
* Before enabling notification mode: wire production configuration, subscription creation, renewal and lifecycle
  handling; select and revalidate the owner and handle revocation and transfer.
* Resolve ambiguous creation/renewal outcomes and recover after restart; expose
  feed health and reconcile any gap. Unknown early deliveries currently receive
  HTTP 503, but recovery after Graph stops retrying is not implemented.
* Exact root-message processing is staged behind the SQLite and PostgreSQL queues'
  `track_targets=True`. A strict parser matches the trusted subscription's
  team/channel, persists root IDs with revisions, and passes explicit targets
  through the access-checked worker to `DirectSource.collect`. Explicit roots
  are fetched directly, including those older than 30 days. Recognised reply
  changes select their root and require a bounded complete reply traversal.
  Unknown resources and oversized batches stay pending as reconciliation work.
  Local mocked-Graph and SQLite logic tests passed; no live service tests ran.
  Exact-target PostgreSQL parity and complete-reply traversal are implemented
  as candidates; queue parity passed native tests, but live Graph behavior remains
  untested. Targeted threads fail closed above 20 reply
  pages, 500 replies, 16000 text characters or the shared time budget; reply
  images are deferred until their resource fetching is implemented.
  Reconciliation execution and production
  configuration remain missing. The default queue still uses
  a bounded recent-channel scan and cannot guarantee complete processing.
  Selected-channel scans now fail on a deadline overrun instead of acknowledging
  partial work; local regression tests passed.
* Candidate lookup now keeps access history as an untrusted user-ID hint, including
  all registered channels in the tenant before an individual sync clears its ACL.
  The trusted subscription mapping provides team IDs, which the channel table lacks.
  The old runtime rotates through batches of ten, still acknowledging on the first
  successful user; it must not be used to guarantee updates for all users.
  `notification_fanout.py` is a separate, unconnected SQLite candidate that atomically
  persists work for each known active user before acknowledging a channel revision.
  Children survive restart, retain later revisions, recheck delegated Graph access
  independently, and complete separately. Ready-time ordering prevents failed jobs
  from starving other users on a minute tick. Damaged encrypted targets are preserved
  as blocked jobs; content-free counts expose the need for explicit repair. Staging
  failures do not stop children that were already saved. Existing experimental
  `graph_user_jobs` tables without `blocked_reason` require an explicit migration;
  the module rejects them without changing that table. Live Graph checks are still
  required. Initial hints can still be missing if ACLs disappeared before first seed
  or before the channel's subscription was registered. PostgreSQL fanout parity,
  cleanup and repair policies, reconciliation, scheduler throughput, reminder access
  expiry and large-channel load remain incomplete. One child is processed per tick;
  this does not establish a five-minute update guarantee for a large beta group.
* Revisit per-user access expiry and reminder delivery in notification mode;
  removing scheduled refresh can prevent existing reminders from being delivered.
* Validate the full package and live behavior with a dedicated HTTPS endpoint,
  school-approved delegated permissions and test accounts. The Teams
  `chatMessage` resource does not supply the Outlook-specific `missed` event.

Do not enable this code or replace the existing CalendarSync daemon until these
items are resolved and reviewed. No server changes were made by this work.
