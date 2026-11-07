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
    def subscriptions(self, key): return [{'id': 'subscription', 'subscription': {}}]
    def claim_delivery_lease(self, *args):
        if self.on_claim:
            self.on_claim(self)
        return 'lease'
    def finish_delivery_lease(self, *args): self.finished.append(args)


class Sender:
    configured = True
    def __init__(self): self.sent = []
    def send(self, subscription, payload):
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
