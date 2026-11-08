"""No-network notification decisions are refreshed after the delivery claim."""
from datetime import datetime, timedelta

import pytest

from web_service.push import KST, Notifications


class Database:
    def __init__(self, on_claim=None):
        self.now = datetime(2026, 11, 8, 11, 0, tzinfo=KST)
        self.item = {'id': 'task', 'source': 'personal', 'completed': False, 'hidden': False,
                     'cancelled': False, 'end_date': '2026-11-08', 'time': '12:00'}
        self.offsets, self.pending, self.visible = [-60], [], True
        self.morning_enabled = False
        self.on_claim, self.finished, self.rule_reads = on_claim, [], []
        self.recheck_error = False
        self.subscribed = True
        self.subscription_value = {'endpoint': 'fixture-original'}

    def clock(self): return self.now.timestamp()
    def preferences(self, user):
        return {'morning_time': '11:00', 'morning_enabled': self.morning_enabled}
    def changes(self, user, tenant): return self.pending
    def agenda(self, user, tenant, day):
        return {'teams': [], 'personal': [self.item] if self.visible and not self.item['hidden'] else []}
    def calendar(self, *args):
        if self.recheck_error:
            raise RuntimeError('Synthetic authorization lookup failure')
        return [self.item] if self.visible and not self.item['hidden'] else []
    def rules(self, user, item, source):
        self.rule_reads.append(item)
        return {'offsets': self.offsets, 'custom_times': []}
    def device(self, key): return {'state': 'connected'}
    def subscriptions(self, key):
        return ([{'id': 'subscription', 'subscription': dict(self.subscription_value)}]
                if self.subscribed else [])
    def claim_delivery_lease(self, *args):
        if self.on_claim:
            self.on_claim(self)
        return 'lease'
    def finish_delivery_lease(self, *args): self.finished.append(args)


class Sender:
    configured = True
    def __init__(self): self.sent, self.subscriptions = [], []
    def send(self, subscription, payload):
        self.subscriptions.append(subscription)
        self.sent.append(payload)
        return 'sent'


def dispatch(db):
    sender = Sender()
    result = Notifications(db, sender).dispatch(
        {'id_hash': 'device', 'user_id': 'user', 'tenant_id': 'tenant'})
    return sender, result


@pytest.mark.parametrize('change', [
    lambda db: db.item.update(completed=True),
    lambda db: db.item.update(cancelled=True),
    lambda db: db.item.update(hidden=True),
    lambda db: setattr(db, 'visible', False),
    lambda db: setattr(db, 'offsets', []),
    lambda db: db.item.update(time='13:00'),
    lambda db: setattr(db, 'pending', [{'event_id': db.item['id']}]),
    lambda db: setattr(db, 'now', db.now + timedelta(seconds=600)),
])
def test_state_or_rule_changed_after_claim_prevents_send(change):
    db = Database(change)
    sender, result = dispatch(db)
    assert result == 0 and sender.sent == []
    assert len(db.finished) == 1 and db.finished[0][2] is False


def test_morning_summary_disabled_after_claim_is_not_sent():
    db = Database(lambda value: setattr(value, 'morning_enabled', False))
    db.morning_enabled, db.offsets = True, []
    sender, result = dispatch(db)
    assert result == 0 and sender.sent == []
    assert db.finished[0][2] is False


def test_current_eligible_claim_is_sent_and_finished_once():
    db = Database()
    sender, result = dispatch(db)
    assert result == 1 and len(sender.sent) == 1
    assert len(db.finished) == 1 and db.finished[0][2] is True
    assert db.rule_reads == ['task', 'task']


def test_failed_revalidation_releases_claim_without_sending():
    db = Database(lambda value: setattr(value, 'recheck_error', True))
    sender = Sender()
    with pytest.raises(RuntimeError, match='authorization lookup'):
        Notifications(db, sender).dispatch({'id_hash': 'device', 'user_id': 'user', 'tenant_id': 'tenant'})
    assert sender.sent == []
    assert len(db.finished) == 1 and db.finished[0][2] is False


