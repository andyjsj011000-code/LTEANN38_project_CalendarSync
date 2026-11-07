"""Do not acknowledge or cancel schedules after dropping source content."""
import json
import httpx
import pytest

from web_service.direct_source import DirectSource, TeamsGraph
from web_service.school_graph import BASE, GraphFailure
from web_service.storage import digest
from test_direct_ingestion_progress import Graph, channels, event, message
from test_direct_shared_ingestion import SharedCache


class InputGraph(Graph):
    def __init__(self,candidates,root):
        super().__init__({candidates[0]['id']:[root,message('good')]})
        self.root=root
        self.image_reads=[]

    def hosted_image(self,token,channel,identity,url):
        self.image_reads.append(url)
        return 'data:image/png;base64,eA=='

    def message(self,token,channel,identity,*,complete_replies=False,deadline=None):
        assert complete_replies and deadline is not None
        assert identity==self.root['id']
        return self.root


class Recorder:
    configured=True
    def __init__(self,*,require_images=False,require_tail=False):
        self.calls=[]
        self.require_images,self.require_tail=require_images,require_tail
    def extract(self,text,images,previous):
        self.calls.append((text,list(images),previous))
        if self.require_images and not images: return []
        if self.require_tail and 'DEADLINE_TAIL' not in text: return []
        return [event('Kept deadline')]


def seed(cache,channel):
    identity='direct:'+digest(channel['id']+'|post|deadline')
    prior={'version':'old','events':[{**event('Prior deadline'),'id':identity,
        'channel_id':channel['id'],'source_version':'old'}]}
    cache.values[channel['id']]={'post':prior}
    return prior


def image_url(identity='image'):
    return BASE+'/teams/team/channels/channel-00/messages/post/hostedContents/'+identity+'/$value'


def legacy_version(root):
    return digest(json.dumps({key:root.get(key) for key in
        ('body','subject','replies','deletedDateTime','lastModifiedDateTime')},sort_keys=True,ensure_ascii=False))


def test_uppercase_root_image_is_analyzed_without_cancelling_prior_deadline():
    candidates,cache,extractor=channels(1),SharedCache(),Recorder(require_images=True)
    prior=seed(cache,candidates[0])
    root=message('post','<IMG SRC="'+image_url()+'">')
    graph=InputGraph(candidates,root)
    graph.values[candidates[0]['id']]=[root]
    result=DirectSource(graph,extractor).collect('token',candidates,'tenant',cache,'alice')
    assert graph.image_reads==[image_url()] and len(extractor.calls[0][1])==1
    assert result[0]['id']==prior['events'][0]['id'] and result[0]['cancelled'] is False
    assert result.report['partial'] is False and candidates[0]['id'] in cache.fresh_until


@pytest.mark.parametrize('exact',[False,True])
def test_existing_three_image_capacity_remains_supported(exact):
    candidates,cache,extractor=channels(1),SharedCache(),Recorder(require_images=True)
    root=message('post',''.join(f'<img src="{image_url(str(index))}">' for index in range(3)))
    graph=InputGraph(candidates,root)
    graph.values[candidates[0]['id']]=[root]
    arguments={'selected_channels':candidates,'selected_messages':['post']} if exact else {}
    result=DirectSource(graph,extractor).collect('token',candidates,'tenant',cache,'alice',**arguments)
    assert graph.image_reads==[image_url(str(index)) for index in range(3)]
    assert len(extractor.calls)==1 and len(extractor.calls[0][1])==3
    assert result.report['partial'] is False


