"""Verified-channel shared collection, ownership fences and retired-key hints."""
import json

import pytest

import web_service.direct_source as module
from web_service.direct_source import DirectSource
from web_service.school_graph import GraphFailure
from web_service.storage import digest
from test_direct_ingestion_progress import Cache, Extractor, Graph, channels, event, message


class SharedCache(Cache):
    shared_ingestion_enabled=True

    def __init__(self):
        super().__init__()
        self.leases,self.fresh_until,self.claims,self.finishes,self.cursors={},{},[],[],{}
        self.sequence=0

    def next_channels(self,user,candidates,count):
        assert count==1
        index=self.cursors.get(user,0)
        selected=sorted(candidates,key=lambda row:row['id'])[index%len(candidates)]
        self.cursors[user]=(index+1)%len(candidates)
        self.visits.append(selected['id'])
        return [selected]

    def channel_ingestion_fresh(self,channel,freshness_seconds=300):
        assert isinstance(channel,dict) and channel.get('team_id')
        return self.fresh_until.get(channel['id'],0)>self.clock() and freshness_seconds>0

    def claim_channel_ingestion(self,channel,freshness_seconds=300):
        assert isinstance(channel,dict) and channel.get('team_id')
        identity=channel['id']
        self.claims.append((identity,freshness_seconds))
        if identity in self.leases or self.channel_ingestion_fresh(channel,freshness_seconds):
            return None
        self.sequence+=1
        lease='lease-'+str(self.sequence)
        self.leases[identity]=lease
        return lease

    def finish_channel_ingestion(self,channel,lease,success):
        assert isinstance(channel,dict) and channel.get('team_id')
        identity=channel['id']
        self.finishes.append((identity,lease,success))
        if self.leases.get(identity)!=lease:
            return False
        del self.leases[identity]
        if success:
            self.fresh_until[identity]=self.clock()+300
        return True

    def cache_analysis(self,key,channel,identity,version,events,*,ingestion_lease=None):
        if self.leases.get(channel)!=ingestion_lease:
            return False
        super().cache_analysis(key,channel,identity,version,events)
        return True

    def touch_analysis(self,key,*,ingestion_lease=None):
        return ingestion_lease in self.leases.values()


@pytest.fixture(autouse=True)
def no_elapsed_time(monkeypatch):
    monkeypatch.setattr(module,'monotonic',lambda:0)


def test_second_account_reuses_fresh_shared_results_and_empty_channel_marker():
    candidates,cache,extractor=channels(2),SharedCache(),Extractor()
    graph=Graph({candidates[0]['id']:[message('one')]})
    source=DirectSource(graph,extractor,images=False)
    first=source.collect('token-a',candidates,'tenant',cache,'alice')
    assert len(first)==1 and first.report['partial'] is False
    assert graph.reads==[row['id'] for row in candidates]
    assert len(cache.fresh_until)==2
    second=source.collect('token-b',candidates,'tenant',cache,'bob')
    assert second==first and len(extractor.calls)==1
    assert graph.reads==[row['id'] for row in candidates]
    assert second.report['channels_total']==2 and second.report['channels_scanned']==0
    assert second.report['partial'] is False


def test_only_new_or_stale_verified_channels_are_scanned_for_next_account():
    candidates,cache,extractor=channels(3),SharedCache(),Extractor()
    graph=Graph({row['id']:[message(row['id'])] for row in candidates})
    source=DirectSource(graph,extractor,images=False)
    source.collect('token-a',candidates[:2],'tenant',cache,'alice')
    cache.fresh_until[candidates[1]['id']]=cache.clock()-1
    second=source.collect('token-b',candidates,'tenant',cache,'bob')
    assert graph.reads==[candidates[0]['id'],candidates[1]['id'],candidates[1]['id'],candidates[2]['id']]
    assert second.report['channels_scanned']==2 and second.report['partial'] is False
    assert len(extractor.calls)==3  # Stale unchanged data reuses its message cache.
    assert second.report['messages_cached']==1


def test_busy_shared_channel_does_not_duplicate_extraction_or_claim_completion():
    candidates,cache=channels(1),SharedCache()
    graph=Graph({candidates[0]['id']:[message('one')]})
    nested=[]
    class ConcurrentExtractor(Extractor):
        def extract(self,text,images,previous):
            nested.append(source.collect('token-b',candidates,'tenant',cache,'bob'))
            return super().extract(text,images,previous)
    extractor=ConcurrentExtractor()
    source=DirectSource(graph,extractor,images=False)
    first=source.collect('token-a',candidates,'tenant',cache,'alice')
    assert len(extractor.calls)==1 and graph.reads==[candidates[0]['id']]
    assert nested[0].report['channels_scanned']==0 and nested[0].report['partial'] is True
    assert first.report['partial'] is False
    assert source.collect('token-b',candidates,'tenant',cache,'bob')==first
    assert len(extractor.calls)==1


def test_failed_analysis_releases_lease_without_marking_channel_fresh():
    candidates,cache=channels(1),SharedCache()
    source=DirectSource(Graph({candidates[0]['id']:[message('bad','bad-message')]}),
                        Extractor(ValueError('bad response')),images=False)
    result=source.collect('token',candidates,'tenant',cache,'alice')
    assert result.report['messages_failed']==1 and result.report['partial'] is True
    assert cache.finishes[-1][2] is False and cache.leases=={} and cache.fresh_until=={}
    assert cache.writes==[]


