"""Private, delegated Teams assignments. No channel catalog or AI extraction."""
from datetime import datetime, timedelta, timezone
import json
from time import monotonic
from urllib.parse import quote, urlencode, urlsplit

from .connections import acquire_snapshot
from .oracle_source import plain
from .school_graph import BASE, DelegatedGraph, GraphFailure
from .storage import Missing, digest

ASSIGNMENT_SCOPE = 'EduAssignments.ReadBasic'
KST = timezone(timedelta(hours=9))
SNAPSHOT_SCHEMA = """CREATE TABLE IF NOT EXISTS web_assignment_snapshots (
 user_id TEXT PRIMARY KEY REFERENCES web_users(id) ON DELETE CASCADE,
 payload_blob TEXT NOT NULL, checked_at BIGINT NOT NULL)"""


def assignment_link(class_id, assignment_id):
    target = {'version': '1.0', 'config': {'classes': [
        {'id': class_id, 'assignmentIds': [assignment_id]}]},
        'action': 'navigate', 'view': 'assignment-viewer'}
    return ('https://teams.microsoft.com/l/entity/'
            '66aeee93-507d-479a-a3ef-8f494af43945/classroom?' +
            urlencode({'context': json.dumps({'subEntityId': json.dumps(target)})}))


def normalize_assignment(item, object_id, classes):
    if not isinstance(item, dict):
        raise GraphFailure('source_unavailable')
    if item.get('status') in {'draft', 'scheduled'}:
        return None
    class_id, assignment_id = item.get('classId'), item.get('id')
    if not all(isinstance(v, str) and 0 < len(v) <= 200 for v in (class_id, assignment_id)):
        raise GraphFailure('source_unavailable')
    title = item.get('displayName')
    if not isinstance(title, str) or not title.strip():
        raise GraphFailure('source_unavailable')
    due = item.get('dueDateTime')
    local = None
    if due is not None:
        try:
            dt = datetime.fromisoformat(due.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                raise ValueError()
            local = dt.astimezone(KST)
            due = dt.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise GraphFailure('source_unavailable') from None
    submissions = item.get('submissions') or []
    if not isinstance(submissions, list):
        raise GraphFailure('source_unavailable')
    own = [s for s in submissions if isinstance(s, dict) and
           str((s.get('recipient') or {}).get('userId', '')).lower() == object_id.lower()]
    status = own[0].get('status', 'unknown') if len(own) == 1 else 'unknown'
    if status not in {'working', 'submitted', 'returned', 'reassigned', 'excused'}:
        status = 'unknown'
    if item.get('status') == 'inactive':
        status = 'inactive'
    url = item.get('webUrl')
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        if not (parsed and parsed.scheme == 'https' and parsed.hostname == 'teams.microsoft.com'
                and not parsed.username and not parsed.password and parsed.port in (None, 443)):
            url = assignment_link(class_id, assignment_id)
    except ValueError:
        url = assignment_link(class_id, assignment_id)
    instructions = item.get('instructions')
    notes = plain(instructions.get('content', '')) if isinstance(instructions, dict) else ''
    day = local.date().isoformat() if local else None
    return {'id': 'assignment:' + digest(class_id + '|' + assignment_id),
            'source': 'assignment', 'kind': 'assignment', 'title': title.strip()[:500],
            'channel': classes.get(class_id, 'Teams 수업'), 'notes': notes[:10000],
            'start_date': day, 'end_date': day, 'time': local.strftime('%H:%M') if local else None,
            'due_at': due, 'submission_status': status,
            'completed': status in {'submitted', 'returned', 'excused', 'inactive'},
            'cancelled': False, 'source_url': url}


class AssignmentGraph(DelegatedGraph):
    def assignments(self, token, object_id, now):
        deadline = monotonic() + 75
        # Include undated tasks and recent history as well as all future deadlines.
        since = datetime.fromtimestamp(now, timezone.utc) - timedelta(days=60)
        query = urlencode({'$top': 50, '$expand': 'submissions',
                           '$filter': 'dueDateTime ge ' + since.strftime('%Y-%m-%dT%H:%M:%SZ') +
                                      ' or dueDateTime eq null'})
        url = BASE + '/education/me/assignments?' + query
        path = urlsplit(url).path
        seen, raw = set(), []
        with self.client_factory() as client:
            self._member(client, token, object_id, deadline=deadline)
            for _ in range(40):
                if monotonic() > deadline or url in seen or urlsplit(url).path != path:
                    raise GraphFailure('source_unavailable')
                seen.add(url)
                page = self._read(client, token, url, forbidden_missing=False,
                                  forbidden_state='assignment_permission_required', deadline=deadline)
                if page is None or not isinstance(page.get('value'), list):
                    raise GraphFailure('source_unavailable')
                raw.extend(page['value'])
                if len(raw) > 2000:
                    raise GraphFailure('source_unavailable')
                url = page.get('@odata.nextLink')
                if not url:
                    break
            else:
                raise GraphFailure('source_unavailable')
            if not isinstance(url, (str, type(None))):
                raise GraphFailure('source_unavailable')
            try:
                teams = self._pages(client, token, '/me/joinedTeams?$select=id,displayName', deadline)
                classes = {t['id']: str(t.get('displayName') or 'Teams 수업')[:200] for t in teams}
            except (GraphFailure, KeyError, TypeError):
                classes = {}
        result = {}
        for entry in raw:
            item = normalize_assignment(entry, object_id, classes)
            if item is not None:
                result[item['id']] = item
        return sorted(result.values(), key=lambda x: (x['due_at'] or '9999', x['id']))


class AssignmentService:
    """One encrypted snapshot per user, never a shared channel event."""
    def __init__(self, db, tokens, graph=None):
        self.db, self.tokens = db, tokens
        self.graph = graph or AssignmentGraph()
        self.pg = getattr(db, 'storage_backend', '') == 'postgresql-experimental'

    def initialize(self):
        with self.db.connect() as conn:
            conn.execute(SNAPSHOT_SCHEMA)

    def _query(self, sql):
        return sql.replace('?', '%s') if self.pg else sql

    def _current(self, device):
        current = self.db.device(device['id_hash'])
        if not current or current['user_id'] != device['user_id'] or current['state'] == 'reauth_required':
            raise GraphFailure('reauth_required')
        if not self.db.unseal(current['credential_blob']).get('assignment_access'):
            raise GraphFailure('assignment_permission_required')
        return current

    def _cached(self, user_id):
        with self.db.connect() as conn:
            row = conn.execute(self._query(
                'SELECT payload_blob,checked_at FROM web_assignment_snapshots WHERE user_id=?'),
                (user_id,)).fetchone()
        return (self.db.unseal(row[0])['items'], row[1]) if row else ([], 0)

    def _save(self, device, items, checked_at):
        with self.db.connect() as conn:
            if self.pg:
                current = self.db._lock_device(conn, device['id_hash'], device['user_id'])
            else:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT user_id,active,expires_at,credential_blob,state FROM web_connections WHERE id_hash=?',
                                   (device['id_hash'],)).fetchone()
                current = (dict(zip(('user_id', 'active', 'expires_at', 'credential_blob', 'state'), row))
                           if row else None)
            if (not current or not current['active'] or current['expires_at'] <= self.db.clock()
                    or current['state'] == 'reauth_required'
                    or not current.get('user_active', True)
                    or current['user_id'] != device['user_id']
                    or not self.db.unseal(current['credential_blob']).get('assignment_access')):
                raise GraphFailure('reauth_required')
            conn.execute(self._query(
                'INSERT INTO web_assignment_snapshots(user_id,payload_blob,checked_at) VALUES(?,?,?) '
                'ON CONFLICT(user_id) DO UPDATE SET payload_blob=excluded.payload_blob,checked_at=excluded.checked_at '
                'WHERE web_assignment_snapshots.checked_at<=excluded.checked_at'),
                (device['user_id'], self.db.seal({'items': items}), checked_at))

    def read(self, session, *, force=False):
        cached, checked_at = [], 0
        try:
            device = self._current(self.db.session_device(session))
            token, device = acquire_snapshot(self.tokens, device, self.db)
            self._current(device)
            cached, checked_at = self._cached(session['user_id'])
            if not force and checked_at and 0 <= self.db.clock() - checked_at < 60:
                self.graph.assert_member(token, device['object_id'])
                self._current(device)
                return {'items': cached, 'state': 'connected', 'checked_at': checked_at, 'stale': False}
            started = int(self.db.clock())
            items = self.graph.assignments(token, device['object_id'], started)
            self._current(device)
            self._save(device, items, started)
            return {'items': items, 'state': 'connected', 'checked_at': started, 'stale': False}
        except Missing:
            return {'items': [], 'state': 'reauth_required', 'checked_at': 0, 'stale': False}
        except GraphFailure as error:
            # A failed permission check never licenses the old snapshot.
            state = ('permission_required' if error.state == 'assignment_permission_required'
                     else 'reauth_required' if error.state == 'reauth_required'
                     else 'unavailable')
            return {'items': [], 'state': state, 'checked_at': checked_at, 'stale': False}
        except Exception:
            # Source content and tokens never enter diagnostics.
            return {'items': [], 'state': 'unavailable', 'checked_at': checked_at, 'stale': False}