@pytest.mark.parametrize('mode',['broad','selected','exact'])
def test_unsupported_image_returning_none_cannot_cancel_prior_analysis(monkeypatch,mode):
    candidates,cache,extractor=channels(1),SharedCache(),Recorder(require_images=True)
    prior=seed(cache,candidates[0])
    root=message('post','<img src="'+image_url()+'">')
    graph=InputGraph(candidates,root)
    def unavailable(token,channel,identity,url):
        graph.image_reads.append(url)
        return None
    monkeypatch.setattr(graph,'hosted_image',unavailable)
    arguments={} if mode=='broad' else {'selected_channels':candidates}
    if mode=='exact': arguments['selected_messages']=['post']
    if mode=='broad':
        result=DirectSource(graph,extractor).collect('token',candidates,'tenant',cache,'alice',**arguments)
        assert result[0]==prior['events'][0] and result[0]['cancelled'] is False
        assert result.report['partial'] is True and result.report['messages_failed']==1
        assert len(extractor.calls)==1 and cache.writes==[(candidates[0]['id'],'good')]
    else:
        with pytest.raises(GraphFailure) as error:
            DirectSource(graph,extractor).collect('token',candidates,'tenant',cache,'alice',**arguments)
        assert error.value.state=='source_unavailable'
        assert extractor.calls==[] and cache.writes==[]
    assert graph.image_reads==[image_url()]
    assert cache.values[candidates[0]['id']]['post'] is prior
    assert cache.fresh_until=={} and cache.leases=={}


@pytest.mark.parametrize('activity',[
    {'lastModifiedDateTime':'2026-11-06T00:00:00Z'},
    {'lastEditedDateTime':'2026-11-06T00:00:00Z'},
    {'replies':[{'createdDateTime':'2026-11-06T00:00:00Z','body':{'content':'Recent deadline'}}]},
    {'replies':[{'createdDateTime':'2026-01-01T00:00:00Z','lastModifiedDateTime':'2026-11-06T00:00:00Z',
                 'body':{'content':'Edited deadline'}}]},
    {'replies':[{'createdDateTime':'2026-10-08T00:00:00Z','body':{'content':'Boundary deadline'}}]},
])
def test_recent_thread_activity_keeps_old_uncached_root_inside_existing_window(activity):
    candidates,cache,extractor=channels(1),SharedCache(),Recorder()
    root={**message('post'),'createdDateTime':'2026-01-01T00:00:00Z',**activity}
    graph=InputGraph(candidates,root)
    graph.values[candidates[0]['id']]=[root]
    result=DirectSource(graph,extractor,images=False).collect('token',candidates,'tenant',cache,'alice')
    assert len(extractor.calls)==1 and result.report['messages_analyzed']==1
    assert result.report['partial'] is False and candidates[0]['id'] in cache.fresh_until


def test_old_inactive_uncached_root_is_still_skipped():
    candidates,cache,extractor=channels(1),SharedCache(),Recorder()
    root={**message('post'),'createdDateTime':'2026-01-01T00:00:00Z',
        'lastModifiedDateTime':'2026-01-01T00:00:00Z',
        'replies':[{'createdDateTime':'2026-10-07T23:59:59Z','body':{'content':'Old reply'}}]}
    graph=InputGraph(candidates,root)
    graph.values[candidates[0]['id']]=[root]
    result=DirectSource(graph,extractor,images=False).collect('token',candidates,'tenant',cache,'alice')
    assert extractor.calls==[] and cache.writes==[] and result==[]
    assert result.report['partial'] is False


@pytest.mark.parametrize('stamp',['v1','1','2','2026-11-06T00:00:00'])
def test_valid_fresh_creation_suffices_when_optional_activity_metadata_is_invalid(stamp):
    candidates,cache,extractor=channels(1),SharedCache(),Recorder()
    root={**message('post'),'createdDateTime':'2026-11-06T00:00:00Z','lastModifiedDateTime':stamp}
    graph=InputGraph(candidates,root)
    graph.values[candidates[0]['id']]=[root]
    result=DirectSource(graph,extractor,images=False).collect('token',candidates,'tenant',cache,'alice')
    assert len(extractor.calls)==1 and result.report['messages_analyzed']==1
    assert result.report['messages_failed']==0 and result.report['partial'] is False
    assert candidates[0]['id'] in cache.fresh_until


def test_fresh_reply_suffices_after_invalid_optional_root_metadata():
    candidates,cache,extractor=channels(1),SharedCache(),Recorder()
    root={**message('post'),'createdDateTime':'2026-01-01T00:00:00Z','lastModifiedDateTime':'v1',
          'replies':[{'createdDateTime':'2026-11-06T00:00:00Z','body':{'content':'Recent reply'}}]}
    graph=InputGraph(candidates,root)
    graph.values[candidates[0]['id']]=[root]
    result=DirectSource(graph,extractor,images=False).collect('token',candidates,'tenant',cache,'alice')
    assert len(extractor.calls)==1 and result.report['partial'] is False


