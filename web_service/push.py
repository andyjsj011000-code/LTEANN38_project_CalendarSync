"""Web Push with endpoint validation, encrypted subscriptions and private payloads."""

import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import Field, model_validator

from .config import PushConfig
from .reminder_rules import reminder_times, within_reminder_window
from .schemas import Input
from .storage import digest

KST = timezone(timedelta(hours=9))


def unbase64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def validate_endpoint(endpoint):
    url = urlsplit(endpoint)
    host = url.hostname or ""
    allowed = host in {
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
    } or host.endswith(".notify.windows.com")
    if (
        not allowed
        or url.scheme != "https"
        or url.username
        or url.password
        or url.port not in {None, 443}
        or url.fragment
        or not url.path
        or len(endpoint) > 4096
    ):
        raise ValueError("Unsupported push endpoint")
    return endpoint


class PushKeys(Input):
    p256dh: str = Field(min_length=80, max_length=100, pattern=r"^[A-Za-z0-9_-]+=*$")
    auth: str = Field(min_length=20, max_length=30, pattern=r"^[A-Za-z0-9_-]+=*$")


class Subscription(Input):
    endpoint: str = Field(max_length=4096)
    expirationTime: float | None = None
    keys: PushKeys

    @model_validator(mode="after")
    def validate_subscription(self):
        validate_endpoint(self.endpoint)
        public = unbase64(self.keys.p256dh)
        if len(public) != 65 or len(unbase64(self.keys.auth)) != 16:
            raise ValueError("Invalid push keys")
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
        return self


class PushSender:
    def __init__(self, private="", subject=""):
        self.private, self.subject = private, subject
        self.public = ""
        if bool(private) != bool(subject):
            raise ValueError("Configure both VAPID values")
        if private:
            key = serialization.load_der_private_key(unbase64(private), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
                key.curve, ec.SECP256R1
            ):
                raise ValueError("VAPID requires P-256")
            if not subject.startswith(("mailto:", "https://")):
                raise ValueError("Invalid VAPID contact")
            self.public = b64(
                key.public_key().public_bytes(
                    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
                )
            )

    @property
    def configured(self):
        return bool(self.public)

    def send(self, subscription, payload):
        import requests
        from pywebpush import WebPushException, webpush

        validated = Subscription.model_validate(subscription).model_dump()

        class NoRedirectSession(requests.Session):
            def request(self, method, url, **kwargs):
                validate_endpoint(url)
                kwargs["allow_redirects"] = False
                kwargs["timeout"] = 10
                return super().request(method, url, **kwargs)

        try:
            with NoRedirectSession() as client:
                client.trust_env = False
                response = webpush(
                    subscription_info=validated,
                    data=json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=self.private,
                    vapid_claims={"sub": self.subject},
                    timeout=10,
                    ttl=600,
                    requests_session=client,
                )
            return "sent" if response.status_code in {200, 201, 202} else "retry"
        except WebPushException as error:
            status = error.response.status_code if error.response is not None else None
            return "expired" if status in {404, 410} else "retry"
        except Exception:
            return "retry"


def sender_from_config(config: PushConfig) -> PushSender:
    return PushSender(config.private, config.subject)


def sender_from_env(environ=None):
    return sender_from_config(PushConfig.from_env(environ))


