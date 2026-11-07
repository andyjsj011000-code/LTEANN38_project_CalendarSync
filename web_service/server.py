"""Test-server entry point: persistent delegated ingestion, reviews and Web Push."""
import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import os
from pathlib import Path
import secrets
from time import monotonic
from fastapi import BackgroundTasks,Depends,HTTPException,Request,Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse,JSONResponse
from pydantic import Field,model_validator
from .api import create_app
from .config import Settings
from .connections import PersistentIdentity,ConnectionTokens,acquire_snapshot
from .direct_source import TeamsGraph,direct_source_from_env
from .oracle_source import source_from_env,OracleSource
from .push import sender_from_env,Subscription,Notifications
from .school import SchoolIntegration
from .school_graph import GraphFailure
from .school_security import SecurityBoundary
from .schemas import Input,TaskInput
from .storage import Missing,PermissionPending,digest
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
        self.notification_runtime=None
        self.device_cookie='__Host-calendarsync_device' if settings.secure_cookies else 'calendarsync_device'
    @property
    def push_enabled(self): return self.sender.configured
    @property
    def shared_ingestion_enabled(self):
        return bool(getattr(self.db,'shared_ingestion_enabled',False) and hasattr(self.source,'collect'))
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
    def logout_response(self,response):
        response.delete_cookie(self.device_cookie,path='/',secure=self.settings.secure_cookies,httponly=True,samesite='lax')
    def logout_request(self,request):
        session=self.db.session(request.cookies.get(self.session_cookie))
        if request.headers.get('x-calendarsync-logout')!='1':
            # Retain session+CSRF compatibility for existing clients. The dedicated
            # non-simple header also permits device revocation after session expiry.
            if not session: raise HTTPException(401,'Sign in or request device logout')
            supplied=request.headers.get('x-csrf-token','')
            if not hmac.compare_digest(supplied.encode('utf-8'),session['csrf_token'].encode('ascii')):
                raise HTTPException(403,'A valid CSRF token is required')
        # SecurityBoundary rejects foreign Origin/Fetch-Site values. Cross-origin
        # browsers cannot add the header because this app grants no CORS access.
        raw=request.cookies.get(self.device_cookie)
        if raw and len(raw)<=128:
            self.db.revoke_device(digest(raw))
        elif session:
            try: device=self.db.session_device(session)
            except Missing: return
            self.db.revoke_device(device['id_hash'],session['user_id'])
    def explicit_logout(self,session): self.db.revoke_device(self.db.session_device(session)['id_hash'],session['user_id'])
    def status(self,session):
        result=super().status(session)
        try: device=self.db.session_device(session)
        except Missing: return {**result,'connection_state':'reauth_required','background_enabled':False,'push_configured':self.sender.configured,'push_registered':False,'ingestion_pending':False}
        pending=self.db.ingestion_pending(device) if self.shared_ingestion_enabled else False
        return {**result,'mode':'test-server','background_enabled':bool(device['background']),'connection_expires':device['expires_at'],'connection_state':device['state'],'push_configured':self.sender.configured,'push_registered':bool(self.db.subscriptions(device['id_hash'])),'ingestion_mode':'direct' if hasattr(self.source,'collect') else 'oracle','ingestion_pending':pending}
    def run_sync(self,device,ingest=False,force=False,*,prepare_collection=False,prepared=None):
        started=monotonic(); logger=logging.getLogger('uvicorn.error')
        def progress(stage,outcome):
            # Only fixed stage/outcome labels and elapsed time leave this method.
            # Diagnostics must never change synchronization or expose inputs.
            try:
                logger.info('CalendarSync sync stage=%s outcome=%s elapsed_ms=%d',
                            stage,outcome,int((monotonic()-started)*1000))
            except Exception:
                pass
        def failure_progress(saved):
            progress('failure','recorded' if saved is True else
                     'not_recorded' if saved is False else 'completed')
        progress('sync','started'); progress('lease','started')
        user=device['user_id']
        tracked=bool(ingest and hasattr(self.source,'collect') and
                     getattr(self.db,'planner_enhancements_enabled',False))
        attempt=self.db.claim_ingestion(device) if tracked else None
        if tracked and not attempt:
            progress('sync','skipped'); return {'state':'busy'}
        attempt_closed=False
        def end_tracking(error_code=None,queued=False):
            nonlocal attempt_closed
            if attempt and not attempt_closed:
                try: self.db.end_ingestion(device,attempt,error_code=error_code,queued=queued)
                except Exception: pass
                attempt_closed=True
        def collection_progress(report):
            try: self.db.update_ingestion(device,attempt,report)
            except Exception: pass
        reuse=bool(ingest and self.shared_ingestion_enabled and prepared and
                   prepared['device']['id_hash']==device['id_hash'] and
                   prepared['device']['user_id']==user and
                   prepared['device']['tenant_id']==device['tenant_id'] and
                   prepared['valid_until']>self.db.clock())
        lease=None if reuse else self.db.begin_sync(user,0 if ingest or force else self.settings.access_seconds,
            **({'preserve_access':True} if self.shared_ingestion_enabled else {}))
        progress('lease','reused' if reuse else 'acquired' if lease else 'skipped')
        if not reuse and not lease:
            end_tracking(queued=True)
            progress('sync','skipped'); return {'state':'busy'} if ingest else None
        def fail_current(state):
            nonlocal lease
            # Shared collection runs after its first snapshot has committed.
            # Reacquire our own lease before recording any collection failure.
            if lease is None: lease=self.db.begin_sync(user,0)
            return self.db.fail_sync(user,state,lease) if lease else False
        try:
            ingestion=None; report=None; reset_channels=False
            collected_scope=None; collected_checked=None; collected_scanned=None
            progress('token','started')
            token,device=acquire_snapshot(self.tokens,device,self.db)
            progress('token','completed'); progress('channels','started')
            channels=prepared['channels'] if reuse else self.graph.channels(token,device['object_id'],device['tenant_id'])
            verified_at=int(self.db.clock())
            progress('channels','completed')
            if not self.source.configured:
                progress('source','not_configured'); progress('failure','started')
                failure_progress(fail_current('source_not_configured'))
                end_tracking('source_not_configured'); return
            progress('source','started')
            if hasattr(self.source,'collect'):
                if ingest:
                    collected_scope={ch['id'] for ch in channels}
                    if self.shared_ingestion_enabled and not reuse:
                        # Publish the authorized cache before potentially slow AI
                        # work, and let normal permission checks run meanwhile.
                        cached=self.db.catalog_events([ch['id'] for ch in channels])
                        self.db.commit_snapshot(device,channels,cached,self.settings.access_seconds,lease)
                        lease=None
                        progress('cached_commit','completed')
                    try:
                        options=({'on_progress':collection_progress} if attempt and
                                 getattr(self.source,'supports_progress',False) else {})
                        events=self.source.collect(token,channels,device['tenant_id'],self.db,user,**options)
                        report=getattr(events,'report',None)
                        if tracked:
                            collected_checked=getattr(events,'checked_channels',None)
                            collected_scanned=getattr(events,'scanned_channels',None)
                            for scope in (collected_checked,collected_scanned):
                                if scope is not None and (not isinstance(scope,(set,frozenset)) or
                                                          not scope<=collected_scope):
                                    raise ValueError('Invalid ingestion channel scope')
                    except GraphFailure as error:
                        if not self.shared_ingestion_enabled or error.state not in {'verification_failed','source_unavailable'}:
                            raise
                        # A failed post fetch does not establish that every
                        # cached channel is inaccessible. Reverify permissions
                        # below before publishing any of the shared catalog.
                        # No per-message progress was returned by this pass.
                        report={'channels_total':len(channels),'channels_scanned':0,
                                'messages_seen':0,'messages_analyzed':0,'messages_cached':0,
                                'messages_failed':0,'partial':True}
                        progress('collection','interrupted')
                    if report is not None:
                        # Only content-free counts cross the API boundary. An
                        # incomplete pass must remain eligible for the worker.
                        counts=('channels_total','channels_scanned','messages_seen',
                                'messages_analyzed','messages_cached','messages_failed')
                        if (not isinstance(report,dict) or type(report.get('partial')) is not bool or
                                any(type(report.get(key)) is not int or report[key]<0 for key in counts)):
                            raise ValueError('Invalid ingestion report')
                        ingestion={key:report[key] for key in counts}
                        ingestion.update(partial=report['partial'],
                                         state='partial' if report['partial'] else 'completed')
                    if self.shared_ingestion_enabled or tracked:
                        # Membership or credentials may have changed during AI
                        # work. Keep the still-valid prior snapshot readable
                        # while a lease serializes the replacement proof. Its
                        # original permission expiry is never extended here.
                        # Non-shared collection still owns its initial lease.
                        if self.shared_ingestion_enabled:
                            lease=self.db.begin_sync(user,0,preserve_access=True)
                            if not lease:
                                end_tracking(queued=True)
                                return {'state':'busy'}
                        token,device=acquire_snapshot(self.tokens,device,self.db)
                        channels=self.graph.channels(token,device['object_id'],device['tenant_id'])
                        events=self.db.catalog_events([ch['id'] for ch in channels])
                        final_scope={ch['id'] for ch in channels}
                        if tracked and final_scope!=collected_scope:
                            reset_channels=True
                            was_partial=bool(report and report.get('partial'))
                            if collected_checked is None and not was_partial:
                                collected_checked=collected_scope
                            report={**(report or {}),'channels_total':len(final_scope),
                                    'partial':was_partial or bool(final_scope-collected_scope)}
                            for key,scope in (('channels_checked',collected_checked),
                                              ('channels_scanned',collected_scanned)):
                                if scope is None: report.pop(key,None)
                                else: report[key]=len(final_scope & scope)
                            if ingestion is not None:
                                for key in ('channels_total','channels_scanned'):
                                    if key in report: ingestion[key]=report[key]
                                    else: ingestion.pop(key,None)
                                ingestion.update(partial=report['partial'],
                                    state='partial' if report['partial'] else 'completed')
                else: events=self.db.catalog_events([ch['id'] for ch in channels])
            elif isinstance(self.source,OracleSource):
                known=self.db.known_oracle_events(user,device['tenant_id'],channels)
                events=self.source.events(channels,device['tenant_id'],known_events=known)
            else: events=self.source.events(channels,device['tenant_id'])
            progress('source','completed'); progress('commit','started')
            context=({'ingestion':{'attempt':attempt,'report':report or {'partial':False},
                                   'reset_channels':reset_channels}}
                     if tracked else {})
            recorded=self.db.commit_snapshot(device,channels,events,self.settings.access_seconds,lease,**context)
            progress('commit','completed')
            if tracked:
                attempt_closed=True
                if not recorded: return {'state':'busy'}
            if prepare_collection and self.shared_ingestion_enabled and not ingest:
                # Internal-only handoff. It contains no token and never enters
                # the API response; queued work revalidates the live device.
                identity={key:device[key] for key in ('id_hash','user_id','tenant_id','object_id','version')}
                return {'device':identity,'channels':[dict(ch) for ch in channels],
                        'valid_until':verified_at+self.settings.access_seconds}
            if ingest and hasattr(self.source,'collect') and not tracked and not (ingestion and ingestion['partial']):
                # Advance the schedule only after the snapshot was committed;
                # failed commits and partial collection need a prompt retry.
                progress('ingest_mark','started')
                self.db.mark_ingested(device['id_hash'])
                progress('ingest_mark','completed')
            if ingest: return ingestion
        except GraphFailure as error:
            progress('sync','graph_failure'); progress('failure','started')
            failure_progress(fail_current(error.state))
            end_tracking(error.state)
            if error.state in {'reauth_required','account_not_allowed'}: self.db.fail_connection(device['id_hash'])
        except Exception:
            progress('sync','source_unavailable'); progress('failure','started')
            failure_progress(fail_current('source_unavailable')); self.db.audit('sync_failed',user)
            end_tracking('source_unavailable')
        finally:
            end_tracking('interrupted')
            progress('sync','finished')
    def run_deferred_ingestion(self,device,prepared=None):
        try:
            self.run_sync(device,ingest=True,prepared=prepared)
        except Exception:
            # A response has already been sent. The durable due-device request
            # will retry database failures without failing a finished response.
            try: self.db.audit('worker_failed',device['user_id'])
            except Exception: pass
    def sync(self,session,background_tasks=None):
        if self.shared_ingestion_enabled:
            device=self.db.session_device(session)
            prepared=self.run_sync(device,force=True,prepare_collection=True)
            result=self.status(session)
            if result['state']=='checking': result['ingestion']={'state':'busy'}
            elif result['state']=='connected' and self.db.request_ingestion(device['id_hash']):
                # Durable due-device scheduling survives a lost response or
                # process restart; the response task simply starts it sooner.
                if background_tasks is not None:
                    background_tasks.add_task(self.run_deferred_ingestion,device,prepared=prepared)
                result['ingestion']={'state':'queued'}
            return result
        ingestion=self.run_sync(self.db.session_device(session),ingest=True)
        result=self.status(session)
        if ingestion is not None: result['ingestion']=ingestion
        return result
    def require_permission_ready(self,session):
        if (self.shared_ingestion_enabled and self.db.sync_status(session['user_id'])['state']=='checking'
                and not self.db.channels(session['user_id'],session['tenant_id'])):
            # Another request owns verification and the old permission proof
            # is absent or expired. Do not turn that temporary state into an
            # empty calendar or a misleading missing-item mutation error.
            raise PermissionPending()
    def check_request(self,session,request):
        checked=self.db.school_session(request.cookies.get(self.session_cookie),self.settings.idle_seconds)
        if not checked: raise HTTPException(401,'Viewing session expired; device notifications remain linked')
        if not self.db.allow('api:'+session['user_id'],300): raise HTTPException(429,'Too many requests')
        path=request.url.path
        if path in {'/api/channels','/api/today','/api/calendar','/api/hidden','/api/changes'} or path.startswith(('/api/events/','/api/channels/','/api/changes/')):
            self.run_sync(self.db.session_device(session),ingest=False)
            self.require_permission_ready(session)
            if (self.shared_ingestion_enabled and request.method=='GET' and
                    (path in {'/api/today','/api/calendar','/api/hidden','/api/changes'} or
                     path.startswith(('/api/events/','/api/changes/')))):
                # Polling can expose each completed post while collection is
                # running. The stored ACL and its original expiry remain the
                # only permission proof; a view never extends that proof.
                self.db.materialize_cached_events(self.db.session_device(session))
    def tick(self):
        owner=secrets.token_hex(16)
        if not self.db.worker_lock(owner): return
        try:
            self.db.maintenance()
            if getattr(self.db,'planner_enhancements_enabled',False): self.db.planner_maintenance()
            for device in self.db.due_devices(3):
                self.db.defer_device(device['id_hash'],60)
                # In injected notification mode, do not poll Graph on the timer.
                if self.notification_runtime is None or not hasattr(self.source,'collect'):
                    self.run_sync(device,ingest=device['ingest_at']<=self.db.clock())
                current=self.db.device(device['id_hash'])
                if current and current['state']!='reauth_required': self.notifications.dispatch(current)
        finally: self.db.worker_unlock(owner)

