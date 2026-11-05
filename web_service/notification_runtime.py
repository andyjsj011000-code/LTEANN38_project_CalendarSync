"""Explicitly wired event worker. Candidate lookup is not an access proof."""
from .notification_worker import process_claimed


class NotificationRuntime:
    def __init__(self, inbox, db, tokens, graph, source, access_seconds):
        self.inbox,self.db,self.tokens,self.graph,self.source = inbox,db,tokens,graph,source
        self.access_seconds=access_seconds

    def all_candidates(self, job):
        # Access rows are intentionally cleared before a Graph revalidation.
        # Retain hints for every known subscription in this tenant before any
        # user's full ACL can be cleared. web_channels does not store team IDs;
        # use only the trusted registered subscription mapping for each channel.
        # A hint never proves access: process_claimed rechecks Graph every time.
        now=int(self.db.clock())
        with self.db.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS graph_candidate_hints (
                tenant_id TEXT NOT NULL,team_id TEXT NOT NULL,channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
                PRIMARY KEY(tenant_id,team_id,channel_id,user_id))''')
            db.execute('''INSERT OR IGNORE INTO graph_candidate_hints
                (tenant_id,team_id,channel_id,user_id)
                SELECT u.tenant_id,s.team_id,c.id,u.id FROM web_channel_access a
                JOIN web_users u ON u.id=a.user_id
                JOIN web_channels c ON c.id=a.channel_id
                JOIN graph_subscriptions s ON s.tenant_id=c.tenant_id
                    AND s.channel_id=c.id AND s.active=1
                WHERE u.active=1 AND u.tenant_id=? AND c.tenant_id=u.tenant_id''',
                (job['tenant_id'],))
            rows=db.execute('''SELECT DISTINCT d.id_hash FROM web_connections d
                JOIN web_users u ON u.id=d.user_id
                JOIN graph_candidate_hints h ON h.user_id=u.id
                WHERE d.active=1 AND d.background=1 AND d.expires_at>?
                AND d.state<>'reauth_required' AND u.active=1
                AND u.tenant_id=? AND h.tenant_id=? AND h.team_id=? AND h.channel_id=?
                ORDER BY d.id_hash''',
                (now,job['tenant_id'],job['tenant_id'],job['team_id'],job['channel_id'])).fetchall()
        return [d for row in rows if (d:=self.db.device(row[0]))]

    def candidates(self, job):
        devices=self.all_candidates(job)
        if not devices:
            return []
        # Retry backoff is at least 60 seconds. Rotate the bounded batch so an
        # authorised account after the first ten eventually gets a chance.
        start=(int(self.db.clock())//60*10)%len(devices)
        return (devices[start:]+devices[:start])[:10]

    def tick(self):
        job=self.inbox.claim(seconds=300)
        if not job:
            return 'empty'
        result='no_authorized_connection'
        try:
            for device in self.candidates(job):
                result=process_claimed(job,device,self.db,self.tokens,self.graph,self.source,self.access_seconds)
                if result=='processed':
                    return result
            return result
        finally:
            self.inbox.finish(job,result=='processed',retry_delay=60 if result!='processed' else 0)