class Notifications:
    def __init__(self, db, sender):
        self.db, self.sender = db, sender

    # 아침 알림과 종료 시각 기준 예약 알림 후보를 계산하며 완료·취소·변경 검토 중인 항목을 제외한다.
    def due(self, user, tenant, *, only_key=None):
        now = datetime.fromtimestamp(self.db.clock(), KST)
        today = now.date().isoformat()
        prefs = self.db.preferences(user)
        pending = {c["event_id"] for c in self.db.changes(user, tenant)}
        candidates = []
        # Keep the morning Teams and personal notifications separate.
        morning = datetime.fromisoformat(today + "T" + prefs["morning_time"]).replace(tzinfo=KST)
        if (
            (only_key is None or only_key.startswith("morning:"))
            and prefs["morning_enabled"]
            and within_reminder_window(now, morning)
        ):
            agenda = self.db.agenda(user, tenant, today)
            for group in ("teams", "personal"):
                visible = [
                    e
                    for e in agenda[group]
                    if not e["completed"] and not e["cancelled"] and e["id"] not in pending
                ]
                key = "morning:" + today + ":" + group
                if visible and (only_key is None or only_key == key):
                    candidates.append(
                        (
                            key,
                            {
                                "title": "CalendarSync",
                                "body": "학교 일정을 확인할 시간이에요."
                                if group == "teams"
                                else "개인 할 일을 확인할 시간이에요.",
                                "url": "/",
                                "tag": "morning-" + today + "-" + group,
                            },
                        )
                    )
        if only_key is not None and only_key.startswith("morning:"):
            return candidates
        for item in self.db.calendar(user, tenant, "0001-01-01", "9999-12-31"):
            if only_key is not None and not only_key.startswith(
                "item:" + item["source"] + ":" + item["id"] + ":"
            ):
                continue
            if item["completed"] or item["cancelled"] or item["id"] in pending:
                continue
            deadline = datetime.fromisoformat(
                item["end_date"] + "T" + (item.get("time") or "23:59")
            ).replace(tzinfo=KST)
            rule = self.db.rules(user, item["id"], item["source"])
            times = reminder_times(deadline, rule["offsets"], rule["custom_times"])
            for due in times:
                if within_reminder_window(now, due):
                    key = "item:" + item["source"] + ":" + item["id"] + ":" + due.isoformat()
                    if only_key is None or only_key == key:
                        candidates.append(
                            (
                                key,
                                {
                                    "title": "CalendarSync",
                                    "body": "확인할 학교 일정이 있어요."
                                    if item["source"] == "teams"
                                    else "확인할 개인 할 일이 있어요.",
                                    "url": "/",
                                    "tag": digest(key)[:24],
                                },
                            )
                        )
        return candidates

    # 발송 처리권 확보 후 장치·일정·현재 구독을 다시 확인하고 전송 결과에 따라 재시도나 구독 정리를 수행한다.
    def dispatch(self, device):
        if not self.sender.configured or not self.db.device(device["id_hash"]):
            return 0
        sent = 0
        for subscription in self.db.subscriptions(device["id_hash"]):
            for key, payload in self.due(device["user_id"], device["tenant_id"]):
                delivery = digest(subscription["id"] + "|" + key)
                if not self.db.device(device["id_hash"]):
                    return sent
                fenced = hasattr(self.db, "claim_delivery_lease")
                lease = (
                    self.db.claim_delivery_lease(delivery, subscription["id"])
                    if fenced
                    else self.db.claim_delivery(delivery, subscription["id"])
                )
                if not lease:
                    continue
                # A successful storage claim may race a logout. Recheck before
                # contacting the external push endpoint; completion also fences.
                current = self.db.device(device["id_hash"])
                if not current or current.get("state") == "reauth_required":
                    return sent
                # Candidates can wait behind claims or earlier sends. Respect
                # task state and reminder changes made since the initial list.
                try:
                    eligible = bool(self.due(device["user_id"], device["tenant_id"], only_key=key))
                    # A user can disable this subscription, or refresh its
                    # browser keys, while the delivery claim is waiting. Read
                    # its current value after the potentially slow due check.
                    current_subscription = (
                        next(
                            (
                                value
                                for value in self.db.subscriptions(device["id_hash"])
                                if value["id"] == subscription["id"]
                            ),
                            None,
                        )
                        if eligible
                        else None
                    )
                except Exception:
                    if fenced:
                        self.db.finish_delivery_lease(delivery, lease, False)
                    else:
                        self.db.finish_delivery(delivery, False)
                    raise
                if not eligible or current_subscription is None:
                    if fenced:
                        self.db.finish_delivery_lease(delivery, lease, False)
                    else:
                        self.db.finish_delivery(delivery, False)
                    continue
                current = self.db.device(device["id_hash"])
                if not current or current.get("state") == "reauth_required":
                    return sent
                try:
                    outcome = self.sender.send(current_subscription["subscription"], payload)
                except Exception:
                    outcome = "retry"
                if outcome == "expired":
                    self.db.unsubscribe_subscription(device["id_hash"], subscription["id"])
                    break
                if fenced:
                    self.db.finish_delivery_lease(delivery, lease, outcome == "sent")
                else:
                    self.db.finish_delivery(delivery, outcome == "sent")
                sent += int(outcome == "sent")
        return sent
