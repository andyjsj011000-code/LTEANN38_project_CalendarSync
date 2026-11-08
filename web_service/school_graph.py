"""Delegated Graph reads only; never uses application-wide Graph credentials."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from queue import Empty, Queue
from threading import Event, Lock
from time import monotonic
from urllib.parse import parse_qs, quote, urlsplit
from uuid import UUID
import httpx

SCOPES = ["User.Read", "Team.ReadBasic.All", "Channel.ReadBasic.All", "ChannelMessage.Read.All"]
BASE = "https://graph.microsoft.com/v1.0"

def _graph_stage(parsed):
    parts=parsed.path.strip('/').split('/')
    if parts==['v1.0','me']: return 'member'
    if parts==['v1.0','me','joinedTeams']: return 'joined_teams'
    if len(parts)==4 and parts[:2]==['v1.0','teams'] and parts[3]=='allChannels': return 'team_channels'
    if len(parts)>=6 and parts[:2]==['v1.0','teams'] and parts[3]=='channels' and parts[5]=='messages':
        if len(parts)==6:
            return 'permission_probe' if parse_qs(parsed.query).get('$top')==['1'] else 'source_messages'
        if len(parts)==7: return 'source_message'
        if len(parts)==8 and parts[7]=='replies': return 'source_replies'
    return 'other'

def _graph_log(stage,outcome,status=0,attempt=1):
    try:
        # Fixed labels and numeric fields only: never URLs, identities or bodies.
        logging.getLogger('uvicorn.error').info(
            'CalendarSync Graph stage=%s outcome=%s http_status=%d attempt=%d',
            stage,outcome,status,attempt)
    except Exception:
        pass

class GraphFailure(Exception):
    def __init__(self, state="verification_failed"):
        self.state = state
        super().__init__(state)

class DelegatedGraph:
    def __init__(self, client_factory=None):
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=8, follow_redirects=False, trust_env=False))
        self._probe_lock=Lock()
        self._next_probe=0
        self._channel_probe_times={}

    def _retry_wait(self, seconds, stopped=None):
        return (stopped or Event()).wait(seconds)

    def _retry_throttled(self, response, attempt, deadline, stage, stopped=None):
        _graph_log(stage,'throttled',429,attempt)
        if attempt>=3:
            _graph_log(stage,'retry_exhausted',429,attempt)
            return False
        raw=response.headers.get('retry-after','').strip()
        # An advertised delay beyond our budget must never be shortened.
        if raw.isascii() and raw.isdigit():
            delay=int(raw) if len(raw)<=4 else 10000
            delay=max(1,delay)
        else:
            delay=2**(attempt-1)
        if monotonic()+delay>=deadline:
            _graph_log(stage,'retry_deadline',429,attempt)
            return False
        return not self._retry_wait(delay,stopped)

    def _read(self, client, token, url, *, forbidden_missing=True, deadline=None,
              stopped=None, retry_gate=None):
        parsed = urlsplit(url)
        stage=_graph_stage(parsed)
        if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or not parsed.path.startswith("/v1.0/") or parsed.fragment:
            _graph_log(stage,'invalid_target')
            raise GraphFailure()
        retry_deadline=min(deadline,monotonic()+10) if deadline is not None else monotonic()+10
        for attempt in range(1,4):
            if stopped is not None and stopped.is_set(): return None
            active_deadline=deadline if attempt==1 else retry_deadline
            if active_deadline is not None and monotonic()>active_deadline:
                _graph_log(stage,'deadline',0,attempt)
                raise GraphFailure()
            if attempt>1 and retry_gate is not None and not retry_gate(retry_deadline): return None
            try:
                with client.stream("GET", url, headers={"Authorization":"Bearer "+token}) as response:
                    status=response.status_code
                    if status==429:
                        pass
                    elif status!=200:
                        _graph_log(stage,'http_failure',status,attempt)
                        if status==403 and forbidden_missing: return None
                        if status==404: return None
                        if status==401: raise GraphFailure('reauth_required')
                        raise GraphFailure()
                    else:
                        chunks=[]; size=0
                        for chunk in response.iter_bytes():
                            if active_deadline is not None and monotonic()>active_deadline:
                                _graph_log(stage,'deadline',200,attempt)
                                raise GraphFailure()
                            size += len(chunk)
                            if size > 2_000_000:
                                _graph_log(stage,'response_limit',200,attempt)
                                raise GraphFailure()
                            chunks.append(chunk)
                        import json
                        data=json.loads(b"".join(chunks))
                        if not isinstance(data,dict):
                            _graph_log(stage,'invalid_response',200,attempt)
                            raise GraphFailure()
                        if attempt>1: _graph_log(stage,'recovered',200,attempt)
                        return data
            except httpx.HTTPError:
                _graph_log(stage,'transport_error',0,attempt)
                raise GraphFailure() from None
            except ValueError:
                _graph_log(stage,'invalid_response',200,attempt)
                raise GraphFailure() from None
            if not self._retry_throttled(response,attempt,retry_deadline,stage,stopped):
                if stopped is not None and stopped.is_set(): return None
                raise GraphFailure()

    def _pages(self, client, token, path, deadline):
        url=BASE+path; expected_path=urlsplit(url).path; items=[]; visited=set()
        for _ in range(20):
            if monotonic()>deadline or url in visited or urlsplit(url).path != expected_path:
                _graph_log(_graph_stage(urlsplit(BASE+path)),'pagination_guard')
                raise GraphFailure()
            visited.add(url)
            data=self._read(client,token,url,deadline=deadline)
            if data is None:
                # Failure to enumerate the account's teams is not evidence
                # that it belongs to no teams. Individual denied team routes
                # remain excluded without granting access to their channels.
                if expected_path == '/v1.0/me/joinedTeams':
                    _graph_log('joined_teams','listing_unavailable')
                    raise GraphFailure()
                return []
            values=data.get("value")
            if not isinstance(values,list) or len(items)+len(values)>500:
                _graph_log(_graph_stage(urlsplit(BASE+path)),'invalid_page',200)
                raise GraphFailure()
            items.extend(values)
            url=data.get("@odata.nextLink")
            if not url:
                return items
        _graph_log(_graph_stage(urlsplit(BASE+path)),'page_limit')
        raise GraphFailure()

    def _member(self, client, token, object_id, *, deadline=None):
        me=self._read(client,token,BASE+"/me?$select=id,userType",deadline=deadline)
        if not me or me.get("id", "").lower()!=object_id.lower() or me.get("userType")!="Member":
            _graph_log('member','membership_failed',200)
            raise GraphFailure("account_not_allowed")

    def assert_member(self, token, object_id):
        with self.client_factory() as client:
            self._member(client,token,object_id)

    def _wait_for_probe(self, channel_id, deadline, stopped):
        while not stopped.is_set():
            with self._probe_lock:
                now=monotonic()
                self._channel_probe_times={key:when for key,when in self._channel_probe_times.items() if when>now}
                ready=max(self._next_probe,self._channel_probe_times.get(channel_id,0),now)
                if ready>deadline:
                    raise GraphFailure()
                delay=ready-now
                if delay<=0:
                    self._next_probe=now+0.08
                    self._channel_probe_times[channel_id]=now+1
                    return True
            if stopped.wait(delay):
                break
        return False

    def _permitted_channels(self, token, candidate_groups, deadline):
        if not candidate_groups:
            return []
        pending=Queue()
        for routes in candidate_groups:
            pending.put(routes)
        stopped=Event()

        def probe():
            permitted=[]
            try:
                # Each worker owns its client, including connection cleanup.
                with self.client_factory() as client:
                    while not stopped.is_set():
                        if monotonic()>deadline:
                            raise GraphFailure()
                        try:
                            routes=pending.get_nowait()
                        except Empty:
                            break
                        if stopped.is_set():
                            break
                        # Duplicate listings are tried in order by one worker;
                        # a denied team route must not hide a later allowed one.
                        for index,channel in routes:
                            if not self._wait_for_probe(channel['id'],deadline,stopped) or stopped.is_set():
                                return permitted
                            path=f"/teams/{channel['team_id']}/channels/{quote(channel['id'],safe='')}/messages?$top=1"
                            # A listed channel name alone is not a permission proof.
                            allowed=self._read(client,token,BASE+path,deadline=deadline,stopped=stopped,
                                retry_gate=lambda retry_deadline:self._wait_for_probe(channel['id'],retry_deadline,stopped)) is not None
                            if stopped.is_set(): return permitted
                            if monotonic()>deadline:
                                raise GraphFailure()
                            if allowed:
                                permitted.append((index,channel))
                                break
            except BaseException:
                stopped.set()
                raise
            return permitted

        permitted={}
        with ThreadPoolExecutor(max_workers=min(6,len(candidate_groups))) as pool:
            futures=[pool.submit(probe) for _ in range(min(6,len(candidate_groups)))]
            try:
                for future in as_completed(futures):
                    permitted.update(future.result())
            except BaseException:
                stopped.set()
                permitted.clear()
                for future in futures:
                    future.cancel()
                raise
        return [permitted[index] for index in sorted(permitted)]

    def channels(self, token, object_id, tenant_id):
        deadline=monotonic()+80
        with self.client_factory() as client:
            self._member(client,token,object_id,deadline=deadline)
            teams=self._pages(client,token,"/me/joinedTeams?$select=id,displayName",deadline)
            candidates={}
            routes=set()
            for team in teams:
                team_id=str(UUID(team["id"]))
                team_name=team.get("displayName")
                team_name=team_name.strip() if isinstance(team_name,str) else ""
                channels=self._pages(client,token,f"/teams/{team_id}/allChannels?$select=id,displayName,membershipType,tenantId",deadline)
                for channel in channels:
                    if monotonic()>deadline:
                        raise GraphFailure()
                    # Cross-tenant shared channels are deliberately excluded.
                    if channel.get("tenantId") not in (None,tenant_id) or (channel.get("membershipType")=="shared" and channel.get("tenantId")!=tenant_id):
                        continue
                    channel_id=channel.get("id","")
                    if not isinstance(channel_id,str) or not 1<=len(channel_id)<=200:
                        raise GraphFailure()
                    route=(team_id,channel_id)
                    if route in routes:
                        continue
                    if len(routes)>=300:
                        raise GraphFailure()
                    channel_name=channel.get("displayName")
                    channel_name=channel_name.strip() if isinstance(channel_name,str) else ""
                    channel_name=channel_name or "Teams"
                    name=f"{team_name} · {channel_name}" if team_name else channel_name
                    if len(name)>200:
                        # Keep both parts when long team names would otherwise
                        # truncate the channel that users need to distinguish.
                        name=f"{team_name[:97]} · {channel_name[:100]}" if team_name else channel_name[:200]
                    candidate={"id":channel_id,"team_id":team_id,"name":name}
                    candidates.setdefault(channel_id,[]).append((len(routes),candidate))
                    routes.add(route)
        return self._permitted_channels(token,list(candidates.values()),deadline)