@pytest.mark.parametrize('stamp',['v1','2026-11-06T00:00:00'])
def test_old_valid_activity_with_unknown_optional_activity_remains_partial(stamp):
    candidates,cache,extractor=channels(1),SharedCache(),Recorder()
    root={**message('post'),'createdDateTime':'2026-01-01T00:00:00Z','lastModifiedDateTime':stamp}
    graph=InputGraph(candidates,root)
    graph.values[candidates[0]['id']]=[root]
    result=DirectSource(graph,extractor,images=False).collect('token',candidates,'tenant',cache,'alice')
    assert extractor.calls==[] and cache.writes==[]
    assert result.report['messages_failed']==1 and result.report['partial'] is True
    assert cache.fresh_until=={} and cache.leases=={}


def incomplete_root(reason):
    root=message('post')
    if reason=='reply_image':
        root['replies']=[{'body':{'content':'<IMG SRC="reply-image">'}}]
    elif reason=='four_images':
        root['body']['content']=''.join(f'<img src="image-{index}">' for index in range(4))
    elif reason=='reply_31':
        root['replies']=[{'body':{'content':'discussion' if i<30 else 'DEADLINE_TAIL'}} for i in range(31)]
    elif reason=='body_too_long':
        root['body']['content']='x'*16000+'DEADLINE_TAIL'
    else:
        root['body']['content']='x'*15990
        root['subject']='Metadata also consumes the AI text budget'
    return root


@pytest.mark.parametrize('reason',['reply_image','four_images','reply_31','body_too_long','total_too_long'])
def test_incomplete_broad_input_preserves_prior_and_continues_without_marking_fresh(reason):
    candidates,cache,extractor=channels(1),SharedCache(),Recorder()
    prior=seed(cache,candidates[0])
    graph=InputGraph(candidates,incomplete_root(reason))
    result=DirectSource(graph,extractor).collect('token',candidates,'tenant',cache,'alice')
    assert cache.values[candidates[0]['id']]['post'] is prior
    assert cache.writes==[(candidates[0]['id'],'good')]
    assert len(extractor.calls)==1 and graph.image_reads==[]
    assert result[0]==prior['events'][0] and result[0]['cancelled'] is False
    assert result.report['partial'] is True and result.report['messages_failed']==1
    assert candidates[0]['id'] not in cache.fresh_until and cache.leases=={}
    assert cache.finishes==[(candidates[0]['id'],'lease-1',False)]


@pytest.mark.parametrize('exact',[False,True])
@pytest.mark.parametrize('reason',['reply_image','four_images','body_too_long','total_too_long'])
def test_incomplete_selected_and_exact_input_remains_strict(reason,exact):
    candidates,cache,extractor=channels(1),SharedCache(),Recorder()
    prior=seed(cache,candidates[0])
    graph=InputGraph(candidates,incomplete_root(reason))
    arguments={'selected_channels':candidates}
    if exact: arguments['selected_messages']=['post']
    with pytest.raises(GraphFailure) as error:
        DirectSource(graph,extractor).collect('token',candidates,'tenant',cache,'alice',**arguments)
    assert error.value.state=='source_unavailable'
    assert cache.values[candidates[0]['id']]['post'] is prior and cache.writes==[]
    assert extractor.calls==[] and graph.image_reads==[]
    assert cache.fresh_until=={} and cache.leases=={}


def test_text_after_10000_characters_is_kept_within_existing_16000_ai_budget():
    candidates,cache,extractor=channels(1),SharedCache(),Recorder(require_tail=True)
    prior=seed(cache,candidates[0])
    root=message('post','x'*10000+'DEADLINE_TAIL')
    graph=InputGraph(candidates,root)
    graph.values[candidates[0]['id']]=[root]
    result=DirectSource(graph,extractor,images=False).collect('token',candidates,'tenant',cache,'alice')
    assert len(extractor.calls)==1 and 'DEADLINE_TAIL' in extractor.calls[0][0]
    assert len(extractor.calls[0][0])<=16000
    assert result[0]['id']==prior['events'][0]['id'] and result[0]['cancelled'] is False
    assert result.report['partial'] is False


