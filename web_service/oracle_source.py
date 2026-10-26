"""Read-only bridge to legacy master_calendars; no legacy app imports or jobs."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import os
from time import monotonic
from urllib.parse import quote, urlencode
from cryptography.fernet import Fernet

KST=timezone(timedelta(hours=9))

class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.parts=[]
    def handle_data(self,data):
        self.parts.append(data)
    def handle_starttag(self,tag,attrs):
        if tag in {"br","p","div","li"}:
            self.parts.append("\n")

def plain(value):
    parser=PlainText(); parser.feed(value or ""); return "".join(parser.parts).strip()[:10000]

@dataclass(frozen=True)
class OracleConfig:
    user: str
    password: str=field(repr=False)
    dsn: str=field(repr=False)
    data_key: str=field(repr=False)
    wallet_dir: str=""
    wallet_password: str=field(default="",repr=False)

class NoSource:
    configured=False
    def events(self,channels,tenant_id):
        return []

class OracleSource:
    configured=True
    def __init__(self,config,connect=None):
        self.config=config; self.cipher=Fernet(config.data_key.encode()); self._connect=connect

    def _decrypt(self,value):
        # Wrong keys fail closed: ciphertext is never displayed as source text.
        if value is None:
            return ""
        return self.cipher.decrypt(str(value).encode()).decode("utf-8")

    def _event(self,row,ch,tenant_id):
        item_id,channel_id,title,description,start,end,message_id=row
        if channel_id != ch["id"]:
            raise ValueError("Source channel mismatch")
        if start.tzinfo: start=start.astimezone(KST)
        if end.tzinfo: end=end.astimezone(KST)
        if end<start:
            raise ValueError("Invalid source period")
        title=self._decrypt(title)[:500]
        kind="assignment" if any(x in title for x in ("과제","제출","마감")) else "application" if "신청" in title else "event"
        link="https://teams.microsoft.com/l/message/"+quote(str(channel_id),safe="")+"/"+quote(str(message_id),safe="")+"?"+urlencode({"groupId":ch["team_id"],"tenantId":tenant_id})
        return {"id":str(item_id),"channel_id":channel_id,"title":title,"notes":plain(self._decrypt(description)),"start_date":start.date().isoformat(),"end_date":end.date().isoformat(),"time":end.strftime("%H:%M") if kind in {"assignment","application"} else start.strftime("%H:%M"),"kind":kind,"source_url":link}

    def events(self,channels,tenant_id,known_events=()):
        if not channels:
            return []
        connect=self._connect
        if connect is None:
            import oracledb
            connect=oracledb.connect
        kwargs={"user":self.config.user,"password":self.config.password,"dsn":self.config.dsn,"tcp_connect_timeout":8,"retry_count":0}
        if self.config.wallet_dir:
            kwargs.update(config_dir=self.config.wallet_dir,wallet_location=self.config.wallet_dir,wallet_password=self.config.wallet_password)
        now=datetime.now(KST).replace(tzinfo=None); results=[]; deadline=monotonic()+25
        with connect(**kwargs) as connection:
            connection.call_timeout=8000
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                for ch in channels:
                    if monotonic()>deadline:
                        raise TimeoutError("Source import time limit")
                    cursor.execute("SELECT id,source_channel_id,title,description,start_datetime,end_datetime,raw_message_id FROM master_calendars WHERE source_channel_id=:channel_id AND end_datetime>=:start_at AND start_datetime<=:end_at", channel_id=ch["id"],start_at=now-timedelta(days=90),end_at=now+timedelta(days=365))
                    rows=cursor.fetchmany(1001)
                    if len(rows)>1000:
                        raise ValueError("Source channel exceeds import limit")
                    for row in rows:
                        results.append(self._event(row,ch,tenant_id))
                # A windowed listing cannot prove deletion. Re-read missing known
                # IDs in bounded batches within the same read-only snapshot.
                present={(e['channel_id'],e['id']) for e in results}
                for ch in channels:
                    missing={str(e['id']):e for e in known_events if e['channel_id']==ch['id'] and (ch['id'],str(e['id'])) not in present}
                    ids=list(missing)
                    for offset in range(0,len(ids),50):
                        if monotonic()>deadline: raise TimeoutError("Source import time limit")
                        batch=ids[offset:offset+50]
                        params={'channel_id':ch['id'],**{'id_'+str(i):item for i,item in enumerate(batch)}}
                        names=','.join(':id_'+str(i) for i in range(len(batch)))
                        cursor.execute("SELECT id,source_channel_id,title,description,start_datetime,end_datetime,raw_message_id FROM master_calendars WHERE source_channel_id=:channel_id AND id IN ("+names+")",**params)
                        checked=cursor.fetchmany(len(batch)+1)
                        if len(checked)>len(batch): raise ValueError("Invalid source lookup result")
                        found=set()
                        for row in checked:
                            item=self._event(row,ch,tenant_id)
                            if item['id'] not in batch or item['id'] in found: raise ValueError("Source ID mismatch")
                            found.add(item['id']); results.append(item)
                        for item_id in batch:
                            if item_id not in found:
                                results.append({**missing[item_id],'cancelled':True})
            connection.rollback()
        return results

def source_from_env():
    names=("WEB_ORACLE_USER","WEB_ORACLE_PASSWORD","WEB_ORACLE_DSN","WEB_ORACLE_DATA_KEY")
    values=[os.environ.get(name,"") for name in names]
    if not any(values):
        return NoSource()
    if not all(values):
        raise ValueError("Configure all WEB_ORACLE_* credentials")
    return OracleSource(OracleConfig(*values,wallet_dir=os.environ.get("WEB_ORACLE_WALLET_DIR",""),wallet_password=os.environ.get("WEB_ORACLE_WALLET_PASSWORD","")))
