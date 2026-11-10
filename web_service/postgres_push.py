"""Unwired PostgreSQL push storage with explicit delivery lease tokens.

Factories provide psycopg 3 transactional context managers with tuple rows.
All mutations use the user -> device -> subscription -> delivery lock order;
revocation and other writers must use that same protocol before integration.
Requires the draft web_deliveries.lease_token column. This module does not
initialize a schema or connect to a live database. SQL-shim tests do not prove
PostgreSQL concurrency behavior. A successful claim is not permission to send
after revocation; the caller should recheck its device before an external send.
"""

import secrets
import time

from .push import Subscription
from .storage import Conflict, Missing, digest


class PostgresPush:
    _SUB_FIELDS = ("id", "device_hash", "endpoint_hash", "subscription_blob", "created_at")

    def __init__(self, connect, seal, unseal, clock=time.time):
        self.connect, self.seal, self.unseal, self.clock = connect, seal, unseal, clock

    def _lock_push_device(self, db, device_hash):
        # Ownership is immutable. The preliminary lookup obtains only the
        # identity needed to lock the user before locking the device itself.
        owner = db.execute(
            "SELECT user_id FROM web_connections WHERE id_hash=%s", (device_hash,)
        ).fetchone()
        if owner is None:
            return None
        user = db.execute(
            "SELECT active,tenant_id FROM web_users WHERE id=%s FOR UPDATE", (owner[0],)
        ).fetchone()
        if user is None:
            return None
        row = db.execute(
            """SELECT id_hash,user_id,active,expires_at,background,state
            FROM web_connections WHERE id_hash=%s AND user_id=%s FOR UPDATE""",
            (device_hash, owner[0]),
        ).fetchone()
        return (
            {
                **dict(
                    zip(("id_hash", "user_id", "active", "expires_at", "background", "state"), row)
                ),
                "user_active": bool(user[0]),
                "tenant_id": user[1],
            }
            if row
            else None
        )

    def _push_device_active(self, device):
        return bool(
            device
            and device["user_active"]
            and device["active"]
            and device["expires_at"] > int(self.clock())
            and device["background"]
            and device["state"] != "reauth_required"
        )

    def _lock_push_subscription(self, db, subscription_id):
        identity = db.execute(
            "SELECT device_hash FROM web_push_subscriptions WHERE id=%s", (subscription_id,)
        ).fetchone()
        if identity is None:
            return None, None
        device = self._lock_push_device(db, identity[0])
        row = db.execute(
            """SELECT id,device_hash,endpoint_hash,subscription_blob,created_at
            FROM web_push_subscriptions WHERE id=%s AND device_hash=%s FOR UPDATE""",
            (subscription_id, identity[0]),
        ).fetchone()
        return device, dict(zip(self._SUB_FIELDS, row)) if row else None

    def subscribe(self, device, subscription):
        value = Subscription.model_validate(subscription).model_dump(mode="json")
        endpoint, key, blob = digest(value["endpoint"]), secrets.token_hex(16), self.seal(value)
        with self.connect() as db:
            current = self._lock_push_device(db, device["id_hash"])
            if (
                not self._push_device_active(current)
                or current["user_id"] != device["user_id"]
                or current["tenant_id"] != device["tenant_id"]
            ):
                raise Missing()
            # The device lock serializes the per-device cap. The unique endpoint
            # index arbitrates first-insert races across different device owners.
            old = db.execute(
                """SELECT id,device_hash FROM web_push_subscriptions
                WHERE endpoint_hash=%s""",
                (endpoint,),
            ).fetchone()
            if old:
                if old[1] != current["id_hash"]:
                    raise Conflict()
                db.execute(
                    "UPDATE web_push_subscriptions SET subscription_blob=%s WHERE id=%s",
                    (blob, old[0]),
                )
                return old[0]
            count = db.execute(
                "SELECT count(*) FROM web_push_subscriptions WHERE device_hash=%s",
                (current["id_hash"],),
            ).fetchone()[0]
            if count >= 3:
                raise Conflict()
            inserted = db.execute(
                """INSERT INTO web_push_subscriptions
                (id,device_hash,endpoint_hash,subscription_blob,created_at)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(endpoint_hash) DO NOTHING RETURNING id""",
                (key, current["id_hash"], endpoint, blob, int(self.clock())),
            ).fetchone()
            if inserted is None:
                # An endpoint owned by another device must never be transferred.
                raise Conflict()
        return key

    def subscriptions(self, device_hash):
        with self.connect() as db:
            rows = db.execute(
                """SELECT s.id,s.device_hash,s.endpoint_hash,s.subscription_blob,s.created_at
                FROM web_push_subscriptions s JOIN web_connections d ON d.id_hash=s.device_hash
                JOIN web_users u ON u.id=d.user_id WHERE d.id_hash=%s AND d.active=1
                AND d.expires_at>%s AND d.background=1 AND d.state<>'reauth_required' AND u.active=1
                ORDER BY s.id""",
                (device_hash, int(self.clock())),
            ).fetchall()
        return [
            {**dict(zip(self._SUB_FIELDS, row)), "subscription": self.unseal(row[3])}
            for row in rows
        ]

    def unsubscribe(self, device_hash):
        with self.connect() as db:
            if self._lock_push_device(db, device_hash):
                db.execute(
                    "DELETE FROM web_push_subscriptions WHERE device_hash=%s", (device_hash,)
                )

    def unsubscribe_subscription(self, device_hash, subscription_id):
        with self.connect() as db:
            if self._lock_push_device(db, device_hash):
                db.execute(
                    "DELETE FROM web_push_subscriptions WHERE id=%s AND device_hash=%s",
                    (subscription_id, device_hash),
                )

    # 활성 구독과 발송 기록을 잠그고 중복·재시도 조건을 통과한 요청에만 새 발송 처리권을 준다.
    def claim_delivery_lease(self, key, subscription_id):
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(subscription_id, str)
            or not subscription_id
        ):
            raise ValueError("Delivery and subscription identities are required")
        with self.connect() as db:
            device, subscription = self._lock_push_subscription(db, subscription_id)
            if not subscription or not self._push_device_active(device):
                return None
            now = int(self.clock())
            db.execute(
                """INSERT INTO web_deliveries(id,subscription_id,created_at)
                VALUES(%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                (key, subscription_id, now),
            )
            row = db.execute(
                """SELECT subscription_id,state,attempts,next_attempt,lease_until
                FROM web_deliveries WHERE id=%s FOR UPDATE""",
                (key,),
            ).fetchone()
            now = int(self.clock())
            # A caller-supplied key cannot be reused for another subscription.
            if (
                not self._push_device_active(device)
                or not row
                or row[0] != subscription_id
                or row[1] != "pending"
                or row[2] >= 5
                or row[3] > now
                or row[4] > now
            ):
                return None
            lease = secrets.token_hex(32)
            db.execute(
                """UPDATE web_deliveries SET lease_token=%s,lease_until=%s,
                attempts=attempts+1 WHERE id=%s""",
                (lease, now + 30, key),
            )
        return lease

    # 같은 발송 처리권이 아직 유효할 때만 결과를 저장해 만료되거나 교체된 작업의 결과를 막는다.
    def finish_delivery_lease(self, key, lease, success):
        if not isinstance(lease, str) or not lease:
            return False
        if not isinstance(success, bool):
            raise ValueError("Delivery result must be a boolean")
        with self.connect() as db:
            identity = db.execute(
                "SELECT subscription_id FROM web_deliveries WHERE id=%s", (key,)
            ).fetchone()
            if identity is None:
                return False
            device, subscription = self._lock_push_subscription(db, identity[0])
            if not subscription or not self._push_device_active(device):
                return False
            row = db.execute(
                """SELECT subscription_id,state,lease_token,lease_until
                FROM web_deliveries WHERE id=%s FOR UPDATE""",
                (key,),
            ).fetchone()
            now = int(self.clock())
            if (
                not self._push_device_active(device)
                or not row
                or row[0] != identity[0]
                or row[1] != "pending"
                or row[2] != lease
                or row[3] <= now
            ):
                return False
            db.execute(
                """UPDATE web_deliveries SET state=%s,lease_token=NULL,lease_until=0,
                next_attempt=%s WHERE id=%s""",
                ("sent" if success else "pending", now + 60, key),
            )
        return True

    def claim_delivery(self, key, subscription_id):
        raise RuntimeError("Use claim_delivery_lease and retain the returned lease token")

    def finish_delivery(self, key, success):
        raise RuntimeError("Use finish_delivery_lease with the claimed lease token")

    def has_pending_change(self, user, event):
        with self.connect() as db:
            return bool(
                db.execute(
                    """SELECT 1 FROM web_changes c JOIN web_users u ON u.id=c.user_id
                WHERE c.user_id=%s AND c.event_id=%s AND c.status='pending' AND u.active=1""",
                    (user, event),
                ).fetchone()
            )
