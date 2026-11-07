# Shared Teams channel ingestion

The PostgreSQL beta can reuse one channel's extracted notices across authorized
accounts. Enable it with `WEB_SHARED_CHANNEL_INGESTION=1`; the default is off.
This switch installs only the additive `web_channel_ingestion` table in the
dedicated web schema. Existing user rows and the legacy Oracle service are not
migrated. Startup validates the existing base schema and school tenant.

Each account still proves its own current channel access through delegated
Microsoft Graph. A cached channel is never evidence of membership. Foreground
reads publish only that account's verified catalog. Manual import commits the
authorized cache and durably queues collection; the response starts the work in
the background and the existing worker retries after a restart.
While initial access verification is still running, the browser performs a
bounded background read retry and keeps open drafts rather than getting stuck
on an empty checking screen.

Successful channel scans, including empty scans, are shared for five minutes.
A 180-second channel lease prevents simultaneous collectors from duplicating
the same analysis. Cache writes and completion are fenced by the current lease.
Failed or incomplete scans remain due. Exact notification work bypasses the
freshness shortcut, and completing one targeted post does not mark the entire
channel fresh. Membership and credentials are verified again before publishing
the background result to an account. The final verification owns the user's
snapshot lease while retaining only an existing ACL's original expiry; it never
extends that access before verification succeeds. Concurrent checks cannot
replace a newer permission decision with an older in-flight Graph response.

With this feature enabled, bounded maintenance retires common Teams events
after seven full days following their end date, using Asia/Seoul calendar dates.
It preserves recent or future dates in the shared cache, base event, personal
overrides, and all pending change reviews. Personal tasks are not modified.
The encrypted source cache keeps lightweight post/version and event-key markers
so an unchanged old post is not analyzed again or restored by a stale snapshot.
Retained markers contain no notice title, notes, or images. Mixed notices keep
their future events. The old 90-day cache eviction is disabled in this mode.

Retention and snapshot commits lock cache rows before event rows in a stable
order. Shared snapshot commits reread the locked cache rather than publishing an
older caller-supplied catalog. Cleanup explicitly removes event dependencies in
the same transaction; invalid dates or payloads roll back the maintenance batch.

The current build can be switched back to account-based collection by setting
the flag to `0`. Keep the current cache decoder: builds from before this change
cannot read the new encrypted cache envelopes after retention has run.

Validation includes mocked provider tests, cross-account collection regression
tests, real PostgreSQL lease/fencing tests, and real PostgreSQL retention and
stale-snapshot tests. No real school token or external provider is used by CI.
