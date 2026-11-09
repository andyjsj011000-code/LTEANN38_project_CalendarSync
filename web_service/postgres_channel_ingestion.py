"""Shared freshness and fenced leases for already Graph-verified school channels."""

import secrets


class PostgresChannelIngestion:
    def _ingestion_identity(self, channel):
        if (
            not isinstance(channel, dict)
            or channel.get("tenant_id", self.tenant_id) != self.tenant_id
            or not isinstance(channel.get("id"), str)
            or not 1 <= len(channel["id"]) <= 200
            or not isinstance(channel.get("team_id"), str)
            or not 1 <= len(channel["team_id"]) <= 200
        ):
            raise ValueError("A verified school channel is required")
        return channel["id"], channel["team_id"]

    @staticmethod
    def _freshness(value):
        if type(value) is not int or not 0 <= value <= 86400:
            raise ValueError("Invalid channel freshness interval")

    def claim_channel_ingestion(self, channel, freshness_seconds=300):
        self._freshness(freshness_seconds)
        identity, team = self._ingestion_identity(channel)
        with self.connect() as db:
            db.execute(
                """INSERT INTO web_channel_ingestion
                (channel_id,tenant_id,team_id) VALUES(%s,%s,%s)
                ON CONFLICT(channel_id) DO NOTHING""",
                (identity, self.tenant_id, team),
            )
            row = db.execute(
                """SELECT tenant_id,team_id,checked_at,lease_until
                FROM web_channel_ingestion WHERE channel_id=%s FOR UPDATE""",
                (identity,),
            ).fetchone()
            if row[:2] != (self.tenant_id, team):
                raise ValueError("Channel ingestion identity mismatch")
            now = int(self.clock())
            if row[3] > now or (row[2] > 0 and row[2] + freshness_seconds > now):
                return None
            lease = secrets.token_hex(32)
            db.execute(
                """UPDATE web_channel_ingestion SET lease_token=%s,lease_until=%s
                WHERE channel_id=%s""",
                (lease, now + 180, identity),
            )
        return lease

    def channel_ingestion_fresh(self, channel, freshness_seconds=300):
        self._freshness(freshness_seconds)
        identity, team = self._ingestion_identity(channel)
        with self.connect() as db:
            row = db.execute(
                """SELECT checked_at FROM web_channel_ingestion
                WHERE channel_id=%s AND tenant_id=%s AND team_id=%s""",
                (identity, self.tenant_id, team),
            ).fetchone()
        return bool(row and row[0] > 0 and row[0] + freshness_seconds > int(self.clock()))

    def _active_channel_lease(self, db, channel_id, lease):
        if not isinstance(lease, str) or not lease:
            return False
        row = db.execute(
            """SELECT lease_token,lease_until FROM web_channel_ingestion
            WHERE channel_id=%s AND tenant_id=%s FOR UPDATE""",
            (channel_id, self.tenant_id),
        ).fetchone()
        return bool(row and row[0] == lease and row[1] > int(self.clock()))

    def finish_channel_ingestion(self, channel, lease, success):
        identity, _ = self._ingestion_identity(channel)
        if type(success) is not bool:
            raise ValueError("Explicit channel completion is required")
        with self.connect() as db:
            if not self._active_channel_lease(db, identity, lease):
                return False
            db.execute(
                """UPDATE web_channel_ingestion SET lease_token=NULL,lease_until=0,
                checked_at=CASE WHEN %s THEN %s ELSE checked_at END WHERE channel_id=%s""",
                (success, int(self.clock()), identity),
            )
        return True
