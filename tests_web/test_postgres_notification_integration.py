"""Opt-in: use only a dedicated disposable PostgreSQL database."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from web_service.notification_queue import UnknownSubscription
from web_service.postgres_notification_queue import PostgresNotificationQueue


@pytest.fixture
def postgres_queue():
    dsn = os.environ.get('CALENDARSYNC_DISPOSABLE_POSTGRES_DSN')
    if not dsn:
        pytest.skip('No dedicated disposable PostgreSQL database configured')
    if os.environ.get('CALENDARSYNC_ALLOW_TEMP_SCHEMA') != 'yes':
        pytest.skip('Disposable database and temporary schema creation must be explicitly enabled')
    psycopg = pytest.importorskip('psycopg')
    schema = 'calendarsync_test_' + uuid.uuid4().hex
    ddl = (Path(__file__).parents[1] / 'web_service' / 'postgresql_schema_draft.sql').read_text(encoding='utf-8')
    ddl = ddl.split('-- The notification worker and registry are also experimental and not wired.', 1)[1]
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute('CREATE SCHEMA ' + schema)
    @contextmanager
    def connect():
        with psycopg.connect(dsn) as connection:
            connection.execute('SET LOCAL search_path TO ' + schema)
            yield connection
    try:
        with connect() as connection:
            # The notification-only schema references web_users for candidate hints.
            connection.execute('CREATE TABLE web_users (id TEXT PRIMARY KEY)')
            statements = '\n'.join(line for line in ddl.splitlines()
                                   if not line.lstrip().startswith('--'))
            for statement in statements.split(';'):
                if statement.strip():
                    connection.execute(statement)
        yield PostgresNotificationQueue(connect, clock=lambda: 1000)
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute('DROP SCHEMA ' + schema + ' CASCADE')


def test_queue_preserves_new_revision_and_only_one_claimant(postgres_queue):
    q = postgres_queue
    q.register('sub', 'secret', 'tenant', 'team', 'channel')
    item = {'subscriptionId': 'sub', 'clientState': 'secret'}
    q.accept([item])
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: q.claim(), range(2)))
    first, = [job for job in claims if job is not None]
    q.accept([item])
    q.finish(first, True)
    second = q.claim()
    assert second['revision'] == 2
    q.finish(second, True)
    assert q.claim() is None


def test_batch_is_rolled_back_when_an_early_subscription_is_unknown(postgres_queue):
    q = postgres_queue
    q.register('known', 'secret', 'tenant', 'team', 'channel')
    with pytest.raises(UnknownSubscription):
        q.accept([{'subscriptionId': 'known', 'clientState': 'secret'},
                  {'subscriptionId': 'unknown', 'clientState': 'secret'}], defer_unknown=True)
    assert q.claim() is None


def test_concurrent_reverse_order_batches_are_retained(postgres_queue):
    q = postgres_queue
    q.register('sub-a', 'secret', 'tenant', 'team', 'channel-a')
    q.register('sub-b', 'secret', 'tenant', 'team', 'channel-b')
    items = [{'subscriptionId': key, 'clientState': 'secret'} for key in ('sub-a', 'sub-b')]
    def deliver(batch):
        for _ in range(10):
            assert q.accept(batch) == 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(deliver, items), pool.submit(deliver, items[::-1])]
        for future in futures:
            future.result(timeout=30)
    first, second = q.claim(), q.claim()
    assert {first['subscription_id'], second['subscription_id']} == {'sub-a', 'sub-b'}
    assert first['revision'] == second['revision'] == 20


def test_concurrent_target_changes_keep_the_new_revision(postgres_queue):
    q = postgres_queue
    q.track_targets = True
    q.register('sub', 'secret', 'tenant', 'team', 'channel')
    item = {'subscriptionId': 'sub', 'clientState': 'secret',
            'resource': "teams('team')/channels('channel')/messages('old-root')/replies('reply')"}
    def deliver(_):
        for _ in range(10):
            assert q.accept([item]) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(deliver, index) for index in range(2)]
        for future in futures:
            future.result(timeout=30)
    first = q.claim()
    assert first['revision'] == 20
    assert first['selected_messages'] == ['old-root']
    assert not first['requires_reconciliation']
    q.accept([item])
    q.finish(first, True)
    second = q.claim()
    assert second['revision'] == 21
    assert second['selected_messages'] == ['old-root']
    q.finish(second, True)
    assert q.claim() is None


def test_late_worker_cannot_delete_another_leases_target(postgres_queue):
    q = postgres_queue
    q.track_targets = True
    now = [1000]
    q.clock = lambda: now[0]
    q.register('sub', 'secret', 'tenant', 'team', 'channel')
    item = {'subscriptionId': 'sub', 'clientState': 'secret',
            'resource': 'teams/team/channels/channel/messages/old-root'}
    q.accept([item])
    expired = q.claim(seconds=1)
    now[0] += 2
    current = q.claim()
    q.finish(expired, True)
    assert q.claim() is None
    q.finish(current, False)
    retry = q.claim()
    assert retry['selected_messages'] == ['old-root']
    q.finish(retry, True)
    assert q.claim() is None