def test_rechecking_one_key_does_not_read_unrelated_item_rules():
    db = Database()
    other = {**db.item, 'id': 'other'}
    db.calendar = lambda *args: [db.item, other]
    key = 'item:personal:task:2026-11-08T11:00:00+09:00'
    result = Notifications(db, Sender()).due('user', 'tenant', only_key=key)
    assert [value[0] for value in result] == [key]
    assert db.rule_reads == ['task']


def test_unsubscribe_after_claim_prevents_external_send():
    db = Database(lambda value: setattr(value, 'subscribed', False))
    sender, result = dispatch(db)
    assert result == 0 and sender.sent == []
    assert len(db.finished) == 1 and db.finished[0][2] is False


def test_unsubscribe_during_the_due_recheck_prevents_external_send():
    db = Database()
    calendar = db.calendar
    calls = []
    def changed_while_checking(*args):
        calls.append(args)
        if len(calls) == 2:
            db.subscribed = False
        return calendar(*args)
    db.calendar = changed_while_checking
    sender, result = dispatch(db)
    assert len(calls) == 2 and result == 0 and sender.sent == []
    assert len(db.finished) == 1 and db.finished[0][2] is False


def test_refreshed_browser_subscription_keys_are_used_for_send():
    db = Database(lambda value: setattr(value, 'subscription_value', {'endpoint': 'fixture-refreshed'}))
    sender, result = dispatch(db)
    assert result == 1
    assert sender.subscriptions == [{'endpoint': 'fixture-refreshed'}]
    assert len(db.finished) == 1 and db.finished[0][2] is True


def test_failed_subscription_recheck_releases_claim_without_send():
    db = Database()
    subscriptions = db.subscriptions
    calls = []
    def unavailable(*args):
        calls.append(args)
        if len(calls) > 1:
            raise RuntimeError('Synthetic subscription lookup failure')
        return subscriptions(*args)
    db.subscriptions = unavailable
    sender = Sender()
    with pytest.raises(RuntimeError, match='subscription lookup'):
        Notifications(db, sender).dispatch({'id_hash': 'device', 'user_id': 'user', 'tenant_id': 'tenant'})
    assert sender.sent == []
    assert len(db.finished) == 1 and db.finished[0][2] is False


def test_real_unsubscribe_after_claim_cannot_use_the_stale_subscription(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from web_service.storage import digest
    from web_service.test_storage import TestDatabase

    now = datetime(2026, 11, 8, 11, 0, tzinfo=KST).timestamp()
    db = TestDatabase(tmp_path / 'push.sqlite3', Fernet.generate_key().decode(), clock=lambda: now)
    db.initialize()
    user = db.save_user('school', 'alice', {'name': 'Fixture'})
    task = db.add_task(user, {'title': 'Fixture task', 'notes': '',
        'start_date': '2026-11-08', 'end_date': '2026-11-08', 'time': '12:00',
        'display_mode': 'daily', 'selected_dates': []})
    db.save_preferences(user, {'morning_enabled': False, 'morning_time': '07:00',
        'disabled_kinds': [], 'calendar_sync_enabled': False, 'teams_chat_enabled': False})
    db.save_rules(user, task['id'], 'personal', {'offsets': [-60], 'custom_times': []})
    session = db.issue_session(user, 3600)
    raw, _ = db.add_connection(session, user, 'fixture-token', 3600, 'fixture-cache', background=True)
    device = db.device(digest(raw))
    subscription = db.subscribe(device, {'endpoint': 'https://push.example.test/fixture'})
    claim = db.claim_delivery
    claims = []
    def unsubscribe_on_claim(*values):
        result = claim(*values)
        if result:
            claims.append(result)
            db.unsubscribe_subscription(device['id_hash'], subscription)
        return result
    monkeypatch.setattr(db, 'claim_delivery', unsubscribe_on_claim)
    sender = Sender()
    assert Notifications(db, sender).dispatch(device) == 0
    assert len(claims) == 1 and sender.sent == []
    assert db.device(device['id_hash']) is not None
    assert db.subscriptions(device['id_hash']) == []