def create_test_server(settings=None,database=None,identity=None,graph=None,source=None,sender=None,worker=True,preview=False,notification_runtime=None):
    if database is None:
        from .render_guard import ensure_hosted_storage_supported
        ensure_hosted_storage_supported()
    if settings is None:
        settings=Settings.from_env()
        # Screen sessions and background device connections have independent lifetimes.
        settings=replace(settings,session_seconds=int(os.environ.get('WEB_SESSION_SECONDS','28800')),idle_seconds=int(os.environ.get('WEB_IDLE_SECONDS','1800')))
    if not settings.auth_configured: raise ValueError('Configure school Entra authentication')
    if not settings.secure_cookies and not settings.allow_local_login: raise ValueError('HTTPS required')
    db=database or TestDatabase(settings.database_path,settings.data_key)
    if not isinstance(db,TestDatabase):
        # Local SQLite packages do not need to import optional PG candidates.
        from .postgres_database import PostgresDatabase
        if not isinstance(db,PostgresDatabase):
            raise ValueError('Test server requires its separate storage')
        if preview or db.tenant_id!=settings.tenant_id:
            raise ValueError('Experimental PostgreSQL storage requires its configured school tenant')
        if notification_runtime is not None:
            raise ValueError('PostgreSQL notification runtime integration is not implemented')
    provider=identity or PersistentIdentity(settings); graph=graph or TeamsGraph()
    if source is None:
        mode=os.environ.get('WEB_INGESTION_MODE','direct')
        if mode not in {'direct','oracle'}: raise ValueError('Invalid ingestion mode')
        source=direct_source_from_env(graph) if mode=='direct' else source_from_env()
    integration=ServerIntegration(settings,db,provider,graph,source,sender if sender is not None else sender_from_env())
    integration.notification_runtime=notification_runtime
    static=Path(__file__).with_name('static')
    app=create_app(settings,db,provider,frontend_directory=static,integration=integration,frontend_entry='school.html')
    app.state.integration=integration
    if notification_runtime is not None:
        # Explicit injection only: the default entry point is not ready for deployment.
        from .notification_webhook import create_notification_router
        app.include_router(create_notification_router(notification_runtime.inbox))
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
                try:
                    await asyncio.to_thread(integration.tick)
                    if notification_runtime is not None:
                        await asyncio.to_thread(notification_runtime.tick)
                except Exception:
                    try: db.audit('worker_failed','worker')
                    except Exception:
                        # A database outage can also prevent the audit write.
                        # Keep the retry loop alive without logging private errors.
                        pass
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
    def info(): return {'mode':'preview' if preview else 'test-server','school_login':not preview,'synthetic_data_only':preview,'idle_seconds':settings.idle_seconds,'shared_channel_ingestion':integration.shared_ingestion_enabled,'personal_agenda':True,'planner_enhancements':bool(getattr(db,'planner_enhancements_enabled',False))}
    @app.get('/service-worker.js')
    def service_worker(): return FileResponse(static/'service-worker.js',media_type='text/javascript',headers={'Service-Worker-Allowed':'/','Cache-Control':'no-store'})
    @app.get('/manifest.webmanifest')
    def manifest(): return FileResponse(static/'manifest.webmanifest',media_type='application/manifest+json')
    @app.get('/privacy')
    def privacy(): return FileResponse(Path(__file__).parent.parent/'docs'/'TEST_SERVER_PRIVACY.md',media_type='text/plain; charset=utf-8')
    @app.post('/api/teams/sync')
    def sync(background_tasks:BackgroundTasks,session=Depends(require)):
        if not db.allow('manual-sync:'+session['user_id'],2): raise HTTPException(429,'Wait before syncing again')
        return integration.sync(session,background_tasks)
    @app.get('/api/teams/ingestion-status')
    def ingestion_status(session=Depends(require)):
        if not getattr(db,'planner_enhancements_enabled',False): raise HTTPException(404,'Not available')
        return db.ingestion_status(db.session_device(session))
    @app.post('/auth/logout-all',status_code=204)
    def logout_all(session=Depends(require)):
        db.revoke_all(session['user_id']); response=Response(status_code=204)
        response.delete_cookie(integration.session_cookie,path='/',secure=settings.secure_cookies,httponly=True,samesite='lax'); integration.logout_response(response); return response
    @app.get('/api/changes')
    def changes(session=Depends(require)): return {'items':db.changes(session['user_id'],session['tenant_id'])}
    @app.post('/api/changes/{change_id}')
    def decide(change_id:str,body:ChangeDecision,session=Depends(require)):
        db.resolve_change(session['user_id'],session['tenant_id'],change_id,body.accept); return {'saved':True}
    @app.put('/api/events/{event_id}/override')
    def override(event_id:str,body:TaskInput,session=Depends(require)):
        if hasattr(db,'override_event'):
            db.override_event(session['user_id'],session['tenant_id'],event_id,
                              body.model_dump(mode='json',exclude={'display_mode','selected_dates'}))
            return {'saved':True}
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
        integration.require_permission_ready(session)
        return next((e for e in db._shared(session['user_id'],session['tenant_id']) if e['id']==item_id),None) or (_ for _ in ()).throw(Missing())
    @app.get('/api/reminders/{source}/{item_id}')
    def reminders(source:str,item_id:str,session=Depends(require)):
        accessible_item(session,source,item_id); return db.rules(session['user_id'],item_id,source)
    @app.put('/api/reminders/{source}/{item_id}')
    def save_reminders(source:str,item_id:str,body:ReminderRule,session=Depends(require)):
        accessible_item(session,source,item_id); now=datetime.fromtimestamp(db.clock(),timezone.utc)
        existing=set(db.rules(session['user_id'],item_id,source)['custom_times'])
        future=[]
        for value in body.custom_times:
            when=datetime.fromisoformat(value)
            if when<=now and value in existing: continue
            if not now<when<=now+timedelta(days=366): raise HTTPException(422,'Custom reminders must be within the next year')
            future.append(value)
        db.save_rules(session['user_id'],item_id,source,{**body.model_dump(),'custom_times':future}); return {'saved':True}
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