def test_deadline_partial_never_marks_channel_fresh(monkeypatch):
    now=[0]
    monkeypatch.setattr(module,'monotonic',lambda:now[0])
    candidates,cache=channels(2),SharedCache()
    class SlowExtractor(Extractor):
        def extract(self,text,images,previous):
            now[0]+=81
            return super().extract(text,images,previous)
    graph=Graph({candidates[0]['id']:[message('first'),message('later')]})
    result=DirectSource(graph,SlowExtractor(),images=False).collect(
        'token',candidates,'tenant',cache,'alice')
    assert result.report['partial'] is True and result.report['channels_scanned']==0
    assert cache.finishes[-1][2] is False and cache.fresh_until=={} and cache.leases=={}
    assert cache.visits==[candidates[0]['id']]


def test_lost_write_fence_preserves_prior_cache_and_other_owner_lease():
    candidates,cache=channels(1),SharedCache()
    prior={'version':'prior','events':[{'id':'prior-event','channel_id':candidates[0]['id'],
                                      'title':'Prior deadline','cancelled':False}]}
    cache.values[candidates[0]['id']]={'one':prior}
    class ReplacedLeaseExtractor(Extractor):
        def extract(self,text,images,previous):
            cache.leases[candidates[0]['id']]='new-owner'
            return super().extract(text,images,previous)
    result=DirectSource(Graph({candidates[0]['id']:[message('one')]}),
                        ReplacedLeaseExtractor(),images=False).collect(
                            'token',candidates,'tenant',cache,'alice')
    assert result==prior['events'] and result.report['partial'] is True
    assert result.report['channels_scanned']==0
    assert cache.values[candidates[0]['id']]['one'] is prior and cache.writes==[]
    assert cache.leases[candidates[0]['id']]=='new-owner' and cache.fresh_until=={}


def test_lost_final_fence_does_not_report_fresh_complete_channel():
    candidates=channels(1)
    class LostFinishCache(SharedCache):
        def finish_channel_ingestion(self,channel,lease,success):
            self.leases[channel['id']]='new-owner'
            return False
    cache=LostFinishCache()
    result=DirectSource(Graph(),Extractor(),images=False).collect(
        'token',candidates,'tenant',cache,'alice')
    assert result.report['partial'] is True and result.report['channels_scanned']==0
    assert cache.fresh_until=={}


@pytest.mark.parametrize('targeted',[False,True])
def test_selected_work_forces_freshness_zero_and_never_silently_acknowledges_busy(targeted):
    candidates,cache=channels(1),SharedCache()
    channel=candidates[0]['id']
    cache.fresh_until[channel]=cache.clock()+300
    class TargetGraph(Graph):
        def message(self,token,channel,identity,**kwargs):
            return message(identity)
    graph=TargetGraph({channel:[message('one')]})
    source=DirectSource(graph,Extractor(),images=False)
    kwargs={'selected_channels':candidates}
    if targeted:
        kwargs['selected_messages']=['one']
    result=source.collect('token',candidates,'tenant',cache,'alice',**kwargs)
    assert cache.claims[-1]==(channel,0) and result.report['channels_scanned']==1
    assert cache.finishes[-1][2] is (not targeted)
    cache.leases[channel]='another-owner'
    with pytest.raises(GraphFailure) as raised:
        source.collect('token',candidates,'tenant',cache,'alice',**kwargs)
    assert raised.value.state=='source_unavailable'


@pytest.mark.parametrize('state',['reauth_required','account_not_allowed','verification_failed'])
def test_access_error_releases_owned_lease_and_still_propagates(state):
    candidates,cache=channels(1),SharedCache()
    source=DirectSource(Graph({candidates[0]['id']:[message('bad','bad-message')]}),
                        Extractor(GraphFailure(state)),images=False)
    with pytest.raises(GraphFailure) as raised:
        source.collect('token',candidates,'tenant',cache,'alice')
    assert raised.value.state==state and cache.leases=={} and cache.fresh_until=={}
    assert cache.finishes[-1][2] is False


def test_retired_keys_are_ai_context_only_and_never_emit_cancelled_events():
    candidates,cache,extractor=channels(1),SharedCache(),Extractor()
    channel,identity=candidates[0]['id'],'one'
    retired={'id':'retired-event','key':'deadline','source_version':'old','end_date':'2026-10-01'}
    cache.values[channel]={identity:{'version':'old','events':[],'retired':[retired]}}
    class EmptyExtractor(Extractor):
        def extract(self,text,images,previous):
            self.calls.append((text,previous))
            return []
    extractor=EmptyExtractor()
    result=DirectSource(Graph({channel:[message(identity)]}),extractor,images=False).collect(
        'token',candidates,'tenant',cache,'alice')
    assert extractor.calls[0][1]==[retired]
    assert result==[] and cache.values[channel][identity]['events']==[]


def test_unchanged_retired_message_skips_ai_without_resurrecting_event():
    candidates,cache,extractor=channels(1),SharedCache(),Extractor()
    channel,root=candidates[0]['id'],message('one')
    version=digest(json.dumps({key:root.get(key) for key in (
        'body','subject','replies','deletedDateTime','lastModifiedDateTime')},
        sort_keys=True,ensure_ascii=False))
    cache.values[channel]={'one':{'version':version,'events':[],
                                  'retired':[{'id':'retired-event','key':'deadline'}]}}
    result=DirectSource(Graph({channel:[root]}),extractor,images=False).collect(
        'token',candidates,'tenant',cache,'alice')
    assert extractor.calls==[] and result==[]
    assert result.report['messages_cached']==1 and result.report['partial'] is False
