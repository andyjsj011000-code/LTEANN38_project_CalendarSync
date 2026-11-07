"""Delegated Graph reads only; never uses application-wide Graph credentials."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Empty, Queue
from threading import Event, Lock
from time import monotonic
from urllib.parse import quote, urlsplit
from uuid import UUID
import httpx

SCOPES = ["User.Read", "Team.ReadBasic.All", "Channel.ReadBasic.All", "ChannelMessage.Read.All"]
BASE = "https://graph.microsoft.com/v1.0"

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

    def _read(self, client, token, url, *, forbidden_missing=True, deadline=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or not parsed.path.startswith("/v1.0/") or parsed.fragment:
            raise GraphFailure()
        if deadline is not None and monotonic()>deadline:
            raise GraphFailure()
        try:
            with client.stream("GET", url, headers={"Authorization":"Bearer "+token}) as response:
                if response.status_code == 403:
                    if forbidden_missing: return None
                    raise GraphFailure()
                if response.status_code == 404:
                    return None
                if response.status_code == 401:
                    raise GraphFailure("reauth_required")
                if response.status_code != 200:
                    raise GraphFailure()
                chunks=[]; size=0
                for chunk in response.iter_bytes():
                    if deadline is not None and monotonic()>deadline:
                        raise GraphFailure()
                    size += len(chunk)
                    if size > 2_000_000:
                        raise GraphFailure()
                    chunks.append(chunk)
                import json
                data=json.loads(b"".join(chunks))
                if not isinstance(data,dict):
                    raise GraphFailure()
                return data
        except (httpx.HTTPError, ValueError) as error:
            raise GraphFailure() from None

    def _pages(self, client, token, path, deadline):
        url=BASE+path; expected_path=urlsplit(url).path; items=[]; visited=set()
        for _ in range(20):
            if monotonic()>deadline or url in visited or urlsplit(url).path != expected_path:
                raise GraphFailure()
            visited.add(url)
            data=self._read(client,token,url)
            if data is None:
                return []
            values=data.get("value")
            if not isinstance(values,list) or len(items)+len(values)>500:
                raise GraphFailure()
            items.extend(values)
            url=data.get("@odata.nextLink")
            if not url:
                return items
        raise GraphFailure()

    def _member(self, client, token, object_id):
        me=self._read(client,token,BASE+"/me?$select=id,userType")
        if not me or me.get("id", "").lower()!=object_id.lower() or me.get("userType")!="Member":
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
                            allowed=self._read(client,token,BASE+path,deadline=deadline) is not None
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
            self._member(client,token,object_id)
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
