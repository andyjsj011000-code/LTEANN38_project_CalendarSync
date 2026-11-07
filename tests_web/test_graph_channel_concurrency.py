"""Concurrent permission proofs keep the same account and channel boundaries."""
from contextlib import contextmanager
from threading import Barrier, Lock, get_ident

import httpx
import pytest

from web_service import school_graph
from web_service.school_graph import DelegatedGraph, GraphFailure


TENANT='11111111-1111-4111-8111-111111111111'
USER='33333333-3333-4333-8333-333333333333'
TEAM_A='22222222-2222-4222-8222-222222222222'
TEAM_B='44444444-4444-4444-8444-444444444444'


def graph_for(channels, probe, *, teams=None, profile=None, pace=False):
    reads=[]
    def handler(request):
        path=request.url.path
        reads.append(path)
        if path=='/v1.0/me':
            return httpx.Response(200,json=profile or {'id':USER,'userType':'Member'})
        if path=='/v1.0/me/joinedTeams':
            return httpx.Response(200,json={'value':teams or [{'id':TEAM_A}]})
        if path.endswith('/allChannels'):
            return httpx.Response(200,json={'value':channels[path.split('/')[3]]})
        return probe(request)
    graph=DelegatedGraph(lambda:httpx.Client(transport=httpx.MockTransport(handler)))
    if not pace:
        graph._wait_for_probe=lambda channel_id,deadline,stopped:not stopped.is_set()
    return graph,reads


def test_permission_probes_overlap_with_six_worker_clients_and_keep_listing_order():
    lock=Lock()
    barrier=Barrier(6)
    active=maximum=started=0
    owners={}
    closed=[]
    listed=[]
    channels={TEAM_A:[{'id':f'a-{i}'} for i in range(12)],
              TEAM_B:[{'id':f'b-{i}'} for i in range(12)]}

    @contextmanager
    def factory():
        with lock:
            client_id=len(owners)
            owners[client_id]=set()
        def handler(request):
            nonlocal active,maximum,started
            path=request.url.path
            with lock:
                owners[client_id].add(get_ident())
            if path=='/v1.0/me':
                return httpx.Response(200,json={'id':USER,'userType':'Member'})
            if path=='/v1.0/me/joinedTeams':
                return httpx.Response(200,json={'value':[{'id':TEAM_A},{'id':TEAM_B}]})
            if path.endswith('/allChannels'):
                team=path.split('/')[3]
                listed.append(team)
                return httpx.Response(200,json={'value':channels[team]})
            assert listed==[TEAM_A,TEAM_B]
            with lock:
                active+=1
                maximum=max(maximum,active)
                started+=1
                first_batch=started<=6
            try:
                if first_batch:
                    barrier.wait(timeout=3)
                return httpx.Response(200,json={'value':[]})
            finally:
                with lock:
                    active-=1
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            try:
                yield client
            finally:
                closed.append(client_id)

    graph=DelegatedGraph(factory)
    graph._wait_for_probe=lambda channel_id,deadline,stopped:not stopped.is_set()
    result=graph.channels('token',USER,TENANT)
    assert [entry['id'] for entry in result]==[f'a-{i}' for i in range(12)]+[f'b-{i}' for i in range(12)]
    assert maximum==6 and started==24
    assert len(owners)==7 and sorted(closed)==list(range(7))
    assert all(len(threads)==1 for threads in owners.values())
    discovery_thread=next(iter(owners[0]))
    assert all(discovery_thread not in threads for index,threads in owners.items() if index)


@pytest.mark.parametrize('status,state',[(401,'reauth_required'),(429,'verification_failed'),(500,'verification_failed')])
def test_fatal_probe_stops_queued_channels_and_returns_no_partial_catalog(status,state):
    probes=[]
    def probe(request):
        probes.append(request.url.path)
        return httpx.Response(status)
    graph,_=graph_for({TEAM_A:[{'id':f'channel-{i}'} for i in range(62)]},probe)
    with pytest.raises(GraphFailure) as error:
        graph.channels('token',USER,TENANT)
    assert error.value.state==state
    assert 1<=len(probes)<=6


@pytest.mark.parametrize('failure',['invalid_json','timeout'])
def test_unverifiable_probe_fails_closed(failure):
    def probe(request):
        if failure=='timeout':
            raise httpx.ReadTimeout('timed out')
        return httpx.Response(200,json=[])
    graph,_=graph_for({TEAM_A:[{'id':'channel'}]},probe)
    with pytest.raises(GraphFailure) as error:
        graph.channels('token',USER,TENANT)
    assert error.value.state=='verification_failed'


def test_denied_missing_foreign_and_unscoped_shared_channels_are_excluded():
    def probe(request):
        identity=request.url.path.split('/')[5]
        return httpx.Response({'denied':403,'missing':404}.get(identity,200),json={'value':[]})
    values=[{'id':'allowed'}, {'id':'denied'}, {'id':'missing'},
            {'id':'foreign','tenantId':TEAM_B},
            {'id':'shared-unknown','membershipType':'shared'},
            {'id':'shared-local','membershipType':'shared','tenantId':TENANT}]
    graph,reads=graph_for({TEAM_A:values},probe)
    assert [entry['id'] for entry in graph.channels('token',USER,TENANT)]==['allowed','shared-local']
    assert not any('/channels/foreign/' in path or '/channels/shared-unknown/' in path for path in reads)