def test_exact_thread_retains_existing_complete_reply_capacity():
    candidates,cache,extractor=channels(1),SharedCache(),Recorder(require_tail=True)
    prior=seed(cache,candidates[0])
    graph=InputGraph(candidates,incomplete_root('reply_31'))
    result=DirectSource(graph,extractor,images=False).collect('token',candidates,'tenant',cache,'alice',
        selected_channels=candidates,selected_messages=['post'])
    assert len(extractor.calls)==1 and 'DEADLINE_TAIL' in extractor.calls[0][0]
    assert result[0]['id']==prior['events'][0]['id'] and result[0]['cancelled'] is False
    assert cache.finishes==[(candidates[0]['id'],'lease-1',False)]


@pytest.mark.parametrize('reason',['reply_image','four_images','reply_31','body_too_long','total_too_long',
                                   'invalid_image_url','reply_next_link'])
def test_same_version_cache_cannot_bypass_completeness_guards(monkeypatch,reason):
    candidates,cache,extractor=channels(1),SharedCache(),Recorder()
    root=incomplete_root(reason)
    if reason=='invalid_image_url': root=message('post','<img src="https://unsupported.example/image.png">')
    if reason=='reply_next_link':
        root=message('post')
        root['replies@odata.nextLink']='https://unsupported.example/continuation'
    prior=seed(cache,candidates[0])
    prior['version']=legacy_version(root)
    graph=InputGraph(candidates,root)
    graph.values[candidates[0]['id']]=[root]
    monkeypatch.setattr(cache,'touch_analysis',lambda *args,**kwargs:pytest.fail('Incomplete cache must not be touched'))
    result=DirectSource(graph,extractor).collect('token',candidates,'tenant',cache,'alice')
    assert cache.values[candidates[0]['id']]['post'] is prior and cache.writes==[]
    assert extractor.calls==[] and graph.image_reads==[]
    assert result.report['messages_cached']==0 and result.report['messages_failed']==1
    assert result.report['partial'] is True and cache.fresh_until=={}


@pytest.mark.parametrize('kind',['uppercase_image','long_body','long_reply'])
def test_only_affected_valid_legacy_input_reanalyzes_once_across_collection_modes(kind):
    candidates,cache=channels(1),SharedCache()
    extractor=Recorder(require_images=kind=='uppercase_image',require_tail=kind!='uppercase_image')
    root=message('post')
    if kind=='uppercase_image': root['body']['content']='<IMG SRC="'+image_url()+'">'
    elif kind=='long_body': root['body']['content']='x'*10000+'DEADLINE_TAIL'
    else: root['replies']=[{'body':{'content':'x'*10000+'DEADLINE_TAIL'}}]
    prior=seed(cache,candidates[0])
    prior['version']=legacy_version(root)
    prior['events'][0]['cancelled']=True
    graph=InputGraph(candidates,root)
    graph.values[candidates[0]['id']]=[root]
    source=DirectSource(graph,extractor)
    first=source.collect('token',candidates,'tenant',cache,'alice')
    corrected=cache.values[candidates[0]['id']]['post']['version']
    assert corrected!=legacy_version(root) and len(extractor.calls)==1
    assert first[0]['id']==prior['events'][0]['id'] and first[0]['cancelled'] is False
    exact=source.collect('token',candidates,'tenant',cache,'alice',
        selected_channels=candidates,selected_messages=['post'])
    cache.fresh_until.clear()
    repeated=source.collect('token',candidates,'tenant',cache,'alice')
    assert cache.values[candidates[0]['id']]['post']['version']==corrected
    assert exact.report['messages_cached']==repeated.report['messages_cached']==1
    assert len(extractor.calls)==1


