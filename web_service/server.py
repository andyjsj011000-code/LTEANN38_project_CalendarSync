"""Test-server entry point: persistent delegated ingestion, reviews and Web Push."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import os
from pathlib import Path
import secrets
from fastapi import Depends,HTTPException,Request,Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse,JSONResponse
from pydantic import Field,model_validator
from .api import create_app
from .config import Settings
from .connections import PersistentIdentity,ConnectionTokens
from .direct_source import TeamsGraph,direct_source_from_env
from .oracle_source import source_from_env
from .push import sender_from_env,Subscription,Notifications
from .school import SchoolIntegration
from .school_graph import GraphFailure
from .school_security import SecurityBoundary
from .schemas import Input,TaskInput
from .storage import Missing,digest
from .test_storage import TestDatabase

class ChangeDecision(Input): accept:bool
class Feedback(Input): message:str=Field(min_length=1,max_length=4000)
class ReminderRule(Input):
    offsets:list[int]=Field(default_factory=lambda:[-1440,-60],max_length=10)
    custom_times:list[str]=Field(default_factory=list,max_length=10)
    @model_validator(mode='after')
    def valid_times(self):
        if any(x>0 or x< -10080 for x in self.offsets): raise ValueError('Invalid reminder offset')
        self.offsets=sorted(set(self.offsets)); parsed=[]
        for value in self.custom_times:
            dt=datetime.fromisoformat(value.replace('Z','+00:00'))
            if dt.tzinfo is None: raise ValueError('Reminder times must include timezone')
            try: parsed.append(dt.astimezone(timezone.utc).isoformat())
            except OverflowError: raise ValueError('Reminder time is out of range') from None
        self.custom_times=sorted(set(parsed)); return self

class ServerIntegration(SchoolIntegration):
    def __init__(self,settings,db,identity,graph,source,sender):
        super().__init__(settings,db,graph,source)
        self.identity=identity; self.tokens=ConnectionTokens(db,identity)
        self.sender=sender; self.notifications=Notifications(db,sender); self._cookie_responses={}
        self.device_cookie='__Host-calendarsync_device' if settings.secure_cookies else 'calendarsync_device'
    @property
    def push_enabled(self): return self.sender.configured
    def begin_login(self,provider,request): return provider.begin(background=request.query_params.get('background')=='1')
    def complete_sign_in(self,raw,user,claims,request):
        old=request.cookies.get(self.device_cookie)
        if old: self.db.revoke_device(digest(old))
        proof=claims['_graph']; link=claims.get('_connection',{})
        device,age=self.db.add_connection(raw,user,proof['token'],int(proof['expires_in']),link.get('cache',''),bool(link.get('background')))
        self._cookie_responses[digest(raw)]=(device,age)
        self.db.audit('connected',user)
    def login_response(self,response,raw):
        device,age=self._cookie_responses.pop(digest(raw))
        response.set_cookie(self.device_cookie,device,max_age=age,secure=self.settings.secure_cookies,httponly=True,samesite='lax',path='/')
    def logout_response(self,response): response.delete_cookie(self.device_cookie,path='/')
    def explicit_logout(self,session): self.db.revoke_device(self.db.session_device(session)['id_hash'],session['user_id'])
    def status(self,session):
        result=super().status(session)
        try: device=self.db.session_device(session)
        except Missing: return {**result,'connection_state':'reauth_required','background_enabled':False,'push_configured':self.sender.configured,'push_registered':False}
        return {**result,'mode':'test-server','background_enabled':bool(device['background']),'connection_expires':device['expires_at'],'connection_state':device['state'],'push_configured':self.sender.configured,'push_registered':bool(self.db.subscriptions(device['id_hash'])),'ingestion_mode':'direct' if hasattr(self.source,'collect') else 'oracle'}
    def run_sync(self,device,ingest=False):
        user=device['user_id']; lease=self.db.begin_sync(user,0 if ingest else self.settings.access_seconds)
        if not lease: return
        try:
            token=self.tokens.acquire(device)
            channels=self.graph.channels(token,device['object_id'],device['tenant_id'])
            if not self.source.configured:
                self.db.fail_sync(user,'source_not_configured',lease); return
            if hasattr(self.source,'collect'):
                if ingest:
                    events=self.source.collect(token,channels,device['tenant_id'],self.db,user)
                    self.db.mark_ingested(device['id_hash'])
                else: events=self.db.catalog_events([ch['id'] for ch in channels])
            else: events=self.source.events(channels,device['tenant_id'])
            self.db.commit_snapshot(device,channels,events,self.settings.access_seconds,lease)
        except GraphFailure as error:
            self.db.fail_sync(user,error.state,lease)
            if error.state in {'reauth_required','account_not_allowed'}: self.db.fail_connection(device['id_hash'])
        except Exception:
            self.db.fail_sync(user,'source_unavailable',lease); self.db.audit('sync_failed',user)
    def sync(self,session):
        self.run_sync(self.db.session_device(session),ingest=True)
        return self.status(session)
    def check_request(self,session,request):
        checked=self.db.school_session(request.cookies.get(self.session_cookie),self.settings.idle_seconds)
        if not checked: raise HTTPException(401,'Viewing session expired; device notifications remain linked')
        if not self.db.allow('api:'+session['user_id'],300): raise HTTPException(429,'Too many requests')
        path=request.url.path
        if path in {'/api/channels','/api/today','/api/calendar','/api/hidden','/api/changes'} or path.startswith(('/api/events/','/api/channels/','/api/changes/')):
            self.run_sync(self.db.session_device(session),ingest=False)
    def tick(self):
        owner=secrets.token_hex(16)
        if not self.db.worker_lock(owner): return
        try:
            self.db.maintenance()
            for device in self.db.due_devices(3):
                self.db.defer_device(device['id_hash'],60)
                self.run_sync(device,ingest=device['ingest_at']<=self.db.clock())
                current=self.db.device(device['id_hash'])
                if current and current['state']!='reauth_required': self.notifications.dispatch(current)
        finally: self.db.worker_unlock(owner)

def create_test_server(settings=None,database=None,identity=None,graph=None,source=None,sender=None,worker=True,preview=False):
    if settings is None:
        settings=Settings.from_env()
        # Screen sessions and background device connections have independent lifetimes.
        settings=replace(settings,session_seconds=int(os.environ.get('WEB_SESSION_SECONDS','28800')),idle_seconds=int(os.environ.get('WEB_IDLE_SECONDS','1800')))
    if not settings.auth_configured: raise ValueError('Configure school Entra authentication')
    if not settings.secure_cookies and not settings.allow_local_login: raise ValueError('HTTPS required')
    db=database or TestDatabase(settings.database_path,settings.data_key)
    if not isinstance(db,TestDatabase): raise ValueError('Test server requires its separate storage')
    provider=identity or PersistentIdentity(settings); graph=graph or TeamsGraph()
    if source is None:
        mode=os.environ.get('WEB_INGESTION_MODE','direct')
        if mode not in {'direct','oracle'}: raise ValueError('Invalid ingestion mode')
        source=direct_source_from_env(graph) if mode=='direct' else source_from_env()
    integration=ServerIntegration(settings,db,provider,graph,source,sender if sender is not None else sender_from_env())
    static=Path(__file__).with_name('static')
    app=create_app(settings,db,provider,frontend_directory=static,integration=integration,frontend_entry='school.html')
    app.state.integration=integration
    app.add_middleware(SecurityBoundary,settings=settings)
    require=app.state.require_session

    @app.exception_handler(RequestValidationError)
    async def invalid(request,error): return JSONResponse({'detail':'Check the input and dates'},status_code=422)
    @app.exception_handler(Exception)
    async def failed(request,error): return JSONResponse({'detail':'Request failed; check service status'},status_code=500)

    @asynccontextmanager
    async def lifespan(app):
        stop=asyncio.Event()
        async def loop():
            while not stop.is_set():
                try: await asyncio.to_thread(integration.tick)
                except Exception: db.audit('worker_failed','worker')
                try: await asyncio.wait_for(stop.wait(),timeout=60)
                except TimeoutError: pass
        task=asyncio.create_task(loop()) if worker else None
        yield
        stop.set()
        if task:
            try: await asyncio.wait_for(task,timeout=5)
            except TimeoutError: task.cancel()
    app.router.lifespan_context=lifespan

    @app.get('/api/service')
    def info(): return {'mode':'preview' if preview else 'test-server','school_login':not preview,'synthetic_data_only':preview,'idle_seconds':settings.idle_seconds}
    @app.get('/service-worker.js')
    def service_worker(): return FileResponse(static/'service-worker.js',media_type='text/javascript',headers={'Service-Worker-Allowed':'/','Cache-Control':'no-store'})
    @app.get('/manifest.webmanifest')
    def manifest(): return FileResponse(static/'manifest.webmanifest',media_type='application/manifest+json')
    @app.get('/privacy')
    def privacy(): return FileResponse(Path(__file__).parent.parent/'docs'/'TEST_SERVER_PRIVACY.md',media_type='text/plain; charset=utf-8')
    @app.post('/api/teams/sync')
    def sync(session=Depends(require)):
        if not db.allow('manual-sync:'+session['user_id'],2): raise HTTPException(429,'Wait before syncing again')
        return integration.sync(session)
    @app.post('/auth/logout-all',status_code=204)
    def logout_all(session=Depends(require)):
        db.revoke_all(session['user_id']); response=Response(status_code=204)
        response.delete_cookie(integration.session_cookie,path='/'); integration.logout_response(response); return response
    @app.get('/api/changes')
    def changes(session=Depends(require)): return {'items':db.changes(session['user_id'],session['tenant_id'])}
    @app.post('/api/changes/{change_id}')
    def decide(change_id:str,body:ChangeDecision,session=Depends(require)):
        db.resolve_change(session['user_id'],session['tenant_id'],change_id,body.accept); return {'saved':True}
    @app.put('/api/events/{event_id}/override')
    def override(event_id:str,body:TaskInput,session=Depends(require)):
        events={e['id']:e for e in db._shared(session['user_id'],session['tenant_id'])}
        if event_id not in events: raise Missing()
        item={**events[event_id],**body.model_dump(mode='json',exclude={'display_mode','selected_dates'})}
        with db.connect() as c:
            c.execute('INSERT INTO web_event_states(user_id,event_id,override_blob) VALUES(?,?,?) ON CONFLICT(user_id,event_id) DO UPDATE SET override_blob=excluded.override_blob',(session['user_id'],event_id,db.seal(item)))
        return {'saved':True}
    @app.post('/api/feedback',status_code=201)
    def feedback(body:Feedback,session=Depends(require)):
        if not db.allow('feedback:'+session['user_id'],5): raise HTTPException(429,'Try again later')
        return {'id':db.save_feedback(session['user_id'],body.message)}
    def accessible_item(session,source,item_id):
        if source=='personal': return db.task(session['user_id'],item_id)
        if source!='teams': raise Missing()
        integration.run_sync(db.session_device(session),False)
        return next((e for e in db._shared(session['user_id'],session['tenant_id']) if e['id']==item_id),None) or (_ for _ in ()).throw(Missing())
    @app.get('/api/reminders/{source}/{item_id}')
    def reminders(source:str,item_id:str,session=Depends(require)):
        accessible_item(session,source,item_id); return db.rules(session['user_id'],item_id,source)
    @app.put('/api/reminders/{source}/{item_id}')
    def save_reminders(source:str,item_id:str,body:ReminderRule,session=Depends(require)):
        accessible_item(session,source,item_id); now=datetime.fromtimestamp(db.clock(),timezone.utc)
        if any(not now<datetime.fromisoformat(t)<=now+timedelta(days=366) for t in body.custom_times): raise HTTPException(422,'Custom reminders must be within the next year')
        db.save_rules(session['user_id'],item_id,source,body.model_dump()); return {'saved':True}
    @app.get('/api/push')
    def push_info(session=Depends(require)):
        device=db.session_device(session)
        return {'configured':integration.sender.configured,'public_key':integration.sender.public,'registered':bool(db.subscriptions(device['id_hash'])),'background_enabled':bool(device['background'])}
    @app.post('/api/push',status_code=201)
    def subscribe(body:Subscription,session=Depends(require)):
        if not integration.sender.configured: raise HTTPException(503,'Push is not configured')
        device=db.session_device(session)
        if not device['background']: raise HTTPException(409,'Sign in with device connection enabled first')
        db.subscribe(device,body.model_dump()); return {'saved':True}
    @app.delete('/api/push',status_code=204)
    def unsubscribe(session=Depends(require)): db.unsubscribe(db.session_device(session)['id_hash'])
    @app.post('/api/push/test')
    def test_push(session=Depends(require)):
        device=db.session_device(session); subscriptions=db.subscriptions(device['id_hash'])
        if not integration.sender.configured or not subscriptions: raise HTTPException(409,'Enable notifications on this device first')
        if not db.allow('push-test:'+device['id_hash'],1): raise HTTPException(429,'Wait a minute before testing again')
        outcomes=[integration.sender.send(s['subscription'],{'title':'CalendarSync','body':'테스트 알림이 도착했어요.','url':'/','tag':'calendarsync-test'}) for s in subscriptions]
        return {'accepted':outcomes.count('sent'),'failed':len(outcomes)-outcomes.count('sent')}
    return app
