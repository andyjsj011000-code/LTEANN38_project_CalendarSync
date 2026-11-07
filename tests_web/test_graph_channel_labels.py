"""Authorized team names distinguish identically named channels in the UI."""
import httpx
import pytest

from web_service.school_graph import DelegatedGraph


TENANT='11111111-1111-4111-8111-111111111111'
USER='33333333-3333-4333-8333-333333333333'
TEAM_A='22222222-2222-4222-8222-222222222222'
TEAM_B='44444444-4444-4444-8444-444444444444'


def graph_for(teams, channel_sets, denied=()):
    reads=[]
    def handler(request):
        path=request.url.path
        reads.append(path)
        if path=='/v1.0/me':
            return httpx.Response(200,json={'id':USER,'userType':'Member'})
        if path=='/v1.0/me/joinedTeams':
            return httpx.Response(200,json={'value':teams})
        if path.endswith('/allChannels'):
            return httpx.Response(200,json={'value':channel_sets[path.split('/')[3]]})
        if any(f'/channels/{identity}/' in path for identity in denied):
            return httpx.Response(403)
        return httpx.Response(200,json={'value':[]})
    return DelegatedGraph(lambda:httpx.Client(transport=httpx.MockTransport(handler))),reads


def test_team_names_distinguish_general_without_changing_access_or_identifiers():
    teams=[{'id':TEAM_A,'displayName':'Physics'},{'id':TEAM_B,'displayName':'Mathematics'}]
    channel_sets={TEAM_A:[{'id':'physics-general','displayName':'General'},
                          {'id':'denied','displayName':'Private'},
                          {'id':'foreign','displayName':'General','tenantId':TEAM_B}],
                  TEAM_B:[{'id':'math-general','displayName':'General'}]}
    graph,reads=graph_for(teams,channel_sets,denied={'denied'})
    result=graph.channels('token',USER,TENANT)
    assert result==[{'id':'physics-general','team_id':TEAM_A,'name':'Physics · General'},
                    {'id':'math-general','team_id':TEAM_B,'name':'Mathematics · General'}]
    assert any('/channels/physics-general/messages' in path for path in reads)
    assert any('/channels/math-general/messages' in path for path in reads)
    assert not any('/channels/foreign/' in path for path in reads)


@pytest.mark.parametrize('team_name,channel_name,expected',[
    (None,'General','General'),
    ('  Physics  ',' General ','Physics · General'),
    ('Physics',None,'Physics · Teams'),
    (' ','','Teams'),
    (17,{},'Teams'),
])
def test_missing_or_blank_labels_have_a_readable_fallback(team_name,channel_name,expected):
    graph,_=graph_for([{'id':TEAM_A,'displayName':team_name}],
                      {TEAM_A:[{'id':'channel','displayName':channel_name}]})
    result=graph.channels('token',USER,TENANT)
    assert result==[{'id':'channel','team_id':TEAM_A,'name':expected}]


def test_long_team_label_keeps_channel_label_and_stays_within_storage_bound():
    graph,_=graph_for([{'id':TEAM_A,'displayName':'Team '+('T'*250)}],
                      {TEAM_A:[{'id':'channel','displayName':'Channel '+('C'*250)}]})
    label=graph.channels('token',USER,TENANT)[0]['name']
    assert len(label)<=200
    assert label.startswith('Team ') and ' · Channel ' in label