@pytest.mark.parametrize('with_image',[False,True])
def test_unaffected_legacy_cache_reuses_original_version_without_ai_or_image_reads(with_image):
    candidates,cache,extractor=channels(1),SharedCache(),Recorder()
    root=message('post','<img src="'+image_url()+'">' if with_image else 'Short normal post')
    prior=seed(cache,candidates[0])
    prior['version']=legacy_version(root)
    graph=InputGraph(candidates,root)
    graph.values[candidates[0]['id']]=[root]
    result=DirectSource(graph,extractor).collect('token',candidates,'tenant',cache,'alice')
    assert cache.values[candidates[0]['id']]['post'] is prior and cache.writes==[]
    assert extractor.calls==[] and graph.image_reads==[]
    assert result.report['messages_cached']==1 and result.report['partial'] is False


@pytest.mark.parametrize('kind',['continuation','overfull'])
def test_normal_graph_reply_page_keeps_incompleteness_visible_to_per_post_collection(kind):
    candidates,cache,extractor=channels(1),SharedCache(),Recorder()
    prior=seed(cache,candidates[0])
    reads=[]
    def response(request):
        reads.append(str(request.url))
        if request.url.path.endswith('/messages'):
            return httpx.Response(200,json={'value':[message('good')]})
        if request.url.path.endswith('/post'):
            return httpx.Response(200,json=message('post'))
        assert request.url.path.endswith('/post/replies')
        data={'value':[{'body':{'content':'discussion'}} for _ in range(31 if kind=='overfull' else 30)]}
        if kind=='continuation': data['@odata.nextLink']='https://unsupported.example/continuation'
        return httpx.Response(200,json=data)
    graph=TeamsGraph(lambda:httpx.Client(transport=httpx.MockTransport(response)))
    result=DirectSource(graph,extractor,images=False).collect('token',candidates,'tenant',cache,'alice')
    assert len(reads)==3 and not any('unsupported.example' in url for url in reads)
    assert cache.values[candidates[0]['id']]['post'] is prior
    assert cache.writes==[(candidates[0]['id'],'good')] and len(extractor.calls)==1
    assert result.report['messages_failed']==1 and result.report['partial'] is True
    assert cache.fresh_until=={} and cache.leases=={}


def test_expanded_reply_continuation_is_rejected_before_old_root_age_skip():
    candidates,cache,extractor=channels(1),SharedCache(),Recorder()
    root={**message('post'),'createdDateTime':'2026-01-01T00:00:00Z',
          'replies@odata.nextLink':'https://unsupported.example/continuation'}
    graph=InputGraph(candidates,root)
    graph.values[candidates[0]['id']]=[root]
    result=DirectSource(graph,extractor,images=False).collect('token',candidates,'tenant',cache,'alice')
    assert extractor.calls==[] and cache.writes==[]
    assert result.report['messages_failed']==1 and result.report['partial'] is True
    assert cache.fresh_until=={}


@pytest.mark.parametrize('exact,count',[(False,30),(True,500)])
def test_existing_complete_reply_boundaries_remain_supported(exact,count):
    candidates,cache,extractor=channels(1),SharedCache(),Recorder()
    root=message('post')
    root['replies']=[{'body':{'content':'reply'}} for _ in range(count)]
    graph=InputGraph(candidates,root)
    graph.values[candidates[0]['id']]=[root]
    arguments={'selected_channels':candidates,'selected_messages':['post']} if exact else {}
    result=DirectSource(graph,extractor,images=False).collect('token',candidates,'tenant',cache,'alice',**arguments)
    assert len(extractor.calls)==1 and result.report['partial'] is False


def test_verified_deleted_root_still_cancels_without_content_validation():
    candidates,cache,extractor=channels(1),SharedCache(),Recorder()
    prior=seed(cache,candidates[0])
    root={**incomplete_root('reply_31'),'deletedDateTime':'2026-11-07T00:00:00Z',
          'replies@odata.nextLink':'https://unsupported.example/continuation'}
    graph=InputGraph(candidates,root)
    graph.values[candidates[0]['id']]=[root]
    result=DirectSource(graph,extractor).collect('token',candidates,'tenant',cache,'alice')
    assert extractor.calls==[] and result[0]['id']==prior['events'][0]['id']
    assert result[0]['cancelled'] is True and result.report['partial'] is False
