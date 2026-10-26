"""Explicit loopback-only synthetic preview. Never connect this entry point to school data."""
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
from uuid import uuid4
from cryptography.fernet import Fernet
from .config import Settings
from .server import create_test_server
from .test_storage import TestDatabase
from .push import PushSender

TENANT='00000000-0000-4000-8000-000000000101'
CLIENT='00000000-0000-4000-8000-000000000102'
USER='00000000-0000-4000-8000-000000000103'
KST=timezone(timedelta(hours=9))

class PreviewIdentity:
    def begin(self,background=False):
        return {'auth_uri':'/auth/callback?state=preview&code=preview','state':'preview','background_allowed':background}
    def finish(self,flow,query):
        if flow.get('state')!='preview' or query!={'state':'preview','code':'preview'}: raise ValueError('Invalid preview flow')
        now=int(datetime.now(timezone.utc).timestamp())
        return {'tid':TENANT,'oid':USER,'iss':f'https://login.microsoftonline.com/{TENANT}/v2.0','aud':CLIENT,'sub':'preview-only','exp':now+3600,'name':'미리보기 학생','preferred_username':'preview@example.invalid','_graph':{'token':'synthetic-preview-token','expires_in':3600},'_connection':{'cache':'synthetic-only','background':bool(flow.get('background_allowed'))}}
    def refresh(self,*args): return 'synthetic-preview-token',3600,'synthetic-only'

class PreviewGraph:
    def assert_member(self,*args): pass
    def channels(self,*args): return [{'id':'preview-class','name':'학급 공지 · 예시','team_id':CLIENT},{'id':'preview-common','name':'전교 공지 · 예시','team_id':CLIENT}]

class PreviewSource:
    configured=True
    def __init__(self): self.calls=0
    def events(self,channels,tenant):
        self.calls+=1; day=datetime.now(KST).date()
        samples=[('physics','preview-class','물리학 탐구 보고서 제출',day,day,'18:00','assignment'),('club','preview-common','동아리 신청 마감',day+timedelta(days=1),day+timedelta(days=1),'17:00','application'),('project','preview-class','탐구 발표 준비 기간',day-timedelta(days=2),day+timedelta(days=4),None,'event')]
        allowed={c['id'] for c in channels}
        return [{'id':key,'channel_id':channel,'title':title,'notes':'예시 전용 공지입니다. 실제 학교 공지나 계정과 연결하지 않습니다. Teams 공지 가져오기를 누르면 첫 일정의 변경 후보를 시험할 수 있습니다.','start_date':start.isoformat(),'end_date':end.isoformat(),'time':'19:00' if key=='physics' and self.calls>1 else at,'kind':kind,'cancelled':False,'source_url':None} for key,channel,title,start,end,at,kind in samples if channel in allowed]

def create_preview():
    port=int(os.environ.get('CALENDARSYNC_PREVIEW_PORT','8093'))
    directory=Path(os.environ.get('CALENDARSYNC_PREVIEW_DATA','web_service/var/preview')).resolve()
    directory.mkdir(parents=True,exist_ok=True); config=directory/'preview-key.json'
    if not config.exists():
        with config.open('x',encoding='utf-8') as file: json.dump({'format':'calendarsync-preview-v2','key':Fernet.generate_key().decode()},file)
        config.chmod(0o600)
    data=json.loads(config.read_text(encoding='utf-8'))
    if data.get('format')!='calendarsync-preview-v2': raise ValueError('Not a preview key')
    settings=Settings(database_path=directory/'preview.sqlite3',data_key=data['key'],origin=f'http://127.0.0.1:{port}',tenant_id=TENANT,client_id=CLIENT,client_secret='synthetic-only',allow_local_login=True,session_seconds=28800,idle_seconds=1800)
    db=TestDatabase(settings.database_path,settings.data_key)
    if db.path.exists():
        with db.connect() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='web_users'").fetchone() and conn.execute('SELECT 1 FROM web_users WHERE tenant_id<>? LIMIT 1',(TENANT,)).fetchone(): raise ValueError('Preview cannot open real account data')
    return create_test_server(settings,db,PreviewIdentity(),PreviewGraph(),PreviewSource(),PushSender(),worker=False,preview=True)
