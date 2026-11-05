"""Local, fake-Graph end-to-end check; no real service is contacted."""
import sqlite3
from fastapi import FastAPI
from fastapi.testclient import TestClient
from web_service.direct_source import DirectSource
from web_service.notification_queue import NotificationQueue
from web_service.notification_webhook import create_notification_router
from web_service.notification_worker import process_notification


def test_webhook_to_authorized_channel_to_event_and_snapshot(tmp_path):
    inbox=NotificationQueue(lambda:sqlite3.connect(tmp_path/'queue.sqlite3'))
    inbox.register('sub','random-state','tenant','team','channel')
    device={'id_hash':'device','user_id':'student','tenant_id':'tenant','object_id':'oid'}
    channel={'id':'channel','team_id':'team','name':'class'}

    class DB:
        values={}; commits=[]
        def device(self,ident): return device
        def begin_sync(self,*args): return 100
        def fail_sync(self,*args): raise AssertionError('unexpected failure')
        def cached_messages(self,ch): return {}
        def catalog_events(self,ids): return list(self.values.values())
        def cache_analysis(self,key,ch,mid,version,events):
            for event in events: self.values[event['id']]=event
        def commit_snapshot(self,dev,channels,events,ttl,lease):
            self.commits.append((dev,channels,events))
        def clock(self): return 0
    class Tokens:
        def acquire(self,device): return 'delegated'
    class Graph:
        def channels(self,*args): return [channel]
        def messages(self,token,chan):
            assert token=='delegated' and chan==channel
            return [{'id':'message-1','subject':'공지','body':{'content':'기한 12월 1일'},'replies':[]}]
    class Extractor:
        configured=True
        def extract(self,text,images,previous):
            return [{'key':'assignment','title':'과제','notes':'','start_date':'2026-12-01',
                     'end_date':'2026-12-01','time':None,'kind':'assignment','cancelled':False}]
    db=DB(); graph=Graph(); source=DirectSource(graph,Extractor(),images=False)
    app=FastAPI(); app.include_router(create_notification_router(inbox))
    with TestClient(app) as client:
        response=client.post('/hooks/graph',json={'value':[{'subscriptionId':'sub','clientState':'random-state'}]})
        assert response.status_code==202
    assert process_notification(inbox,device,db,Tokens(),graph,source,60)=='processed'
    assert len(db.commits)==1 and db.commits[0][2][0]['title']=='과제'
    assert inbox.claim() is None
