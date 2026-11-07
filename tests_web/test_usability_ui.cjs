'use strict';
// Synthetic controller regressions. No real school data, network or layout claim.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.resolve(__dirname, '../web_service/static');
const source = name => fs.readFileSync(path.join(assets, name), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const settle = async () => {for (let n = 0; n < 8; n++) await tick();};
const response = (data, status = 200) => ({status, ok: status < 400, async json() {return data;}});
function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => {resolve = done; reject = fail;});
  return {promise, resolve, reject};
}
const writes = h => h.requests.filter(r => ['POST','PUT','PATCH','DELETE'].includes(r.options.method));
const calendarUrl = (h, month) => {const grid = h.context.window.BetaCalendar.monthGrid(month); return `/api/calendar?start=${grid.start}&end=${grid.end}`;};
const filtersForm = (channels, disabled = []) => ({id:'filters-form', querySelectorAll(selector) {
  return selector === '[name=channel]' ? channels.map(ch => ({value:ch.id, checked:ch.enabled})) : ['assignment','application','event'].map(value => ({value, checked:!disabled.includes(value)}));
}});

(async () => {
  // A slow manual collection never consumes navigation or replaces an open draft.
  {
    const pending = deferred(), h = harness(); await settle();
    h.overrides['/api/teams/sync'] = () => pending.promise;
    const sync = h.click({action:'sync-teams'}); await settle();
    assert.equal(h.elements['sync-button'].disabled, true);
    await h.advance(10000); assert.match(h.elements['sync-status'].textContent, /가져오고/);
    await h.click({action:'calendar'}); assert.match(h.elements.app.innerHTML, /calendar-grid/);
    await h.click({detail:'fixture', source:'teams'}); assert.equal(h.elements.modal.open, true);
    await h.click({action:'today'}); assert.match(h.elements.app.innerHTML, /quick-form/);
    await h.click({action:'sync-teams'}); assert.equal(writes(h).length, 1);
    h.elements.modal.innerHTML = '<textarea>Unsent feedback draft</textarea>';
    h.elements['quick-form'] = {elements:{title:{value:'Unsent quick task'}}};
    pending.resolve(response({state:'connected'})); await sync; await settle();
    assert.equal(h.elements.modal.open, true); assert.equal(h.elements.modal.innerHTML, '<textarea>Unsent feedback draft</textarea>');
    assert.match(h.elements.app.innerHTML, /value="Unsent quick task"/);
    assert.equal(h.elements['sync-button'].disabled, false);
  }
  // A missing POST response has a finite wait and no automatic write retry.
  {
    const pending = deferred(), h = harness({ignoreAbort:new Set(['/api/teams/sync'])}); await settle();
    h.overrides['/api/teams/sync'] = () => pending.promise;
    const sync = h.click({action:'sync-teams'}); await settle(); await h.advance(210001); await sync;
    assert.match(h.elements['sync-status'].textContent, /서버에서는 계속 처리 중/);
    assert.equal(h.elements['sync-button'].disabled, false); assert.equal(writes(h).length, 1);
    await h.click({action:'calendar'}); assert.match(h.elements.app.innerHTML, /calendar-grid/);
    const before = h.requests.length;
    pending.resolve(response({state:'connected'})); await settle();
    assert.equal(h.requests.length, before); assert.match(h.elements['sync-status'].textContent, /처리 중/);
  }
  // Calendar arrows only read that range; consecutive clicks use the pending destination.
  {
    const h = harness(); await settle(); await h.click({action:'calendar'});
    const december = deferred(), january = deferred();
    h.overrides[calendarUrl(h,'2026-12')] = () => december.promise;
    h.overrides[calendarUrl(h,'2027-01')] = () => january.promise;
    const before = h.requests.length;
    const first = h.click({action:'next-month'}); await settle();
    assert.match(h.elements.app.innerHTML, /2026년 11월/); assert.match(h.elements.app.innerHTML, /2026-12 일정을/);
    const second = h.click({action:'next-month'}); await settle();
    assert.equal(h.requests.length, before + 2); assert.ok(h.requests.slice(before).every(r => r.url.startsWith('/api/calendar?')));
    january.resolve(response({items:[{...h.item,title:'January only',start_date:'2027-01-01',end_date:'2027-01-01'}]})); await second;
    december.resolve(response({items:[{...h.item,title:'Stale December'}]})); await first; await settle();
    assert.match(h.elements.app.innerHTML, /2027년 1월/); assert.match(h.elements.app.innerHTML, /January only/); assert.doesNotMatch(h.elements.app.innerHTML, /Stale December/);
  }
  // A failed destination leaves both the old month label and its items intact.
  {
    const h = harness(); await settle(); await h.click({action:'calendar'});
    h.overrides[calendarUrl(h,'2026-12')] = () => response({detail:'Calendar temporarily unavailable'},503);
    await h.click({action:'next-month'});
    assert.match(h.elements.app.innerHTML, /2026년 11월/); assert.match(h.elements.app.innerHTML, /Fixture assignment/);
    assert.doesNotMatch(h.elements.app.innerHTML, /일정을 불러오는 중/);
    delete h.overrides[calendarUrl(h,'2026-12')]; await h.click({action:'next-month'}); assert.match(h.elements.app.innerHTML, /2026년 12월/);
  }
  // A newer month wins over a simultaneous background collection refresh.
  {
    const h = harness(); await settle(); await h.click({action:'calendar'});
    const pendingToday = deferred(); h.overrides['/api/today'] = () => pendingToday.promise;
    h.overrides['/api/teams/sync'] = () => response({state:'connected'});
    const sync = h.click({action:'sync-teams'}); await settle();
    h.overrides[calendarUrl(h,'2026-12')] = () => response({items:[{...h.item,title:'Latest month item',start_date:'2026-12-01',end_date:'2026-12-01'}]});
    await h.click({action:'next-month'}); pendingToday.resolve(response(h.today)); await sync;
    assert.match(h.elements.app.innerHTML, /2026년 12월/); assert.match(h.elements.app.innerHTML, /Latest month item/);
  }
  // Session loss from a month read still removes all private content.
  {
    const h = harness(); await settle(); await h.click({action:'calendar'});
    h.overrides[calendarUrl(h,'2026-12')] = () => response({},401); await h.click({action:'next-month'});
    assert.match(h.elements.app.innerHTML, /school-signin/); assert.doesNotMatch(h.elements.app.innerHTML, /Fixture assignment/);
  }
  // Sixty-three unchanged channels cause no writes; one change causes one PUT.
  {
    const channels = Array.from({length:63},(_,i) => ({id:`channel-${i}`,name:`Channel ${i}`,enabled:true}));
    const h = harness({overrides:{'/api/channels':() => response(channels.map(ch => ({...ch})))}}); await settle();
    await h.submit(filtersForm(channels)); assert.equal(writes(h).length,0);
    channels[25].enabled = false;
    h.overrides['/api/channels/channel-25/preference'] = () => response({});
    await h.submit(filtersForm(channels));
    assert.equal(writes(h).length,1); assert.equal(writes(h)[0].url,'/api/channels/channel-25/preference');
  }
  // Changing only a kind does not rewrite any channel preferences.
  {
    const h = harness(); await settle();
    h.overrides['/api/settings'] = options => options.method === 'PUT' ? response({}) : response({preferences:{disabled_kinds:['event']},connection:{state:'connected'}});
    await h.submit(filtersForm([{id:'fixture-channel',enabled:true}],['event']));
    assert.equal(writes(h).length,1); assert.equal(writes(h)[0].url,'/api/settings');
    assert.deepEqual(JSON.parse(writes(h)[0].options.body).disabled_kinds,['event']);
  }
  // A retry after a partially saved preference list sends only remaining changes.
  {
    const original = [{id:'a',name:'A',enabled:true},{id:'b',name:'B',enabled:true}];
    const h = harness({overrides:{'/api/channels':() => response(original.map(ch => ({...ch})))}}); await settle();
    h.overrides['/api/channels/a/preference'] = () => response({});
    h.overrides['/api/channels/b/preference'] = () => response({detail:'Temporary failure'},503);
    const form = filtersForm(original.map(ch => ({...ch,enabled:false})));
    await h.submit(form); assert.equal(writes(h).length,2);
    h.overrides['/api/channels/b/preference'] = () => response({}); await h.submit(form);
    assert.equal(writes(h).length,3); assert.equal(writes(h)[2].url,'/api/channels/b/preference');
  }
  // A committed task followed by read failure is reported as saved, without duplicate POST.
  {
    const h = harness(); await settle();
    const form = {id:'quick-form',elements:{title:{value:'Save once'}}}; h.elements['quick-form'] = form;
    h.overrides['/api/tasks'] = () => response({id:'saved'},201);
    h.overrides['/api/today'] = () => response({detail:'Read failed'},503);
    await h.submit(form);
    assert.match(h.elements.toast.textContent, /저장됐지만/); assert.equal(form.elements.title.value,'');
    assert.equal(writes(h).length,1);
    delete h.overrides['/api/today']; await h.click({action:'refresh'}); assert.equal(writes(h).length,1);
  }
  // A failed task save retains the draft so the user can correct it.
  {
    const h = harness(); await settle();
    const form = {id:'quick-form',elements:{title:{value:'Keep draft'}}}; h.elements['quick-form'] = form;
    h.overrides['/api/tasks'] = () => response({detail:'Validation failed'},422);
    await h.submit(form); assert.equal(form.elements.title.value,'Keep draft'); assert.match(h.elements.toast.textContent,/Validation failed/);
  }
  // Backend partial/busy collection states are not presented as full completion.
  for (const ingestion of [{state:'partial',partial:true,channels_scanned:12,channels_total:63,messages_failed:2},{state:'busy'}]) {
    const h = harness(); await settle();
    h.overrides['/api/teams/sync'] = () => response({state:'connected',ingestion}); await h.click({action:'sync-teams'});
    assert.match(h.elements['sync-status'].textContent, ingestion.state === 'busy' ? /이미 Teams 공지를 확인/ : /12\/63개 채널/);
    assert.doesNotMatch(h.elements['sync-status'].textContent,/권한과 일정 자료를 확인했어요/);
  }
  // A late collection response cannot restore a logged-out session or its toast.
  {
    const pending = deferred(), h = harness({ignoreAbort:new Set(['/api/teams/sync'])}); await settle();
    h.overrides['/api/teams/sync'] = () => pending.promise; const sync = h.click({action:'sync-teams'}); await settle();
    await h.click({action:'logout'}); pending.resolve(response({state:'connected'})); await sync;
    assert.match(h.elements.app.innerHTML,/school-signin/); assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
  }
  // While permissions are being rebuilt, queue month changes and retain the old view.
  {
    const pending = deferred(), h = harness(); await settle(); await h.click({action:'calendar'});
    h.overrides['/api/teams/sync'] = () => pending.promise;
    const sync = h.click({action:'sync-teams'}); await settle(); const before = h.requests.length;
    await h.click({action:'next-month'}); await h.click({action:'next-month'}); await h.click({action:'refresh'});
    assert.equal(h.requests.length,before); assert.match(h.elements.app.innerHTML,/2026년 11월/); assert.match(h.elements.app.innerHTML,/Fixture assignment/);
    pending.resolve(response({state:'connected'})); await sync;
    assert.match(h.elements.app.innerHTML,/2027년 1월/);
    assert.ok(h.requests.some(r => r.url === calendarUrl(h,'2027-01')));
  }
  // A busy lease does not replace private data with temporary empty access results.
  {
    const h = harness(); await settle(); const before = h.requests.length;
    h.overrides['/api/teams/sync'] = () => response({state:'checking',ingestion:{state:'busy'}});
    await h.click({action:'sync-teams'}); assert.equal(h.requests.length,before+1); assert.match(h.elements.app.innerHTML,/Fixture assignment/);
    h.overrides['/api/settings'] = () => response({connection:{state:'checking'}});
    await h.click({action:'calendar'}); await h.click({action:'next-month'});
    assert.equal(h.requests.length,before+2); assert.match(h.elements.app.innerHTML,/2026년 11월/);
    await h.click({action:'refresh'}); assert.equal(h.requests.length,before+3); assert.match(h.elements.app.innerHTML,/Fixture assignment/);
  }
  // Sync completion waits for a pending save before obtaining the replacement view.
  {
    const pendingSync = deferred(), pendingSave = deferred(), h = harness(); await settle();
    h.overrides['/api/teams/sync'] = () => pendingSync.promise;
    h.overrides['/api/tasks'] = () => pendingSave.promise;
    const sync = h.click({action:'sync-teams'}); await settle();
    const save = h.submit({id:'quick-form',elements:{title:{value:'Concurrent save'}}}); await settle();
    const before = h.requests.filter(r => r.url === '/api/today').length;
    pendingSync.resolve(response({state:'connected'})); await settle();
    assert.equal(h.requests.filter(r => r.url === '/api/today').length,before);
    pendingSave.resolve(response({id:'saved'},201)); await save; await sync;
    assert.equal(h.requests.filter(r => r.url === '/api/today').length,before+1);
    assert.equal(writes(h).filter(r => r.url === '/api/tasks').length,1);
  }
  // Review decisions retain their rendered ID when background data changes order.
  {
    const a={id:'change-a',before:{title:'A',start_date:'2026-11-07',end_date:'2026-11-07'},after:{start_date:'2026-11-08',end_date:'2026-11-08'}}, b={...a,id:'change-b',before:{...a.before,title:'B'}};
    const h = harness({overrides:{'/api/changes':() => response({items:[a,b]})}}); await settle();
    await h.click({action:'changes'}); assert.match(h.elements.modal.innerHTML,/data-change-id="change-a"/);
    h.overrides['/api/changes'] = () => response({items:[b,a]}); h.overrides['/api/teams/sync'] = () => response({state:'connected'});
    await h.click({action:'sync-teams'}); assert.equal(h.elements.modal.open,true);
    h.overrides['/api/changes/change-a'] = () => response({});
    await h.click({changeId:'change-a',changeDecision:'accept'});
    assert.equal(writes(h).at(-1).url,'/api/changes/change-a');
  }
  // A hung save is bounded too, preserves its draft, and warns against duplicate writes.
  {
    const pending = deferred(), h = harness(); await settle();
    const form={id:'quick-form',elements:{title:{value:'Uncertain task'}}}; h.elements['quick-form']=form;
    h.overrides['/api/tasks'] = () => pending.promise;
    const save=h.submit(form); await settle(); await h.advance(90001); await save;
    assert.match(h.elements.toast.textContent,/서버에서는 계속 처리 중/); assert.equal(form.elements.title.value,'Uncertain task'); assert.equal(writes(h).length,1);
    await h.click({action:'calendar'}); assert.match(h.elements.app.innerHTML,/calendar-grid/);
  }
  // A save starting after sync's reads began invalidates that pre-save snapshot.
  {
    const oldToday = deferred(), h = harness(); await settle();
    const staleToday = {...h.today,personal:[]}; let reads=0;
    h.overrides['/api/today'] = () => ++reads === 1 ? oldToday.promise : response(h.today);
    h.overrides['/api/teams/sync'] = () => response({state:'connected'});
    h.overrides['/api/tasks'] = () => {h.today.personal=[{...h.item,id:'new-task',source:'personal',title:'Saved during refresh'}]; return response({id:'new-task'},201);};
    const sync=h.click({action:'sync-teams'}); await settle();
    await h.submit({id:'quick-form',elements:{title:{value:'Saved during refresh'}}});
    const historyStart=h.history.length;
    oldToday.resolve(response(staleToday)); await sync;
    assert.equal(reads,2); assert.equal(writes(h).filter(r => r.url === '/api/tasks').length,1);
    assert.match(h.elements.app.innerHTML,/Saved during refresh/);
    assert.ok(h.history.slice(historyStart).filter(change => change.id==='app').every(change => change.html.includes('Saved during refresh')));
  }
  // Queued shared ingestion displays the committed catalog without claiming new posts are ready.
  {
    const h = harness({overrides:{'/api/service':() => response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true})}}); await settle();
    h.elements.modal.open=true; h.elements.modal.innerHTML='<textarea>Queued collection draft</textarea>';
    h.elements['quick-form']={elements:{title:{value:'Keep this quick draft'}}};
    h.overrides['/api/teams/sync']=() => response({state:'connected',ingestion:{state:'queued'}});
    const before=h.requests.filter(r => r.url === '/api/today').length;
    await h.click({action:'sync-teams'});
    assert.equal(h.requests.filter(r => r.url === '/api/today').length,before+1);
    assert.match(h.elements.app.innerHTML,/Fixture assignment/);
    assert.match(h.elements['sync-status'].textContent,/저장된 일정을 표시/);
    assert.match(h.elements['sync-status'].textContent,/새 공지는 백그라운드에서 확인 중/);
    assert.doesNotMatch(h.elements['sync-status'].textContent,/권한과 일정 자료를 확인했어요/);
    assert.equal(h.elements['sync-button'].disabled,false);
    assert.equal(h.elements.modal.open,true); assert.equal(h.elements.modal.innerHTML,'<textarea>Queued collection draft</textarea>');
    assert.match(h.elements.app.innerHTML,/value="Keep this quick draft"/);
    const requestCount=h.requests.length;
    await h.advance(60000);
    assert.equal(h.requests.length,requestCount); assert.equal(writes(h).length,1);
    assert.match(h.elements['sync-status'].textContent,/백그라운드에서 확인 중/);
  }
  // A shared server's initial checking lease is retried without losing a draft.
  {
    let connected=false;
    const h=harness({overrides:{
      '/api/service':() => response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),
      '/api/settings':() => response({preferences:{disabled_kinds:[]},connection:{state:connected?'connected':'checking'}})
    }}); await settle();
    h.elements.modal.open=true; h.elements.modal.innerHTML='<textarea>Retry draft</textarea>';
    h.elements['quick-form']={elements:{title:{value:'Keep retry draft'}}};
    const before=h.requests.filter(r => r.url==='/api/today').length;
    await h.advance(15000); assert.equal(h.requests.filter(r => r.url==='/api/today').length,before+1);
    connected=true;
    h.overrides['/api/today']=() => response({...h.today,teams:[{...h.item,title:'Recovered authorized event'}]});
    await h.advance(15000);
    assert.match(h.elements.app.innerHTML,/Recovered authorized event/);
    assert.equal(h.elements.modal.open,true); assert.equal(h.elements.modal.innerHTML,'<textarea>Retry draft</textarea>');
    assert.match(h.elements.app.innerHTML,/value="Keep retry draft"/);
    const completed=h.requests.length; await h.advance(60000); assert.equal(h.requests.length,completed);
    assert.equal(writes(h).length,0);
  }
  // Logout cancels an outstanding checking retry before any further account read.
  {
    const h=harness({overrides:{
      '/api/service':() => response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),
      '/api/settings':() => response({preferences:{disabled_kinds:[]},connection:{state:'checking'}})
    }}); await settle(); await h.click({action:'logout'});
    const before=h.requests.length; await h.advance(60000);
    assert.equal(h.requests.length,before); assert.match(h.elements.app.innerHTML,/school-signin/);
  }
  // Session expiry during a retry immediately clears private state and cancels future retries.
  {
    const h=harness({overrides:{
      '/api/service':() => response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),
      '/api/settings':() => response({preferences:{disabled_kinds:[]},connection:{state:'checking'}})
    }}); await settle(); h.overrides['/api/today']=() => response({},401);
    await h.advance(15000); assert.match(h.elements.app.innerHTML,/school-signin/);
    const before=h.requests.length; await h.advance(60000); assert.equal(h.requests.length,before);
  }
  // Retry traffic has a finite five-minute budget even when every read still says checking.
  {
    const h=harness({overrides:{
      '/api/service':() => response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),
      '/api/settings':() => response({preferences:{disabled_kinds:[]},connection:{state:'checking'}})
    }}); await settle(); await h.advance(315000);
    const reads=h.requests.filter(r => r.url==='/api/today').length;
    assert.ok(reads>1 && reads<=21);
    const before=h.requests.length; await h.advance(60000); assert.equal(h.requests.length,before);
    assert.equal(writes(h).length,0); assert.match(h.elements.app.innerHTML,/Teams 접근 권한을 확인하고/);
  }
  // A failed checking read retains existing data and stops automatic attempts.
  {
    const h=harness({overrides:{
      '/api/service':() => response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),
      '/api/settings':() => response({preferences:{disabled_kinds:[]},connection:{state:'checking'}})
    }}); await settle(); h.overrides['/api/today']=() => response({detail:'Temporary failure'},503);
    await h.advance(15000); assert.match(h.elements.app.innerHTML,/Fixture assignment/);
    const before=h.requests.length; await h.advance(60000); assert.equal(h.requests.length,before);
    assert.equal(writes(h).length,0);
  }
  // The checking timer does not overlap a read already started by the user.
  {
    let connected=false;
    const h=harness({overrides:{
      '/api/service':() => response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),
      '/api/settings':() => response({preferences:{disabled_kinds:[]},connection:{state:connected?'connected':'checking'}})
    }}); await settle();
    const pending=deferred(); h.overrides['/api/today']=() => pending.promise;
    const refresh=h.click({action:'refresh'}); await settle(); const before=h.requests.length;
    await h.advance(15000); assert.equal(h.requests.length,before);
    connected=true; pending.resolve(response(h.today)); await refresh;
    const completed=h.requests.length; await h.advance(60000); assert.equal(h.requests.length,completed);
  }
  // A later independent checking episode gets a fresh budget after a successful recovery.
  {
    let connected=false;
    const h=harness({overrides:{
      '/api/service':() => response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),
      '/api/settings':() => response({preferences:{disabled_kinds:[]},connection:{state:connected?'connected':'checking'}})
    }}); await settle();
    connected=true; await h.advance(15000);
    const firstRecovery=h.requests.length; await h.advance(300001);
    assert.equal(h.requests.length,firstRecovery);
    connected=false; await h.click({action:'refresh'});
    const before=h.requests.filter(r => r.url==='/api/today').length;
    connected=true;
    h.overrides['/api/today']=() => response({...h.today,teams:[{...h.item,title:'Second episode recovered'}]});
    await h.advance(15000);
    assert.equal(h.requests.filter(r => r.url==='/api/today').length,before+1);
    assert.match(h.elements.app.innerHTML,/Second episode recovered/);
    const secondRecovery=h.requests.length; await h.advance(60000);
    assert.equal(h.requests.length,secondRecovery); assert.equal(writes(h).length,0);
  }
  // An explicit refresh leaves the current screen and unsaved dialog available until replacement arrives.
  {
    const h=harness(); await settle(); const pending=deferred();
    h.elements.modal.open=true; h.elements.modal.innerHTML='<textarea>Refresh draft</textarea>';
    h.elements['quick-form']={elements:{title:{value:'Keep refresh input'}}};
    h.overrides['/api/today']=() => pending.promise;
    const refresh=h.click({action:'refresh'}); await settle();
    assert.match(h.elements.app.innerHTML,/Fixture assignment/); assert.doesNotMatch(h.elements.app.innerHTML,/data-load-state/);
    assert.equal(h.elements['refresh-button'].disabled,true); assert.match(h.elements['refresh-status'].textContent,/최신 일정을 확인/);
    const before=h.requests.length; await h.click({action:'refresh'}); assert.equal(h.requests.length,before);
    await h.click({action:'calendar'}); assert.match(h.elements.app.innerHTML,/calendar-grid/);
    pending.resolve(response({...h.today,teams:[{...h.item,title:'Refreshed assignment'}]})); await refresh;
    await h.click({action:'today'}); assert.match(h.elements.app.innerHTML,/Refreshed assignment/);
    assert.equal(h.elements.modal.open,true); assert.equal(h.elements.modal.innerHTML,'<textarea>Refresh draft</textarea>');
    assert.match(h.elements.app.innerHTML,/value="Keep refresh input"/); assert.equal(h.elements['refresh-button'].disabled,false);
  }
  // A transient refresh failure keeps the old snapshot and a persistent retry explanation.
  {
    const h=harness(); await settle(); h.overrides['/api/today']=() => response({detail:'Temporary unavailable'},503);
    await h.click({action:'refresh'}); assert.match(h.elements.app.innerHTML,/Fixture assignment/);
    assert.equal(h.elements['refresh-button'].disabled,false); assert.match(h.elements['refresh-status'].textContent,/마지막으로 확인한 일정/);
    await h.advance(10000); assert.match(h.elements['refresh-status'].textContent,/다시 확인해 주세요/);
    delete h.overrides['/api/today']; await h.click({action:'refresh'}); assert.equal(h.elements['refresh-status'].textContent,'');
  }
  // Authorization failures clear the preserved view immediately, even if the error body stalls.
  for (const status of [401,403]) {
    const h=harness(); await settle(); let bodyReads=0;
    h.elements.modal.open=true; h.elements.modal.innerHTML='Private detail';
    h.overrides['/api/today']=() => ({status,ok:false,json(){bodyReads++; return new Promise(() => {});}});
    await h.click({action:'refresh'});
    assert.equal(bodyReads,0); assert.match(h.elements.app.innerHTML,/school-signin/);
    assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/); assert.equal(h.elements.modal.open,false);
  }
  // A valid empty response replaces old events; preservation never invents authority for old data.
  {
    const h=harness(); await settle();
    h.overrides['/api/today']=() => response({...h.today,teams:[],personal:[]});
    h.overrides['/api/calendar']=() => response({items:[]});
    h.overrides['/api/settings']=() => response({preferences:{disabled_kinds:[]},connection:{state:'verification_failed'}});
    await h.click({action:'refresh'}); assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
  }
  // Direct event IDs are encoded exactly once and hiding sends only one PATCH.
  {
    const h=harness(); await settle(); h.item.id='direct:fixture-digest';
    const route='/api/events/direct%3Afixture-digest/state';
    h.overrides[route]=options => {assert.deepEqual(JSON.parse(options.body),{hidden:true}); h.today.teams=[]; return response({...h.item,hidden:true});};
    h.overrides['/api/calendar']=() => response({items:[]});
    await h.click({detail:h.item.id,source:'teams'}); await h.click({action:'hide-detail'});
    assert.equal(writes(h).length,1); assert.equal(writes(h)[0].url,route); assert.equal(writes(h)[0].options.method,'PATCH');
    assert.match(h.elements.toast.textContent,/숨겼어요/); assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    assert.equal(h.elements['refresh-button'].disabled,false);
  }
  // A truly missing event refreshes the list and explains the issue without retrying the write.
  {
    const h=harness(); await settle(); const before=h.requests.filter(r => r.url==='/api/today').length;
    h.overrides['/api/events/fixture/state']=() => response({detail:'Item not found'},404);
    await h.click({detail:'fixture',source:'teams'}); await h.click({action:'hide-detail'});
    assert.equal(writes(h).length,1); assert.equal(h.requests.filter(r => r.url==='/api/today').length,before+1);
    assert.match(h.elements.toast.textContent,/최신 목록에서 다시 선택/); assert.doesNotMatch(h.elements.toast.textContent,/Item not found|숨겼어요/);
    assert.equal(h.elements.modal.open,false);
  }
  // Hidden events follow the selected calendar year, so future events remain restorable.
  {
    const h=harness(); await settle(); await h.click({action:'calendar'});
    await h.click({action:'next-month'}); await h.click({action:'next-month'});
    assert.match(h.elements.app.innerHTML,/class="month-actions"[^]*data-action="hidden"/);
    const future={...h.item,id:'future-task',source:'personal',title:'Hidden next year',start_date:'2027-01-02',end_date:'2027-01-02',version:1};
    h.overrides['/api/hidden?start=2027-01-01&end=2027-12-31']=() => response({items:[future]});
    h.overrides['/api/tasks/future-task/state']=options => {assert.deepEqual(JSON.parse(options.body),{hidden:false,version:1}); return response({...future,hidden:false});};
    await h.click({action:'hidden'}); assert.match(h.elements.modal.innerHTML,/숨긴 일정 · 2027년/); assert.match(h.elements.modal.innerHTML,/Hidden next year/);
    await h.click({restore:'0'}); assert.equal(writes(h).at(-1).url,'/api/tasks/future-task/state');
    await h.click({action:'today'}); h.overrides['/api/hidden?start=2026-01-01&end=2026-12-31']=() => response({items:[]});
    await h.click({action:'hidden'}); assert.match(h.elements.modal.innerHTML,/숨긴 일정 · 2026년/);
  }
  // A typed permission-pending response on first entry gets bounded read retries and recovers.
  {
    const h=harness({overrides:{
      '/api/service':() => response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),
      '/api/today':() => response({detail:'Permission checking',code:'permission_check_pending'},503)
    }}); await settle(); assert.match(h.elements.app.innerHTML,/Teams 권한 확인을 기다리고/);
    await h.advance(15000); assert.match(h.elements.app.innerHTML,/Teams 권한 확인을 기다리고/);
    delete h.overrides['/api/today']; await h.advance(15000);
    assert.match(h.elements.app.innerHTML,/Fixture assignment/); assert.equal(writes(h).length,0);
  }
  // A permission-pending hide keeps the dialog and retries reads, never the user's PATCH.
  {
    const h=harness({overrides:{'/api/service':() => response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true})}}); await settle();
    h.overrides['/api/events/fixture/state']=() => response({detail:'Permission checking',code:'permission_check_pending'},503);
    await h.click({detail:'fixture',source:'teams'}); const dialog=h.elements.modal.innerHTML;
    await h.click({action:'hide-detail'}); assert.equal(h.elements.modal.innerHTML,dialog); assert.equal(h.elements.modal.open,true);
    assert.match(h.elements['refresh-status'].textContent,/Teams 접근 권한을 확인/);
    await h.advance(15000); assert.equal(writes(h).length,1); assert.match(h.elements.app.innerHTML,/Fixture assignment/);
    assert.doesNotMatch(h.elements.toast.textContent,/숨겼어요/);
  }
  // Repeated typed pending responses cannot reset the same five-minute retry budget.
  {
    const h=harness({overrides:{
      '/api/service':() => response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),
      '/api/today':() => response({code:'permission_check_pending'},503)
    }}); await settle(); await h.advance(315000);
    const reads=h.requests.filter(r => r.url==='/api/today').length; assert.ok(reads>1 && reads<=21);
    const before=h.requests.length; await h.advance(60000); assert.equal(h.requests.length,before); assert.equal(writes(h).length,0);
  }
  // A slow write keeps close/sign-out controls usable; signing out cancels its private response.
  {
    const pending=deferred(), h=harness({ignoreAbort:new Set(['/api/tasks'])}); await settle();
    const escapes=['close','logout','disconnect-device','logout-all'].map(action => ({disabled:false,dataset:{action}}));
    const input={disabled:false,dataset:{}}, submit={disabled:false,dataset:{action:'save'}};
    h.controls.push(...escapes,input,submit);
    h.elements.modal.open=true; h.elements.modal.innerHTML='Private pending draft';
    h.overrides['/api/tasks']=() => pending.promise;
    const save=h.submit({id:'quick-form',elements:{title:{value:'Pending personal task'}}}); await settle();
    assert.ok(escapes.every(control => !control.disabled)); assert.equal(input.disabled,true); assert.equal(submit.disabled,true);
    await h.click({action:'close'}); assert.equal(h.elements.modal.open,false);
    await h.click({action:'logout'}); await save;
    assert.match(h.elements.app.innerHTML,/school-signin/); assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    const before=h.requests.length; pending.resolve(response({id:'late-task'},201)); await settle(); await h.advance(90001);
    assert.equal(h.requests.length,before); assert.equal(writes(h).filter(r => r.url==='/api/tasks').length,1);
  }
  // Resuming a tab after access is forbidden removes the previously visible private screen.
  {
    const h=harness(); await settle(); h.elements.modal.open=true; h.elements.modal.innerHTML='Private tab draft';
    h.overrides['/api/me']=() => response({},403);
    h.visibility(true); h.visibility(false); await settle();
    assert.match(h.elements.app.innerHTML,/school-signin/); assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    assert.equal(h.elements.modal.open,false); assert.equal(h.elements['account-name'].textContent,'');
  }
  console.log('Usability UI: 41 synthetic scenarios passed (state-preserving refresh, permission-pending recovery, fail-closed auth, usable sign-out during writes, encoded hide IDs, selected-year restore and prior sync/race regressions).');
})().catch(error => {console.error(error); process.exitCode = 1;});
function abortable(value, signal) {
  if (!signal) return Promise.resolve(value);
  return new Promise((resolve, reject) => {
    const abort = () => reject(new DOMException('The operation was aborted.', 'AbortError'));
    if (signal.aborted) return abort();
    signal.addEventListener('abort', abort, {once: true});
    Promise.resolve(value).then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
  });
}
function harness({overrides = {}, ignoreAbort = new Set(), ignoreBodyAbort = new Set()} = {}) {
  const listeners = {}, requests = [], history = [], controls = [], timers = new Map();
  let timerId = 0, now = 0;
  const epoch = Date.UTC(2026, 10, 7, 0, 0);
  class ClockDate extends Date {
    constructor(...args) {super(...(args.length ? args : [epoch + now]));}
    static now() {return epoch + now;}
  }
  const elements = Object.fromEntries(['app', 'modal', 'topbar', 'account-name', 'nav-today', 'nav-calendar', 'toast', 'login-error', 'sync-status', 'sync-button', 'refresh-status', 'refresh-button'].map(id => {
    let html = '';
    return [id, {textContent: '', hidden: true, dataset: {}, attributes: {},
      get innerHTML() {return html;},
      set innerHTML(value) {html = value; history.push({id, html: value});},
      classList: {toggle() {}, add() {}, remove() {}},
      setAttribute(name, value) {this.attributes[name] = value;},
      removeAttribute(name) {delete this.attributes[name];},
      replaceChildren() {this.innerHTML = '';}}];
  }));
  elements.modal.close = function () {this.open = false;};
  elements.modal.showModal = function () {this.open = true;};
  const item = {id: 'fixture', source: 'teams', title: 'Fixture assignment', notes: 'Fixture notes', channel: 'Fixture channel',
    start_date: '2026-11-07', end_date: '2026-11-07', time: null, kind: 'assignment', completed: false, cancelled: false};
  const today = {date: '2026-11-07', teams: [item], personal: []};
  const context = {window: {}, location: {protocol: 'https:', search: ''}, URL, console, Intl, Date: ClockDate, Set, Map,
    AbortController, DOMException,
    setTimeout(callback, delay = 0) {const id = ++timerId; timers.set(id, {callback, at: now + Number(delay)}); return id;},
    clearTimeout(id) {timers.delete(id);},
    document: {hidden: false, documentElement: {dataset: {mode: 'school'}},
      querySelector(selector) {return selector === '[data-action="sync-teams"]' ? elements['sync-button'] : selector === '[data-action="refresh"]' ? elements['refresh-button'] : elements[selector.slice(1)] || null;},
      querySelectorAll(selector) {return selector === '[data-locked]' ? controls.filter(control => control.dataset.locked) : selector === 'button,input,select,textarea' ? controls : [];},
      addEventListener(type, handler) {listeners[type] = handler;}},
    async fetch(url, options = {}) {
      requests.push({url, options});
      let result;
      if (overrides[url]) result = overrides[url](options);
      else if (url.startsWith('/api/calendar?') && overrides['/api/calendar']) result = overrides['/api/calendar'](options, url);
      else if (url === '/api/service') result = response({mode: 'test-server', idle_seconds: 1800});
      else if (url === '/api/me') result = response({id: 'fixture-user', name: 'Fixture student', csrf_token: 'fixture-csrf'});
      else if (url === '/api/today') result = response(today);
      else if (url.startsWith('/api/calendar?')) result = response({items: [item]});
      else if (url === '/api/channels') result = response([{id: 'fixture-channel', name: 'Fixture channel', enabled: true}]);
      else if (url === '/api/settings') result = response({preferences: {disabled_kinds: [], morning_enabled: true, morning_time: '07:00'}, connection: {state: 'connected'}});
      else if (url === '/api/changes') result = response({items: []});
      else if (url === '/auth/logout') result = response(null, 204);
      else throw new Error('Unexpected fixture request: ' + url);
      const received = await (ignoreAbort.has(url) ? result : abortable(result, options.signal));
      return {...received, json: () => ignoreAbort.has(url) || ignoreBodyAbort.has(url) ? received.json() : abortable(received.json(), options.signal)};
    }};
  vm.createContext(context);
  vm.runInContext(source('beta-calendar.js'), context);
  vm.runInContext(source('beta.js'), context);
  return {elements, requests, history, controls, overrides, item, today, context,
    click(data) {return listeners.click({preventDefault() {}, target: {closest() {return {dataset: data, disabled: false};}}});},
    submit(form) {return listeners.submit({preventDefault() {}, target: form});},
    visibility(hidden) {context.document.hidden = hidden; return listeners.visibilitychange();},
    activity(type = 'pointerdown') {return listeners[type]({});},
    // Simulates a suspended/throttled browser where clock time passes before timers run.
    jump(milliseconds) {now += milliseconds;},
    async advance(milliseconds) {
      const until = now + milliseconds;
      for (let guard = 0; guard < 1000; guard++) {
        const due = [...timers.entries()].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) {now = until; await settle(); return;}
        const [id, timer] = due; timers.delete(id); now = timer.at; timer.callback(); await settle();
      }
      throw new Error('Fixture timer loop exceeded: possible automatic retry loop');
    }};
}
