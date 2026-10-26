"""Web Push with endpoint validation, encrypted subscriptions and private payloads."""
import base64
from datetime import datetime,timedelta,timezone
import json
import os
from urllib.parse import urlsplit
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import Field,model_validator
from .schemas import Input
from .storage import digest

KST=timezone(timedelta(hours=9))

def unbase64(value): return base64.urlsafe_b64decode(value+'='*(-len(value)%4))
def b64(value): return base64.urlsafe_b64encode(value).decode().rstrip('=')

def validate_endpoint(endpoint):
    url=urlsplit(endpoint)
    host=url.hostname or ''
    allowed=host in {'fcm.googleapis.com','updates.push.services.mozilla.com','web.push.apple.com'} or host.endswith('.notify.windows.com')
    if not allowed or url.scheme!='https' or url.username or url.password or url.port not in {None,443} or url.fragment or not url.path or len(endpoint)>4096: raise ValueError('Unsupported push endpoint')
    return endpoint

class PushKeys(Input):
    p256dh:str=Field(min_length=80,max_length=100,pattern=r'^[A-Za-z0-9_-]+=*$')
    auth:str=Field(min_length=20,max_length=30,pattern=r'^[A-Za-z0-9_-]+=*$')

class Subscription(Input):
    endpoint:str=Field(max_length=4096)
    expirationTime:float|None=None
    keys:PushKeys
    @model_validator(mode='after')
    def validate_subscription(self):
        validate_endpoint(self.endpoint)
        public=unbase64(self.keys.p256dh)
        if len(public)!=65 or len(unbase64(self.keys.auth))!=16: raise ValueError('Invalid push keys')
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),public)
        return self

class PushSender:
    def __init__(self,private='',subject=''):
        self.private,self.subject=private,subject; self.public=''
        if bool(private)!=bool(subject): raise ValueError('Configure both VAPID values')
        if private:
            key=serialization.load_der_private_key(unbase64(private),password=None)
            if not isinstance(key,ec.EllipticCurvePrivateKey) or not isinstance(key.curve,ec.SECP256R1): raise ValueError('VAPID requires P-256')
            if not subject.startswith(('mailto:','https://')): raise ValueError('Invalid VAPID contact')
            self.public=b64(key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint))
    @property
    def configured(self): return bool(self.public)
    def send(self,subscription,payload):
        from pywebpush import webpush,WebPushException
        import requests
        validated=Subscription.model_validate(subscription).model_dump()
        class NoRedirectSession(requests.Session):
            def request(self,method,url,**kwargs):
                validate_endpoint(url); kwargs['allow_redirects']=False
                kwargs['timeout']=10
                return super().request(method,url,**kwargs)
        try:
            with NoRedirectSession() as client:
                client.trust_env=False
                response=webpush(subscription_info=validated,data=json.dumps(payload,ensure_ascii=False),vapid_private_key=self.private,vapid_claims={'sub':self.subject},timeout=10,ttl=600,requests_session=client)
            return 'sent' if response.status_code in {200,201,202} else 'retry'
        except WebPushException as error:
            status=error.response.status_code if error.response is not None else None
            return 'expired' if status in {404,410} else 'retry'
        except Exception: return 'retry'

def sender_from_env(): return PushSender(os.environ.get('WEB_VAPID_PRIVATE_KEY',''),os.environ.get('WEB_VAPID_SUBJECT',''))

class Notifications:
    def __init__(self,db,sender): self.db,self.sender=db,sender
    def due(self,user,tenant):
        now=datetime.fromtimestamp(self.db.clock(),KST); today=now.date().isoformat()
        prefs=self.db.preferences(user)
        pending={c['event_id'] for c in self.db.changes(user,tenant)}
        candidates=[]
        # Keep the morning Teams and personal notifications separate.
        agenda=self.db.agenda(user,tenant,today)
        morning=datetime.fromisoformat(today+'T'+prefs['morning_time']).replace(tzinfo=KST)
        if prefs['morning_enabled'] and 0<=(now-morning).total_seconds()<600:
            for group in ('teams','personal'):
                visible=[e for e in agenda[group] if not e['completed'] and not e['cancelled'] and e['id'] not in pending]
                if visible: candidates.append(('morning:'+today+':'+group,{'title':'CalendarSync','body':'학교 일정을 확인할 시간이에요.' if group=='teams' else '개인 할 일을 확인할 시간이에요.','url':'/','tag':'morning-'+today+'-'+group}))
        for item in self.db.calendar(user,tenant,'0001-01-01','9999-12-31'):
            if item['completed'] or item['cancelled'] or item['id'] in pending: continue
            deadline=datetime.fromisoformat(item['end_date']+'T'+(item.get('time') or '23:59')).replace(tzinfo=KST)
            rule=self.db.rules(user,item['id'],item['source'])
            times=[]
            for offset in rule['offsets']:
                try: times.append(deadline+timedelta(minutes=offset))
                except OverflowError: continue
            for value in rule['custom_times']:
                try: times.append(datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(KST))
                except (OverflowError,ValueError): continue
            for due in times:
                if 0<=(now-due).total_seconds()<600:
                    key='item:'+item['source']+':'+item['id']+':'+due.isoformat()
                    candidates.append((key,{'title':'CalendarSync','body':'확인할 학교 일정이 있어요.' if item['source']=='teams' else '확인할 개인 할 일이 있어요.','url':'/','tag':digest(key)[:24]}))
        return candidates

    def dispatch(self,device):
        if not self.sender.configured or not self.db.device(device['id_hash']): return 0
        sent=0
        for subscription in self.db.subscriptions(device['id_hash']):
            for key,payload in self.due(device['user_id'],device['tenant_id']):
                delivery=digest(subscription['id']+'|'+key)
                if not self.db.device(device['id_hash']): return sent
                if not self.db.claim_delivery(delivery,subscription['id']): continue
                try: outcome=self.sender.send(subscription['subscription'],payload)
                except Exception: outcome='retry'
                if outcome=='expired':
                    self.db.unsubscribe_subscription(device['id_hash'],subscription['id']); break
                self.db.finish_delivery(delivery,outcome=='sent')
                sent+=int(outcome=='sent')
        return sent
