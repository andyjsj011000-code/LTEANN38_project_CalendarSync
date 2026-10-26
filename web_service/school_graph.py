"""Delegated Graph reads only; never uses application-wide Graph credentials."""
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

    def _read(self, client, token, url, *, forbidden_missing=True):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or not parsed.path.startswith("/v1.0/") or parsed.fragment:
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

    def channels(self, token, object_id, tenant_id):
        deadline=monotonic()+80
        with self.client_factory() as client:
            self._member(client,token,object_id)
            teams=self._pages(client,token,"/me/joinedTeams?$select=id,displayName",deadline)
            result={}
            for team in teams:
                team_id=str(UUID(team["id"]))
                channels=self._pages(client,token,f"/teams/{team_id}/allChannels?$select=id,displayName,membershipType,tenantId",deadline)
                for channel in channels:
                    if monotonic()>deadline or len(result)>=300:
                        raise GraphFailure()
                    # Cross-tenant shared channels are deliberately excluded.
                    if channel.get("tenantId") not in (None,tenant_id) or (channel.get("membershipType")=="shared" and channel.get("tenantId")!=tenant_id):
                        continue
                    channel_id=channel.get("id","")
                    if not isinstance(channel_id,str) or not 1<=len(channel_id)<=200:
                        raise GraphFailure()
                    if channel_id in result:
                        continue
                    path=f"/teams/{team_id}/channels/{quote(channel_id,safe='')}/messages?$top=1"
                    # A channel's name appearing in a team list is NOT a permission proof.
                    if self._read(client,token,BASE+path) is None:
                        continue
                    result[channel_id]={"id":channel_id,"team_id":team_id,"name":str(channel.get("displayName") or "Teams")[:200]}
            return list(result.values())