def test_later_team_route_can_prove_access_to_a_duplicate_channel():
    def probe(request):
        return httpx.Response(403 if f'/teams/{TEAM_A}/' in request.url.path else 200,json={'value':[]})
    graph,reads=graph_for({TEAM_A:[{'id':'shared'}],TEAM_B:[{'id':'shared'}]},probe,
                         teams=[{'id':TEAM_A},{'id':TEAM_B}])
    assert graph.channels('token',USER,TENANT)==[{'id':'shared','team_id':TEAM_B,'name':'Teams'}]
    assert len([path for path in reads if path.endswith('/messages')])==2


@pytest.mark.parametrize('profile',[{'id':USER,'userType':'Guest'},
                                    {'id':TEAM_B,'userType':'Member'}, {'id':USER}])
def test_membership_failure_never_enumerates_or_probes(profile):
    graph,reads=graph_for({TEAM_A:[{'id':'channel'}]},lambda request:httpx.Response(200),profile=profile)
    with pytest.raises(GraphFailure) as error:
        graph.channels('token',USER,TENANT)
    assert error.value.state=='account_not_allowed'
    assert reads==['/v1.0/me']


def test_foreign_pagination_url_aborts_before_any_permission_probe():
    reads=[]
    def handler(request):
        reads.append(str(request.url))
        if request.url.path=='/v1.0/me':
            return httpx.Response(200,json={'id':USER,'userType':'Member'})
        if request.url.path=='/v1.0/me/joinedTeams':
            return httpx.Response(200,json={'value':[{'id':TEAM_A}]})
        return httpx.Response(200,json={'value':[{'id':'channel'}],
            '@odata.nextLink':f'https://evil.test/v1.0/teams/{TEAM_A}/allChannels'})
    graph=DelegatedGraph(lambda:httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(GraphFailure):
        graph.channels('token',USER,TENANT)
    assert len(reads)==3 and not any('/messages' in url or 'evil.test' in url for url in reads)


def test_trusted_pagination_verifies_every_channel_at_the_300_route_boundary():
    probes=[]
    def handler(request):
        path=request.url.path
        next_page=request.url.params.get('$skiptoken')=='next'
        if path=='/v1.0/me':
            return httpx.Response(200,json={'id':USER,'userType':'Member'})
        if path=='/v1.0/me/joinedTeams':
            value={'value':[{'id':TEAM_B if next_page else TEAM_A}]}
            if not next_page:
                value['@odata.nextLink']=school_graph.BASE+'/me/joinedTeams?$skiptoken=next'
            return httpx.Response(200,json=value)
        if path.endswith('/allChannels'):
            if f'/teams/{TEAM_A}/' in path:
                start,end=(75,150) if next_page else (0,75)
                value={'value':[{'id':f'a-{i}'} for i in range(start,end)]}
                if not next_page:
                    value['@odata.nextLink']=school_graph.BASE+path.removeprefix('/v1.0')+'?$skiptoken=next'
            else:
                value={'value':[{'id':f'b-{i}'} for i in range(150)]}
            return httpx.Response(200,json=value)
        probes.append(path.split('/')[5])
        return httpx.Response(200,json={'value':[]})
    graph=DelegatedGraph(lambda:httpx.Client(transport=httpx.MockTransport(handler)))
    graph._wait_for_probe=lambda channel_id,deadline,stopped:not stopped.is_set()
    expected=[f'a-{i}' for i in range(150)]+[f'b-{i}' for i in range(150)]
    assert [channel['id'] for channel in graph.channels('token',USER,TENANT)]==expected
    assert len(probes)==300 and set(probes)==set(expected)


def test_more_than_300_candidate_routes_aborts_before_permission_probes():
    graph,reads=graph_for({TEAM_A:[{'id':f'channel-{i}'} for i in range(301)]},
                         lambda request:httpx.Response(200,json={'value':[]}))
    with pytest.raises(GraphFailure):
        graph.channels('token',USER,TENANT)
    assert not any(path.endswith('/messages') for path in reads)


def test_probe_finishing_after_deadline_cannot_return_permission_catalog(monkeypatch):
    clock=[0]
    monkeypatch.setattr(school_graph,'monotonic',lambda:clock[0])
    def probe(request):
        clock[0]=81
        return httpx.Response(200,json={'value':[]})
    graph,_=graph_for({TEAM_A:[{'id':'channel'}]},probe)
    with pytest.raises(GraphFailure):
        graph.channels('token',USER,TENANT)


def test_probe_pacing_bounds_total_rate_and_duplicate_channel_rate(monkeypatch):
    clock=[0.0]
    waits=[]
    monkeypatch.setattr(school_graph,'monotonic',lambda:clock[0])
    class Stop:
        def is_set(self): return False
        def wait(self,seconds):
            waits.append(seconds)
            clock[0]+=seconds
            return False
    graph=DelegatedGraph()
    stop=Stop()
    starts=[]
    for identity in ['a','b','a']:
        assert graph._wait_for_probe(identity,80,stop)
        starts.append(clock[0])
    assert starts==pytest.approx([0,0.08,1])
    assert waits==pytest.approx([0.08,0.92])
    with pytest.raises(GraphFailure):
        graph._wait_for_probe('a',1.5,stop)


def test_fatal_cancellation_interrupts_waiting_probe():
    class Stop:
        def is_set(self): return False
        def wait(self,seconds): return True
    graph=DelegatedGraph()
    graph._next_probe=school_graph.monotonic()+10
    assert not graph._wait_for_probe('channel',school_graph.monotonic()+80,Stop())
