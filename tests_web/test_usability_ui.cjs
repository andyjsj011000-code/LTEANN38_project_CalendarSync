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
const personalTask = (values={}) => ({id:'personal-ready',source:'personal',title:'Personal first result',start_date:'2026-11-07',end_date:'2026-11-07',time:null,notes:'',kind:'personal',display_mode:'daily',selected_dates:[],completed:false,hidden:false,version:1,...values});
const personalService = () => response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true,personal_agenda:true});
const accessSettings = (verified, retryable = !verified, state = verified ? 'connected' : 'source_unavailable') => response({preferences:{disabled_kinds:[]},connection:{state,teams_verified:verified,retryable}});
const unverifiedAccess = (retryable = true, state = 'source_unavailable') => response({detail:'Teams access must be checked again',code:'permission_check_pending',teams_access_unverified:true,retryable,connection_state:state},503);
const writes = h => h.requests.filter(r => ['POST','PUT','PATCH','DELETE'].includes(r.options.method));
const calendarUrl = (h, month) => {const grid = h.context.window.BetaCalendar.monthGrid(month); return `/api/calendar?start=${grid.start}&end=${grid.end}`;};
const filtersForm = (channels, disabled = []) => ({id:'filters-form', querySelectorAll(selector) {
  return selector === '[name=channel]' ? channels.map(ch => ({value:ch.id, checked:ch.enabled})) : ['assignment','application','event'].map(value => ({value, checked:!disabled.includes(value)}));
}});

(async () => {
  // Channel search must preserve unsaved checkboxes, including rows outside the result.
  {
    const channels = [{id:'a',name:'물리 공지',enabled:true},{id:'b',name:'English ＡＢＣ',enabled:false},{id:'c',name:'물리 과제',enabled:true}];
    const h = harness({overrides:{'/api/channels':() => response(channels)}}); await settle();
    await h.click({action:'filters'});
    assert.match(h.elements.modal.innerHTML, /type="search"[^>]*data-channel-query/);
    const rows = channels.map(ch => ({textContent:ch.name,hidden:false,checkbox:{value:ch.id,checked:ch.enabled}}));
    const status = {textContent:''};
    const form = {id:'filters-form',querySelectorAll:selector => selector === '[data-channel-row]' ? rows : selector === '[name=channel]' ? rows.map(row => row.checkbox) : ['assignment','application','event'].map(value => ({value,checked:true})),querySelector:() => status};
    const field = {dataset:{channelQuery:''},value:'물리 공지',closest:() => form};
    const before = h.requests.length;
    h.input(field);
    assert.deepEqual(rows.map(row => row.hidden),[false,true,true]);
    rows[0].checkbox.checked = false;
    field.value = ' abc '; h.input(field);
    assert.deepEqual(rows.map(row => row.hidden),[true,false,true]);
    field.value = '없는 채널'; h.input(field);
    assert.deepEqual(rows.map(row => row.hidden),[true,true,true]);
    assert.match(status.textContent,/검색 결과가 없어요/);
    field.value = '  '; h.input(field);
    assert.deepEqual(rows.map(row => row.hidden),[false,false,false]);
    assert.equal(rows[0].checkbox.checked,false);
    assert.equal(rows[1].checkbox.checked,false);
    assert.equal(h.requests.length,before);
    field.value = 'English'; h.input(field);
    h.overrides['/api/channels/a/preference'] = () => response({});
    await h.submit(form);
    assert.equal(writes(h).length,1);
    assert.equal(writes(h)[0].url,'/api/channels/a/preference');
    assert.equal(JSON.parse(writes(h)[0].options.body).enabled,false);
  }
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
  // Hide stays pending until its write is acknowledged, then disappears before the refresh finishes.
  {
    const h=harness(); await settle(); h.item.end_date='2026-11-09';
    const write=deferred(), read=deferred(); h.overrides['/api/events/fixture/state']=() => write.promise;
    h.overrides['/api/today']=() => read.promise; h.overrides['/api/calendar']=() => response({items:[]});
    await h.click({detail:'fixture',source:'teams'}); const operation=h.click({action:'hide-detail'}); await settle(); await h.advance(10000);
    assert.match(h.elements.app.innerHTML,/Fixture assignment/); assert.equal(h.elements.modal.open,true);
    assert.equal(h.elements['mutation-status'].hidden,false); assert.match(h.elements['mutation-status'].textContent,/숨기는 중/);
    assert.doesNotMatch(h.elements.toast.textContent,/숨겼어요/);
    write.resolve(response({...h.item,hidden:true})); await settle();
    assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/); assert.equal(h.elements.modal.open,false); assert.match(h.elements.toast.textContent,/숨겼어요/);
    await h.click({action:'calendar'}); assert.match(h.elements.app.innerHTML,/calendar-grid/); assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    read.resolve(response({...h.today,teams:[]})); await operation;
    assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/); assert.equal(writes(h).length,1);
  }
  // A confirmed hide remains applied if its follow-up read fails.
  {
    const h=harness(); await settle();
    h.overrides['/api/events/fixture/state']=() => response({...h.item,hidden:true});
    h.overrides['/api/today']=() => response({detail:'Read unavailable'},503);
    await h.click({detail:'fixture',source:'teams'}); await h.click({action:'hide-detail'});
    assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/); assert.match(h.elements.toast.textContent,/저장됐지만/);
    assert.equal(h.elements.modal.open,false); assert.equal(writes(h).length,1);
  }
  // A response from before the confirmed hide cannot resurrect it even if transport ignores abort.
  {
    const oldRead=deferred(), newRead=deferred(), h=harness({ignoreAbort:new Set(['/api/today'])}); await settle(); let reads=0;
    h.overrides['/api/today']=() => ++reads===1?oldRead.promise:newRead.promise;
    h.overrides['/api/calendar']=() => response({items:[]});
    const refresh=h.click({action:'refresh'}); await settle();
    h.overrides['/api/events/fixture/state']=() => response({...h.item,hidden:true});
    await h.click({detail:'fixture',source:'teams'}); const hide=h.click({action:'hide-detail'}); await settle();
    const afterSaved=h.history.length; oldRead.resolve(response(h.today)); await refresh; await settle();
    assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    assert.ok(h.history.slice(afterSaved).filter(change => change.id==='app').every(change => !change.html.includes('Fixture assignment')));
    newRead.resolve(response({...h.today,teams:[]})); await hide; assert.equal(writes(h).length,1);
  }
  // Restoring an eligible hidden item updates today's list before its follow-up read returns.
  {
    const h=harness({overrides:{'/api/today':() => response({date:'2026-11-07',teams:[],personal:[]}),'/api/calendar':() => response({items:[]})}}); await settle();
    const item={...h.item,channel_id:'fixture-channel',hidden:true}, write=deferred(), read=deferred();
    h.overrides['/api/hidden?start=2026-01-01&end=2026-12-31']=() => response({items:[item]});
    h.overrides['/api/events/fixture/state']=() => write.promise; h.overrides['/api/today']=() => read.promise;
    await h.click({action:'hidden'}); const restore=h.click({restore:'0'}); await settle();
    assert.match(h.elements['mutation-status'].textContent,/복원하는 중/); assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    write.resolve(response({...item,hidden:false})); await settle(); assert.match(h.elements.app.innerHTML,/Fixture assignment/);
    assert.match(h.elements.toast.textContent,/복원했어요/); assert.equal(h.elements.modal.open,false);
    read.resolve(response({...h.today,teams:[{...item,hidden:false}]})); await restore; assert.equal(writes(h).length,1);
  }
  // Restoration keeps disabled channels hidden rather than bypassing the user's display preferences.
  {
    const h=harness({overrides:{'/api/channels':() => response([{id:'fixture-channel',name:'Fixture',enabled:false}]),'/api/today':() => response({date:'2026-11-07',teams:[],personal:[]}),'/api/calendar':() => response({items:[]})}}); await settle();
    const item={...h.item,channel_id:'fixture-channel',hidden:true}; h.overrides['/api/hidden?start=2026-01-01&end=2026-12-31']=() => response({items:[item]});
    h.overrides['/api/events/fixture/state']=() => response({...item,hidden:false}); h.overrides['/api/today']=() => response({},503);
    await h.click({action:'hidden'}); await h.click({restore:'0'}); assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
  }
  // A permission-pending write leaves the item intact and explains the error inside its open dialog.
  {
    const h=harness(); await settle(); h.overrides['/api/events/fixture/state']=() => response({detail:'권한 확인 중이에요. 잠시 후 다시 시도해 주세요.',code:'permission_check_pending'},503);
    await h.click({detail:'fixture',source:'teams'}); await h.click({action:'hide-detail'}); await h.advance(10000);
    assert.equal(h.elements.modal.open,true); assert.equal(h.elements['mutation-status'].hidden,false);
    assert.match(h.elements['mutation-status'].textContent,/권한 확인 중/); assert.match(h.elements.app.innerHTML,/Fixture assignment/); assert.equal(writes(h).length,1);
  }
  // A late acknowledged hide cannot restore data after the user signs out.
  {
    const pending=deferred(), h=harness({ignoreAbort:new Set(['/api/events/fixture/state'])}); await settle();
    h.overrides['/api/events/fixture/state']=() => pending.promise;
    await h.click({detail:'fixture',source:'teams'}); const hide=h.click({action:'hide-detail'}); await settle();
    await h.click({action:'logout'}); pending.resolve(response({...h.item,hidden:true})); await hide;
    assert.match(h.elements.app.innerHTML,/school-signin/); assert.equal(h.elements.modal.open,false); assert.doesNotMatch(h.elements.toast.textContent,/숨겼어요/);
  }
  // A confirmed hide during collection preserves the user's queued calendar destination.
  {
    const pending=deferred(), h=harness(); await settle(); await h.click({action:'calendar'});
    h.overrides['/api/teams/sync']=() => pending.promise;
    const sync=h.click({action:'sync-teams'}); await settle(); await h.click({action:'next-month'});
    h.overrides['/api/events/fixture/state']=() => {h.today.teams=[]; return response({...h.item,hidden:true});};
    h.overrides['/api/calendar']=() => response({items:[]});
    await h.click({detail:'fixture',source:'teams'}); await h.click({action:'hide-detail'});
    assert.match(h.elements.app.innerHTML,/2026-12 일정을/); assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    pending.resolve(response({state:'connected'})); await sync;
    assert.match(h.elements.app.innerHTML,/2026년 12월/); assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    assert.equal(writes(h).filter(request => request.options.method==='PATCH').length,1);
  }
  // A slow hidden-list response cannot replace a newly opened personal-task draft.
  {
    const pending=deferred(), h=harness(); await settle();
    h.overrides['/api/hidden?start=2026-01-01&end=2026-12-31']=() => pending.promise;
    const hidden=h.click({action:'hidden'}); await settle(); await h.click({action:'new'});
    h.elements.modal.innerHTML+='<textarea>Keep new task draft</textarea>';
    pending.resolve(response({items:[]})); await hidden;
    assert.match(h.elements.modal.innerHTML,/Keep new task draft/); assert.doesNotMatch(h.elements.modal.innerHTML,/hidden-list/);
  }
  // An old reminder response cannot replace a different task's draft or change its eventual write target.
  {
    const task={id:'task-b',source:'personal',title:'Task B',start_date:'2026-11-07',end_date:'2026-11-07',display_mode:'daily',selected_dates:[],version:1,hidden:false,completed:false};
    const pending=deferred(), h=harness(); await settle(); h.today.personal=[task]; await h.click({action:'refresh'});
    h.overrides['/api/reminders/teams/fixture']=() => pending.promise;
    await h.click({detail:'fixture',source:'teams'}); const reminder=h.click({action:'reminders'}); await settle();
    await h.click({action:'close'}); await h.click({detail:'task-b',source:'personal'}); await h.click({action:'edit-detail'});
    h.elements.modal.innerHTML+='<textarea>Keep task B draft</textarea>';
    pending.resolve(response({offsets:[],custom_times:[]})); await reminder;
    assert.match(h.elements.modal.innerHTML,/Keep task B draft/); assert.doesNotMatch(h.elements.modal.innerHTML,/id="reminder-form"/);
    h.context.FormData=class {constructor(form){return new Map(Object.entries(form.values));}};
    h.overrides['/api/tasks/task-b']=() => response({...task,title:'Edited B',version:2});
    h.overrides['/api/today']=() => response({},503);
    await h.submit({id:'task-form',values:{title:'Edited B',notes:'',start_date:task.start_date,end_date:task.end_date,time:'',display_mode:'daily'}});
    assert.equal(writes(h).at(-1).url,'/api/tasks/task-b'); assert.equal(JSON.parse(writes(h).at(-1).options.body).version,1);
    assert.match(h.elements.app.innerHTML,/Edited B/);
  }
  // A slow notification panel cannot replace a newer feedback draft.
  {
    const pending=deferred(), h=harness(); await settle(); h.overrides['/api/push']=() => pending.promise;
    const notification=h.click({action:'notifications'}); await settle(); await h.click({action:'feedback'});
    h.elements.modal.innerHTML+='<textarea>Keep feedback draft</textarea>';
    pending.resolve(response({configured:false,registered:false,background_enabled:true})); await notification;
    assert.match(h.elements.modal.innerHTML,/Keep feedback draft/); assert.doesNotMatch(h.elements.modal.innerHTML,/id="notification-form"/);
  }
  // With two asynchronous panel requests, only the last requested panel opens.
  {
    const first=deferred(), last=deferred(), h=harness(); await settle();
    h.overrides['/api/hidden?start=2026-01-01&end=2026-12-31']=() => first.promise; h.overrides['/api/push']=() => last.promise;
    const hidden=h.click({action:'hidden'}), notification=h.click({action:'notifications'}); await settle();
    first.resolve(response({items:[]})); await hidden; assert.notEqual(h.elements.modal.open,true);
    last.resolve(response({configured:false,registered:false,background_enabled:true})); await notification;
    assert.match(h.elements.modal.innerHTML,/id="notification-form"/);
  }
  // Both an explicit close and Escape cancel a pending reminder dialog.
  for (const close of ['button','escape']) {
    const pending=deferred(), h=harness(); await settle(); h.overrides['/api/reminders/teams/fixture']=() => pending.promise;
    await h.click({detail:'fixture',source:'teams'}); const reminder=h.click({action:'reminders'}); await settle();
    if(close==='button') await h.click({action:'close'}); else h.escape();
    pending.resolve(response({offsets:[],custom_times:[]})); await reminder;
    assert.equal(h.elements.modal.open,false); assert.doesNotMatch(h.elements.modal.innerHTML,/id="reminder-form"/);
  }
  // Errors for the current dialog are visible and announced inside that dialog.
  {
    const h=harness(); await settle();
    h.overrides['/api/reminders/teams/fixture']=() => response({detail:'권한 확인이 진행 중이에요.',code:'permission_check_pending'},503);
    await h.click({detail:'fixture',source:'teams'}); await h.click({action:'reminders'});
    assert.equal(h.elements.modal.open,true); assert.equal(h.elements['mutation-status'].hidden,false);
    assert.equal(h.elements['mutation-status'].attributes.role,'alert'); assert.match(h.elements['mutation-status'].textContent,/권한 확인이 진행 중/);
    assert.equal(writes(h).length,0);
  }
  // An obsolete dialog's error cannot overwrite the current draft or its inline status.
  {
    const pending=deferred(), h=harness(); await settle(); h.overrides['/api/reminders/teams/fixture']=() => pending.promise;
    await h.click({detail:'fixture',source:'teams'}); const reminder=h.click({action:'reminders'}); await settle();
    await h.click({action:'feedback'}); h.elements.modal.innerHTML+='<textarea>New feedback draft</textarea>';
    const status=h.elements['mutation-status'].textContent;
    pending.resolve(response({detail:'Old panel failed'},503)); await reminder;
    assert.match(h.elements.modal.innerHTML,/New feedback draft/); assert.equal(h.elements['mutation-status'].textContent,status);
    assert.doesNotMatch(h.elements.toast.textContent,/Old panel failed/);
  }
  // Confirmed deletion removes only the chosen task before slow reads finish and frees navigation.
  {
    const task={id:'delete-task',source:'personal',title:'Delete this task',start_date:'2026-11-07',end_date:'2026-11-09',display_mode:'daily',selected_dates:[],version:1,hidden:false,completed:false};
    const h=harness(); await settle(); h.today.personal=[task]; await h.click({action:'refresh'});
    const write=deferred(), read=deferred(); h.overrides['/api/tasks/delete-task?version=1']=() => write.promise; h.overrides['/api/today']=() => read.promise;
    await h.click({detail:'delete-task',source:'personal'}); await h.click({action:'delete-detail'}); const deletion=h.click({action:'confirm-delete'}); await settle();
    assert.match(h.elements['mutation-status'].textContent,/삭제하는 중/); assert.match(h.elements.app.innerHTML,/Delete this task/);
    write.resolve(response(null,204)); await settle(); assert.doesNotMatch(h.elements.app.innerHTML,/Delete this task/); assert.match(h.elements.app.innerHTML,/Fixture assignment/);
    assert.match(h.elements.toast.textContent,/삭제했어요/); await h.click({action:'calendar'}); assert.match(h.elements.app.innerHTML,/calendar-grid/);
    read.resolve(response({...h.today,personal:[]})); await deletion; assert.equal(writes(h).length,1);
  }
  // A confirmed deletion stays removed if the post-delete read fails.
  {
    const task={id:'delete-task',source:'personal',title:'Deleted task stays gone',start_date:'2026-11-07',end_date:'2026-11-07',display_mode:'daily',selected_dates:[],version:1};
    const h=harness(); await settle(); h.today.personal=[task]; await h.click({action:'refresh'});
    h.overrides['/api/tasks/delete-task?version=1']=() => response(null,204); h.overrides['/api/today']=() => response({},503);
    await h.click({detail:task.id,source:'personal'}); await h.click({action:'delete-detail'}); await h.click({action:'confirm-delete'});
    assert.doesNotMatch(h.elements.app.innerHTML,/Deleted task stays gone/); assert.match(h.elements.toast.textContent,/저장됐지만/);
  }
  // A rejected delete retains the task and presents the conflict inside its confirmation dialog.
  {
    const task={id:'delete-task',source:'personal',title:'Keep conflicting task',start_date:'2026-11-07',end_date:'2026-11-07',display_mode:'daily',selected_dates:[],version:1};
    const h=harness(); await settle(); h.today.personal=[task]; await h.click({action:'refresh'});
    h.overrides['/api/tasks/delete-task?version=1']=() => response({},409);
    await h.click({detail:task.id,source:'personal'}); await h.click({action:'delete-detail'}); await h.click({action:'confirm-delete'});
    assert.match(h.elements.app.innerHTML,/Keep conflicting task/); assert.equal(h.elements.modal.open,true);
    assert.equal(h.elements['mutation-status'].attributes.role,'alert'); assert.equal(writes(h).length,1);
  }
  // Teams overrides expose and submit only the fields that the server actually applies.
  {
    const h=harness(); await settle(); await h.click({detail:'fixture',source:'teams'}); await h.click({action:'override-detail'});
    assert.doesNotMatch(h.elements.modal.innerHTML,/name="display_mode"|id="pick-date"/); assert.match(h.elements.modal.innerHTML,/name="title"/);
    h.context.FormData=class {constructor(form){return new Map(Object.entries(form.values));}};
    h.overrides['/api/events/fixture/override']=() => response({saved:true});
    await h.submit({id:'task-form',values:{title:'Changed title',notes:'Notes',start_date:'2026-11-07',end_date:'2026-11-08',time:'',display_mode:'final'}});
    const body=JSON.parse(writes(h).at(-1).options.body); assert.equal(body.title,'Changed title'); assert.equal(body.time,null);
    assert.ok(!('display_mode' in body)); assert.ok(!('selected_dates' in body));
    await h.click({action:'new'}); assert.match(h.elements.modal.innerHTML,/name="display_mode"/); assert.match(h.elements.modal.innerHTML,/id="pick-date"/);
  }
  // A newly created personal task is visible immediately after its confirmed write even if reads fail.
  {
    const h=harness(); await settle();
    h.overrides['/api/tasks']=() => response({id:'created-task',source:'personal',title:'Created without repeat',start_date:'2026-11-07',end_date:'2026-11-07',display_mode:'daily',selected_dates:[],version:1,completed:false,hidden:false},201);
    h.overrides['/api/today']=() => response({},503);
    await h.submit({id:'quick-form',elements:{title:{value:'Created without repeat'}}});
    assert.match(h.elements.app.innerHTML,/Created without repeat/); assert.equal(writes(h).length,1); assert.match(h.elements.toast.textContent,/저장됐지만/);
  }
  // A confirmed personal edit replaces its title/version locally and honors its updated date range.
  {
    const task={id:'edit-task',source:'personal',title:'Old today task',start_date:'2026-11-07',end_date:'2026-11-07',display_mode:'selected',selected_dates:['2026-11-07'],version:1,completed:false,hidden:false};
    const updated={...task,title:'Updated December task',start_date:'2026-12-01',end_date:'2026-12-03',display_mode:'daily',selected_dates:[],version:2};
    const h=harness(); await settle(); h.today.personal=[task]; await h.click({action:'refresh'});
    await h.click({detail:task.id,source:'personal'}); await h.click({action:'edit-detail'});
    assert.match(h.elements.modal.innerHTML,/name="display_mode"/);
    h.context.FormData=class {constructor(form){return new Map(Object.entries(form.values));}};
    h.overrides['/api/tasks/edit-task']=() => response(updated); h.overrides['/api/today']=() => response({},503);
    await h.submit({id:'task-form',values:{title:updated.title,notes:'',start_date:updated.start_date,end_date:updated.end_date,time:'',display_mode:'daily'}});
    assert.doesNotMatch(h.elements.app.innerHTML,/Old today task|Updated December task/);
    h.overrides['/api/calendar']=() => response({items:[updated]}); await h.click({action:'calendar'}); await h.click({action:'next-month'});
    assert.match(h.elements.app.innerHTML,/Updated December task/); assert.equal(writes(h).length,1);
  }
  // A fake denied push permission is announced inside its originating notification dialog.
  {
    const h=harness(); await settle(); let permissions=0;
    h.context.navigator={serviceWorker:{}}; h.context.window.PushManager=function(){};
    h.context.Notification={requestPermission(){permissions++; return Promise.resolve('denied');}};
    h.context.window.Notification=h.context.Notification;
    h.overrides['/api/push']=() => response({configured:true,registered:false,background_enabled:true});
    await h.click({action:'notifications'}); await h.click({action:'push-enable'});
    assert.equal(permissions,1); assert.equal(writes(h).length,0); assert.equal(h.elements.modal.open,true);
    assert.equal(h.elements['mutation-status'].attributes.role,'alert'); assert.match(h.elements['mutation-status'].textContent,/알림 권한이 허용되지/);
  }
  // A late fake permission denial from a closed dialog cannot contaminate a new task draft.
  {
    const permission=deferred(), h=harness(); await settle();
    h.context.navigator={serviceWorker:{}}; h.context.window.PushManager=function(){};
    h.context.Notification={requestPermission(){return permission.promise;}}; h.context.window.Notification=h.context.Notification;
    h.overrides['/api/push']=() => response({configured:true,registered:false,background_enabled:true});
    await h.click({action:'notifications'}); const enabling=h.click({action:'push-enable'}); await settle();
    await h.click({action:'close'}); await h.click({action:'new'}); h.elements.modal.innerHTML+='<textarea>New task after permission request</textarea>';
    const status=h.elements['mutation-status'].textContent, toast=h.elements.toast.textContent;
    permission.resolve('denied'); await enabling;
    assert.match(h.elements.modal.innerHTML,/New task after permission request/); assert.equal(h.elements['mutation-status'].textContent,status);
    assert.equal(h.elements.toast.textContent,toast); assert.equal(writes(h).length,0);
  }

  // Today is useful before any independent calendar, channel, preference or change response.
  {
    const settings=deferred(), channels=deferred(), changes=deferred(), month=deferred(), upcoming=deferred();
    const h=harness({overrides:{'/api/settings':()=>settings.promise,'/api/channels':()=>channels.promise,'/api/changes':()=>changes.promise,
      '/api/calendar':(_,url)=>url.includes('start=2026-11-07') ? upcoming.promise : month.promise}}); await settle();
    assert.match(h.elements.app.innerHTML,/Fixture assignment/);
    assert.match(h.elements.app.innerHTML,/다가오는 일정을 불러오고/);
    assert.doesNotMatch(h.elements.app.innerHTML,/다시 로그인|학교 계정 연결|data-load-state/);
    assert.match(h.elements.app.innerHTML,/data-action="filters" disabled/);
    await h.click({action:'filters'}); assert.ok(!h.elements.modal.open);
    await h.click({action:'calendar'}); assert.match(h.elements.app.innerHTML,/달력 일정을 불러오고/);
    month.resolve(response({items:[{...h.item,title:'Independent calendar result'}]})); await settle();
    assert.match(h.elements.app.innerHTML,/Independent calendar result/);
    await h.click({action:'today'});
    upcoming.resolve(response({items:[{...h.item,title:'Independent upcoming result',end_date:'2026-11-09'}]})); await settle();
    assert.match(h.elements.app.innerHTML,/Independent upcoming result/);
    settings.resolve(response({preferences:{disabled_kinds:[]},connection:{state:'connected'}})); await settle();
    assert.match(h.elements.app.innerHTML,/data-action="filters" disabled/);
    channels.resolve(response([{id:'fixture-channel',enabled:true}])); await settle();
    assert.doesNotMatch(h.elements.app.innerHTML,/data-action="filters" disabled/);
    await h.click({action:'filters'}); assert.equal(h.elements.modal.open,true); await h.click({action:'close'});
    changes.resolve(response({detail:'Changes failed'},503)); await settle();
    assert.match(h.elements.app.innerHTML,/Fixture assignment/); assert.match(h.elements['refresh-status'].textContent,/일부 최신 정보/);
  }
  // An empty confirmed snapshot is still described as being checked until metadata arrives.
  {
    const settings=deferred(), h=harness({overrides:{'/api/today':()=>response({date:'2026-11-07',teams:[],personal:[]}),'/api/settings':()=>settings.promise}}); await settle();
    assert.match(h.elements.app.innerHTML,/Teams 일정을 확인하는 대로/);
    assert.doesNotMatch(h.elements.app.innerHTML,/다시 로그인|확인 필요/);
    settings.resolve(response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:true}})); await settle();
    assert.match(h.elements.app.innerHTML,/Teams 일정을 확인하는 대로/);
    assert.doesNotMatch(h.elements.app.innerHTML,/오늘은 Teams 일정이 없어요/);
  }
  // Partial rendering still treats a later metadata 403 as loss of the whole private session.
  {
    const settings=deferred(), h=harness({overrides:{'/api/settings':()=>settings.promise}}); await settle();
    assert.match(h.elements.app.innerHTML,/Fixture assignment/);
    settings.resolve(response({},403)); await settle();
    assert.match(h.elements.app.innerHTML,/school-signin/); assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
  }
  // New independent responses during a write must lock the newly rendered quick input too.
  {
    const settings=deferred(), saving=deferred(), h=harness({overrides:{'/api/settings':()=>settings.promise}}); await settle();
    const descriptor=Object.getOwnPropertyDescriptor(h.elements.app,'innerHTML'); let latest;
    Object.defineProperty(h.elements.app,'innerHTML',{get:descriptor.get,set(value){descriptor.set.call(this,value);
      latest={value:'Pending quick draft',dataset:{},disabled:false}; h.elements['quick-form']={elements:{title:latest}}; h.controls.push(latest);}});
    const form={id:'quick-form',elements:{title:{value:'Pending quick draft',dataset:{},disabled:false}}}; h.elements['quick-form']=form; h.controls.push(form.elements.title);
    h.overrides['/api/tasks']=()=>saving.promise; const save=h.submit(form); await settle();
    settings.resolve(response({preferences:{disabled_kinds:[]},connection:{state:'connected'}})); await settle();
    assert.ok(latest); assert.equal(latest.disabled,true);
    saving.resolve(response({detail:'Temporary save error'},503)); await save;
    assert.equal(latest.disabled,false); assert.equal(latest.value,'Pending quick draft');
  }
  // Connected ingestion uses bounded GETs and displays each newly materialized schedule.
  {
    let pending=true, reads=0;
    const h=harness({overrides:{'/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:pending}})}}); await settle();
    h.overrides['/api/today']=()=>{reads++; return response({...h.today,teams:[...h.today.teams,{...h.item,id:'new-post',title:'New analyzed post'}]});};
    const identityReads=h.requests.filter(r=>r.url==='/api/me').length;
    await h.advance(3000); assert.equal(reads,1); assert.match(h.elements.app.innerHTML,/New analyzed post/);
    pending=false; await h.advance(3000); assert.equal(reads,3); // observed completion + final data pass
    const count=h.requests.length; await h.advance(20000); assert.equal(h.requests.length,count);
    assert.equal(writes(h).length,0); assert.equal(h.requests.filter(r=>r.url==='/api/me').length,identityReads);
  }
  // A first pending=false can race the worker's last commit; one final pass catches it.
  {
    const h=harness({overrides:{'/api/service':()=>response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),'/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:false}})}}); await settle();
    h.overrides['/api/today']=()=>response({...h.today,teams:[{...h.item,title:'Last post committed after first today'}]});
    await h.advance(3000); assert.match(h.elements.app.innerHTML,/Last post committed after first today/);
    const count=h.requests.length; await h.advance(30000); assert.equal(h.requests.length,count);
  }
  // Unchanged polling data never replaces the root repeatedly; real changes preserve quick focus/selection.
  {
    const h=harness({overrides:{'/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:true}})}}); await settle();
    const before=h.history.filter(x=>x.id==='app').length;
    await h.advance(18000); assert.equal(h.history.filter(x=>x.id==='app').length,before);
    const descriptor=Object.getOwnPropertyDescriptor(h.elements.app,'innerHTML'); let focusCalls=0;
    const makeInput=value=>({value,dataset:{},selectionStart:3,selectionEnd:7,selectionDirection:'backward',
      focus(){focusCalls++; h.context.document.activeElement=this;},setSelectionRange(a,b,d){this.selectionStart=a;this.selectionEnd=b;this.selectionDirection=d;}});
    const old=makeInput('Keep typing this draft'); h.elements['quick-form']={elements:{title:old}}; h.context.document.activeElement=old;
    Object.defineProperty(h.elements.app,'innerHTML',{get:descriptor.get,set(value){descriptor.set.call(this,value);h.elements['quick-form']={elements:{title:makeInput(value.match(/name="title"/) ? 'Keep typing this draft' : '')}};}});
    h.overrides['/api/today']=()=>response({...h.today,teams:[{...h.item,title:'Arrived while typing'}]});
    await h.advance(3000); const input=h.elements['quick-form'].elements.title;
    assert.match(h.elements.app.innerHTML,/Arrived while typing/); assert.match(h.elements.app.innerHTML,/Keep typing this draft/);
    assert.notEqual(input,old); assert.equal(h.context.document.activeElement,input); assert.equal(focusCalls,1);
    assert.equal(input.selectionStart,3);assert.equal(input.selectionEnd,7);assert.equal(input.selectionDirection,'backward');
  }
  // Hidden tabs do not poll; becoming visible schedules checks without an immediate full reload.
  {
    const h=harness({overrides:{'/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:true}})}}); await settle();
    await h.visibility(true); const before=h.requests.filter(r=>r.url==='/api/today').length;
    await h.advance(30000); assert.equal(h.requests.filter(r=>r.url==='/api/today').length,before);
    await h.visibility(false); await settle(); assert.equal(h.requests.filter(r=>r.url==='/api/today').length,before);
    await h.advance(3000); assert.equal(h.requests.filter(r=>r.url==='/api/today').length,before+1);
  }
  // A delayed poll cannot overlap another poll or mutate an open dialog.
  {
    const h=harness({overrides:{'/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:true}})}}); await settle();
    await h.click({action:'new'}); h.elements.modal.innerHTML+='<textarea>Open draft</textarea>';
    const slow=deferred(); h.overrides['/api/today']=()=>slow.promise;
    await h.advance(3000); const count=h.requests.filter(r=>r.url==='/api/today').length;
    await h.advance(9000); assert.equal(h.requests.filter(r=>r.url==='/api/today').length,count);
    slow.resolve(response({...h.today,teams:[{...h.item,title:'Background result'}]})); await settle();
    assert.match(h.elements.modal.innerHTML,/Open draft/); assert.match(h.elements.app.innerHTML,/Background result/);
  }
  // Confirmed writes fence a pre-write polling response even if transport ignores abort.
  {
    const h=harness({ignoreAbort:new Set(['/api/today']),overrides:{'/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:true}})}}); await settle();
    const stale=deferred(); h.overrides['/api/today']=()=>stale.promise; await h.advance(3000);
    await h.click({detail:'fixture',source:'teams'});
    h.overrides['/api/events/fixture/state']=()=>response({hidden:true});
    h.overrides['/api/today']=()=>response({detail:'Read failed'},503);
    await h.click({action:'hide-detail'}); assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    stale.resolve(response(h.today)); await settle(); assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    assert.equal(writes(h).length,1);
  }
  // The ingestion budget is finite and its status stops promising automatic progress.
  {
    const h=harness({overrides:{'/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:true}})}}); await settle();
    await h.advance(180001); const count=h.requests.filter(r=>r.url==='/api/today').length;
    assert.ok(count>1&&count<=61); assert.match(h.elements['refresh-status'].textContent,/나머지는 새로고침/);
    await h.advance(60000); assert.equal(h.requests.filter(r=>r.url==='/api/today').length,count); assert.equal(writes(h).length,0);
  }
  // A polling error preserves verified results and stops automatic retries.
  {
    const h=harness({overrides:{'/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:true}})}}); await settle();
    h.overrides['/api/today']=()=>response({detail:'Read failed'},503); await h.advance(3000);
    assert.match(h.elements.app.innerHTML,/Fixture assignment/); assert.match(h.elements['refresh-status'].textContent,/다시 확인/);
    const count=h.requests.length; await h.advance(30000); assert.equal(h.requests.length,count); assert.equal(writes(h).length,0);
  }
  // Permission checking owns its slower retry, never a duplicate ingestion polling loop.
  {
    const h=harness({overrides:{'/api/service':()=>response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),
      '/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'checking',ingestion_pending:true}})}}); await settle();
    const count=h.requests.filter(r=>r.url==='/api/today').length;
    await h.advance(14999); assert.equal(h.requests.filter(r=>r.url==='/api/today').length,count);
    await h.advance(1); assert.equal(h.requests.filter(r=>r.url==='/api/today').length,count+1);
  }
  // Polling is not user activity and cannot extend the local idle session.
  {
    const h=harness({overrides:{'/api/service':()=>response({mode:'test-server',idle_seconds:30}),
      '/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:true}})}}); await settle();
    h.activity(); await h.advance(30001); assert.match(h.elements.app.innerHTML,/school-signin/);
    const count=h.requests.length; await h.advance(30000); assert.equal(h.requests.length,count);
  }
  // Logout invalidates an in-flight abort-ignoring poll and its future timers.
  {
    const h=harness({ignoreAbort:new Set(['/api/today']),overrides:{'/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:true}})}}); await settle();
    const slow=deferred();h.overrides['/api/today']=()=>slow.promise;await h.advance(3000);
    await h.click({action:'logout'}); const count=h.requests.length;slow.resolve(response(h.today));await settle();await h.advance(30000);
    assert.match(h.elements.app.innerHTML,/school-signin/);assert.equal(h.requests.length,count);
  }

  // A final completion read interrupted by a failed write stays pending until data is applied.
  {
    const h=harness({overrides:{'/api/service':()=>response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),
      '/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:false}})}}); await settle();
    const finalRead=deferred(), write=deferred();h.overrides['/api/today']=()=>finalRead.promise;await h.advance(3000);
    h.overrides['/api/tasks']=()=>write.promise;
    const form={id:'quick-form',elements:{title:{value:'Keep failed draft'}}};h.elements['quick-form']=form;
    const save=h.submit(form);await settle();finalRead.resolve(response(h.today));await settle();
    write.resolve(response({detail:'Save rejected'},422));await save;
    h.overrides['/api/today']=()=>response({...h.today,teams:[{...h.item,title:'Final result after failed write'}]});
    await h.advance(3000);assert.match(h.elements.app.innerHTML,/Final result after failed write/);assert.match(h.elements.app.innerHTML,/Keep failed draft/);
    const count=h.requests.length;await h.advance(15000);assert.equal(h.requests.length,count);assert.equal(writes(h).length,1);
  }
  // A known permission failure clears earlier Teams data and fences late same-cycle reads.
  {
    const settings=deferred(), calendar=deferred(), changes=deferred();
    const h=harness({overrides:{'/api/settings':()=>settings.promise,'/api/calendar':()=>calendar.promise,'/api/changes':()=>changes.promise}});await settle();
    assert.match(h.elements.app.innerHTML,/Fixture assignment/);await h.click({detail:'fixture',source:'teams'});
    settings.resolve(response({preferences:{disabled_kinds:[]},connection:{state:'verification_failed',ingestion_pending:false}}));await settle();
    assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);assert.equal(h.elements.modal.open,false);
    calendar.resolve(response({items:[h.item]}));changes.resolve(response({items:[{id:'stale-change',title:'Stale change'}]}));await settle();
    assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);await h.click({action:'calendar'});
    assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);assert.match(h.elements.app.innerHTML,/접근 권한을 확인하지 못해/);
  }
  // The new false field alone does not add background reads to non-shared deployments.
  {
    const h=harness({overrides:{'/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:false}})}});await settle();
    const count=h.requests.length;await h.advance(30000);assert.equal(h.requests.length,count);
  }

  // Automatic reads wait for identity revalidation after a tab/account switch.
  for (const state of ['connected','checking']) {
    const h=harness({overrides:{'/api/service':()=>response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),
      '/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state,ingestion_pending:true}})}});await settle();
    const identity=deferred();h.overrides['/api/me']=()=>identity.promise;
    h.overrides['/api/today']=()=>response({...h.today,teams:[{...h.item,title:'Other account private result'}]});
    await h.visibility(true);await h.visibility(false);await settle();const count=h.requests.filter(r=>r.url==='/api/today').length;
    await h.advance(16000);assert.equal(h.requests.filter(r=>r.url==='/api/today').length,count);
    assert.doesNotMatch(h.elements.app.innerHTML,/Other account private result/);
    identity.resolve(response({id:'other-account',name:'Other account',csrf_token:'other-csrf'}));await settle();
    assert.match(h.elements.app.innerHTML,/school-signin/);await h.advance(20000);
    assert.equal(h.requests.filter(r=>r.url==='/api/today').length,count);
  }
  // A read already in flight also cannot publish while resumed identity is unknown.
  {
    const h=harness({overrides:{'/api/service':()=>response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),
      '/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:false}})}});await settle();
    const data=deferred(),identity=deferred();h.overrides['/api/today']=()=>data.promise;await h.advance(3000);
    h.overrides['/api/me']=()=>identity.promise;await h.visibility(true);await h.visibility(false);await settle();
    data.resolve(response({...h.today,teams:[{...h.item,title:'Unvalidated account snapshot'}]}));await settle();
    assert.doesNotMatch(h.elements.app.innerHTML,/Unvalidated account snapshot/);
    identity.resolve(response({id:'fixture-user',name:'Fixture student',csrf_token:'fixture-csrf'}));await settle();
    h.overrides['/api/today']=()=>response({...h.today,teams:[{...h.item,title:'Final result after identity check'}]});
    await h.advance(3000);assert.match(h.elements.app.innerHTML,/Final result after identity check/);
    const count=h.requests.length;await h.advance(15000);assert.equal(h.requests.length,count);
  }

  // Permission failure closes only a currently Teams-bearing dialog, preserving unrelated drafts.
  {
    const h=harness();await settle();await h.click({detail:'fixture',source:'teams'});await h.click({action:'close'});
    await h.click({action:'feedback'});h.elements.modal.innerHTML+='<textarea>Unsent feedback after Teams detail</textarea>';
    h.overrides['/api/settings']=()=>response({preferences:{disabled_kinds:[]},connection:{state:'verification_failed'}});
    await h.click({action:'refresh'});assert.equal(h.elements.modal.open,true);
    assert.match(h.elements.modal.innerHTML,/Unsent feedback after Teams detail/);assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
  }
  // Hidden Teams data is removed on explicit permission failure without relying on detailItem.
  {
    const h=harness();await settle();
    h.overrides['/api/hidden?start=2026-01-01&end=2026-12-31']=()=>response({items:[{...h.item,hidden:true}]});
    await h.click({action:'hidden'});assert.match(h.elements.modal.innerHTML,/Fixture assignment/);
    h.overrides['/api/settings']=()=>response({preferences:{disabled_kinds:[]},connection:{state:'verification_failed'}});
    await h.click({action:'refresh'});assert.equal(h.elements.modal.open,false);assert.doesNotMatch(h.elements.modal.innerHTML,/Fixture assignment/);
  }
  // A change-review dialog also belongs to the revoked Teams data, even with no prior detail.
  {
    const h=harness();await settle();
    h.overrides['/api/changes']=()=>response({items:[{id:'change-fixture',before:h.item,after:{...h.item,notes:'Changed private notes'}}]});
    await h.click({action:'refresh'});await h.click({action:'changes'});assert.match(h.elements.modal.innerHTML,/Changed private notes/);
    h.overrides['/api/settings']=()=>response({preferences:{disabled_kinds:[]},connection:{state:'verification_failed'}});
    await h.click({action:'refresh'});assert.equal(h.elements.modal.open,false);assert.doesNotMatch(h.elements.modal.innerHTML,/Changed private notes/);
  }

  // A failed/expired resumed identity check never enables automatic reads under the old label.
  for (const failure of ['http503','timeout']) {
    const h=harness({overrides:{'/api/service':()=>response({mode:'test-server',idle_seconds:1800,shared_channel_ingestion:true}),
      '/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:true}})}});await settle();
    await h.click({action:'feedback'});h.elements.modal.innerHTML+='<textarea>Draft during identity failure</textarea>';
    const identity=deferred();h.overrides['/api/me']=()=>failure==='timeout' ? identity.promise : response({},503);
    h.overrides['/api/today']=()=>response({...h.today,teams:[{...h.item,title:'Unverified other account data'}]});
    await h.visibility(true);await h.visibility(false);await settle();const before=h.requests.filter(r=>r.url==='/api/today').length;
    await h.advance(61000);assert.equal(h.requests.filter(r=>r.url==='/api/today').length,before);
    assert.doesNotMatch(h.elements.app.innerHTML,/Unverified other account data/);
    assert.match(h.elements.modal.innerHTML,/Draft during identity failure/);assert.match(h.elements['refresh-status'].textContent,/자동 갱신을 잠시 멈췄어요/);
    h.overrides['/api/me']=()=>response({id:'fixture-user',name:'Fixture student',csrf_token:'fixture-csrf'});
    h.overrides['/api/today']=()=>response({...h.today,teams:[{...h.item,title:'Verified resumed account result'}]});
    h.activity();await settle();await h.advance(3000);
    assert.match(h.elements.app.innerHTML,/Verified resumed account result/);assert.match(h.elements.modal.innerHTML,/Draft during identity failure/);
  }

  // Personal schedules appear while the independent Teams permission read is still pending.
  {
    const full=deferred(), task=personalTask();
    const h=harness({overrides:{'/api/service':personalService,'/api/personal/today':()=>response({date:'2026-11-07',personal:[task]}),'/api/today':()=>full.promise}});await settle();
    assert.match(h.elements.app.innerHTML,/Personal first result/);assert.match(h.elements.app.innerHTML,/Teams 일정 <span class="count">확인 중/);
    assert.match(h.elements.app.innerHTML,/role="status">Teams 일정을 확인하는 대로/);assert.match(h.elements.app.innerHTML,/개인 할 일 기준/);
    assert.doesNotMatch(h.elements.app.innerHTML,/오늘의 Teams 일정이 없어요|data-load-state|다시 로그인/);
    assert.equal(h.requests.filter(r=>r.url.startsWith('/api/calendar?')).length,0);
    await h.click({action:'new'});assert.equal(h.elements.modal.open,true);h.elements.modal.innerHTML+='<textarea>Draft before Teams arrives</textarea>';
    full.resolve(response({...h.today,personal:[task]}));await settle();
    assert.match(h.elements.app.innerHTML,/Fixture assignment/);assert.match(h.elements.app.innerHTML,/Personal first result/);
    assert.match(h.elements.modal.innerHTML,/Draft before Teams arrives/);assert.equal(h.requests.filter(r=>r.url.startsWith('/api/calendar?')).length,2);
  }
  // A combined response completes the view without waiting for or later accepting stale personal data.
  {
    const personal=deferred(), task=personalTask({title:'Full snapshot personal'});
    const h=harness({overrides:{'/api/service':personalService,'/api/personal/today':()=>personal.promise,
      '/api/today':()=>response({date:'2026-11-07',teams:[],personal:[task]})}});await settle();
    assert.match(h.elements.app.innerHTML,/Full snapshot personal/);assert.equal(h.elements['refresh-button'].disabled,false);
    personal.resolve(response({date:'2026-11-07',personal:[personalTask({title:'Stale delayed personal'})]}));await settle();
    assert.match(h.elements.app.innerHTML,/Full snapshot personal/);assert.doesNotMatch(h.elements.app.innerHTML,/Stale delayed personal/);
  }
  // True empty personal data is distinguished from an unreceived personal response.
  {
    const personal=deferred(),full=deferred();
    const h=harness({overrides:{'/api/service':personalService,'/api/personal/today':()=>personal.promise,'/api/today':()=>full.promise}});await settle();
    assert.match(h.elements.app.innerHTML,/개인 할 일을 불러오는 중/);assert.doesNotMatch(h.elements.app.innerHTML,/개인 할 일 <span class="count">0/);
    personal.resolve(response({date:'2026-11-07',personal:[]}));await settle();
    assert.match(h.elements.app.innerHTML,/개인 할 일 <span class="count">0/);assert.match(h.elements.app.innerHTML,/오늘 할 일을 가볍게/);
    assert.match(h.elements.app.innerHTML,/Teams 일정 <span class="count">확인 중/);
  }
  // Permission-pending Teams is a progress state; personal completion and creation stay usable.
  {
    let task=personalTask(),items=[task];
    const h=harness({overrides:{'/api/service':personalService,'/api/personal/today':()=>response({date:'2026-11-07',personal:items}),
      '/api/today':()=>response({code:'permission_check_pending'},503)}});await settle();
    assert.match(h.elements.app.innerHTML,/Personal first result/);assert.match(h.elements.app.innerHTML,/role="status">Teams 접근 권한/);
    assert.doesNotMatch(h.elements.app.innerHTML,/role="alert"|서버 응답이 늦|data-load-state="error"/);
    h.overrides['/api/tasks/personal-ready/state']=options=>{task={...task,...JSON.parse(options.body),version:2};items=[task];return response(task);};
    await h.change({dataset:{complete:task.id,source:'personal'},checked:true});
    assert.match(h.elements.app.innerHTML,/data-complete="personal-ready" data-source="personal" checked/);
    const created=personalTask({id:'new-personal',title:'Added while Teams checks'});
    h.overrides['/api/tasks']=()=>{items=[task,created];return response(created,201);};
    const form={id:'quick-form',elements:{title:{value:created.title}}};h.elements['quick-form']=form;await h.submit(form);
    assert.match(h.elements.app.innerHTML,/Added while Teams checks/);assert.equal(writes(h).length,2);
  }
  // A permission wait before either source arrives is announced as status, without a server-failure claim.
  {
    const personal=deferred(),h=harness({overrides:{'/api/service':personalService,'/api/personal/today':()=>personal.promise,
      '/api/today':()=>response({code:'permission_check_pending'},503)}});await settle();
    assert.match(h.elements.app.innerHTML,/data-load-state="checking"/);assert.match(h.elements.app.innerHTML,/role="status"/);
    assert.doesNotMatch(h.elements.app.innerHTML,/role="alert"|서버 응답이 늦/);
    personal.resolve(response({date:'2026-11-07',personal:[personalTask()]}));await settle();assert.match(h.elements.app.innerHTML,/Personal first result/);
  }
  // A stalled Teams response cannot remove the already verified personal list.
  {
    const full=deferred(),h=harness({overrides:{'/api/service':personalService,'/api/personal/today':()=>response({date:'2026-11-07',personal:[personalTask()]}),'/api/today':()=>full.promise}});await settle();
    await h.advance(90001);assert.match(h.elements.app.innerHTML,/Personal first result/);assert.doesNotMatch(h.elements.app.innerHTML,/data-load-state="error"/);
    assert.match(h.elements.app.innerHTML,/role="alert">불러오지 못했어요/);assert.equal(h.elements['refresh-button'].disabled,false);
  }
  // Failure of the optional personal read is superseded by a complete successful snapshot.
  {
    const h=harness({overrides:{'/api/service':personalService,'/api/personal/today':()=>response({},503),
      '/api/today':()=>response({date:'2026-11-07',teams:[],personal:[personalTask()]})}});await settle();
    assert.match(h.elements.app.innerHTML,/Personal first result/);assert.doesNotMatch(h.elements['refresh-status'].textContent,/못했어요/);
    assert.doesNotMatch(h.elements.app.innerHTML,/role="alert"/);
  }
  // When neither source succeeds, the whole load still ends in an actionable error.
  {
    const h=harness({overrides:{'/api/service':personalService,'/api/personal/today':()=>response({},503),'/api/today':()=>response({},503)}});await settle();
    assert.match(h.elements.app.innerHTML,/data-load-state="error"/);assert.match(h.elements.app.innerHTML,/role="alert"/);
  }
  // A new personal date never borrows Teams rows from the previous displayed day.
  {
    let day='2026-11-07';const task=personalTask();
    const h=harness({overrides:{'/api/service':personalService,'/api/personal/today':()=>response({date:day,personal:[{...task,start_date:day,end_date:day}]})}});await settle();
    assert.match(h.elements.app.innerHTML,/Fixture assignment/);day='2026-11-08';const full=deferred();h.overrides['/api/today']=()=>full.promise;
    const refresh=h.click({action:'refresh'});await settle();assert.match(h.elements.app.innerHTML,/11월 8일/);assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    full.resolve(response(h.today));await refresh;assert.match(h.elements.app.innerHTML,/11월 8일/);assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    assert.match(h.elements.app.innerHTML,/날짜가 바뀌었어요/);
  }
  // Confirmed hide during the personal-first screen fences the old combined response.
  {
    const full=deferred(),task=personalTask();let items=[task];
    const h=harness({ignoreAbort:new Set(['/api/today']),overrides:{'/api/service':personalService,
      '/api/personal/today':()=>response({date:'2026-11-07',personal:items}),'/api/today':()=>full.promise}});await settle();
    await h.click({detail:task.id,source:'personal'});h.overrides['/api/tasks/personal-ready/state']=()=>{items=[];return response({...task,hidden:true,version:2});};
    h.overrides['/api/today']=()=>response({code:'permission_check_pending'},503);await h.click({action:'hide-detail'});
    assert.doesNotMatch(h.elements.app.innerHTML,/Personal first result/);full.resolve(response({...h.today,personal:[task]}));await settle();
    assert.doesNotMatch(h.elements.app.innerHTML,/Personal first result/);assert.equal(writes(h).length,1);
  }
  // Logout prevents both abort-ignoring parallel sources from publishing private content.
  {
    const personal=deferred(),full=deferred(),h=harness({ignoreAbort:new Set(['/api/today','/api/personal/today']),overrides:{'/api/service':personalService,
      '/api/personal/today':()=>personal.promise,'/api/today':()=>full.promise}});await settle();
    await h.click({action:'logout'});const before=h.requests.length;personal.resolve(response({date:'2026-11-07',personal:[personalTask()]}));full.resolve(response(h.today));await settle();
    assert.match(h.elements.app.innerHTML,/school-signin/);assert.doesNotMatch(h.elements.app.innerHTML,/Personal first result|Fixture assignment/);assert.equal(h.requests.length,before);
  }
  // A personal-only 401 clears the session even while a protected Teams response is pending.
  {
    const full=deferred(),h=harness({overrides:{'/api/service':personalService,'/api/personal/today':()=>response({},401),'/api/today':()=>full.promise}});await settle();
    assert.match(h.elements.app.innerHTML,/school-signin/);assert.doesNotMatch(h.elements.app.innerHTML,/Personal first result/);
  }
  // Identity verification failure also suspends the new automatic personal read.
  {
    const h=harness({overrides:{'/api/service':personalService,'/api/personal/today':()=>response({date:'2026-11-07',personal:[personalTask()]}),
      '/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',ingestion_pending:true}})}});await settle();
    h.overrides['/api/me']=()=>response({},503);await h.visibility(true);await h.visibility(false);await settle();
    const before=h.requests.filter(r=>r.url==='/api/personal/today').length;await h.advance(30000);
    assert.equal(h.requests.filter(r=>r.url==='/api/personal/today').length,before);assert.match(h.elements['refresh-status'].textContent,/자동 갱신을 잠시 멈췄어요/);
  }
  // A verified calendar becoming unverified is redacted, with a clear progress message and usable personal rows.
  {
    let verified=true;const task=personalTask({title:'Keep personal during access check'});
    const h=harness({overrides:{'/api/service':personalService,'/api/personal/today':()=>response({date:'2026-11-07',personal:[task]}),
      '/api/settings':()=>accessSettings(verified)}});
    h.today.personal=[task];h.overrides['/api/calendar']=()=>response({items:[h.item,task]});await settle();await h.click({action:'calendar'});
    assert.match(h.elements.app.innerHTML,/Fixture assignment/);await h.click({detail:task.id,source:'personal'});const personalDialog=h.elements.modal.innerHTML;
    verified=false;await h.click({action:'refresh'});await settle();
    assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);assert.match(h.elements.app.innerHTML,/Keep personal during access check/);
    assert.match(h.elements.app.innerHTML,/권한/);assert.match(h.elements.app.innerHTML,/(일시|잠시)[^<]*(숨|표시)/);
    assert.equal(h.elements.modal.open,true);assert.equal(h.elements.modal.innerHTML,personalDialog);assert.equal(writes(h).length,0);
  }
  // A late successful calendar response from before unverified metadata cannot reinsert private Teams titles.
  {
    let verified=true;const h=harness({ignoreAbort:new Set(),overrides:{'/api/service':personalService,'/api/settings':()=>accessSettings(verified)}});await settle();await h.click({action:'calendar'});
    const late=deferred();h.overrides[calendarUrl(h,'2026-11')]=()=>late.promise;verified=false;
    const refresh=h.click({action:'refresh'});await settle();assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);const start=h.history.length;
    late.resolve(response({items:[{...h.item,title:'Late unverified private title'}]}));await refresh;await settle();
    assert.doesNotMatch(h.elements.app.innerHTML,/Late unverified private title/);
    assert.ok(h.history.slice(start).filter(value=>value.id==='app').every(value=>!value.html.includes('Late unverified private title')));
  }
  // A retryable source failure recovers the current calendar by reading fresh data, then stops its own loop.
  {
    let verified=true;const h=harness({overrides:{'/api/service':personalService,'/api/settings':()=>accessSettings(verified)}});await settle();await h.click({action:'calendar'});
    verified=false;await h.click({action:'refresh'});assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    const calendarBefore=h.requests.filter(value=>value.url===calendarUrl(h,'2026-11')).length;
    verified=true;h.overrides['/api/calendar']=()=>response({items:[{...h.item,title:'Fresh verified calendar result'}]});
    await h.advance(15000);await settle();assert.match(h.elements.app.innerHTML,/Fresh verified calendar result/);
    assert.ok(h.requests.filter(value=>value.url===calendarUrl(h,'2026-11')).length>calendarBefore);
    const completed=h.requests.length;await h.advance(60000);assert.equal(h.requests.length,completed);assert.equal(writes(h).length,0);
  }
  // An unverified month destination is remembered and retried automatically after permissions recover.
  {
    const task=personalTask();let verified=true;const h=harness({overrides:{'/api/service':personalService,'/api/settings':()=>accessSettings(verified)}});h.today.personal=[task];await settle();await h.click({action:'calendar'});
    h.overrides[calendarUrl(h,'2026-12')]=()=>verified ? response({items:[{...h.item,title:'Recovered requested December',start_date:'2026-12-01',end_date:'2026-12-01'}]}) : unverifiedAccess();
    verified=false;await h.click({action:'next-month'});assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);assert.match(h.elements.app.innerHTML,/(일시|잠시)[^<]*(숨|표시)/);
    verified=true;await h.advance(15000);await settle();assert.match(h.elements.app.innerHTML,/2026년 12월/);assert.match(h.elements.app.innerHTML,/Recovered requested December/);
    assert.equal(writes(h).length,0);
  }
  // A terminal authentication failure on initial Teams loading still offers personal tasks and a sign-in action.
  {
    const h=harness({overrides:{'/api/service':personalService,'/api/personal/today':()=>response({date:'2026-11-07',personal:[personalTask()]}),
      '/api/today':()=>unverifiedAccess(false,'reauth_required')}});await settle();
    assert.match(h.elements.app.innerHTML,/Personal first result/);assert.match(h.elements.app.innerHTML,/다시 로그인/);
    assert.doesNotMatch(h.elements.app.innerHTML,/school-signin|Fixture assignment/);
    const stopped=h.requests.length;await h.advance(60000);assert.equal(h.requests.length,stopped);assert.equal(writes(h).length,0);
  }
  // A freshly verified empty channel set is a genuine empty calendar, without a permission retry banner.
  {
    const h=harness({overrides:{'/api/service':personalService,'/api/settings':()=>accessSettings(true),
      '/api/today':()=>response({date:'2026-11-07',teams:[],personal:[]}),'/api/calendar':()=>response({items:[]})}});await settle();await h.click({action:'calendar'});
    assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment|(일시|잠시)[^<]*(숨|표시)/);
    const stopped=h.requests.length;await h.advance(60000);assert.equal(h.requests.length,stopped);assert.equal(writes(h).length,0);
  }
  // Retryable metadata on a non-checking state shares the finite five-minute retry budget.
  {
    const h=harness({overrides:{'/api/service':personalService,'/api/settings':()=>accessSettings(false)}});await settle();await h.advance(315000);
    const reads=h.requests.filter(value=>value.url==='/api/today').length;assert.ok(reads>1&&reads<=21);
    assert.match(h.elements.app.innerHTML,/권한/);assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    const stopped=h.requests.length;await h.advance(60000);assert.equal(h.requests.length,stopped);assert.equal(writes(h).length,0);
  }
  // New readiness retries pause while hidden and after a failed identity check on resume.
  {
    const h=harness({overrides:{'/api/service':personalService,'/api/settings':()=>accessSettings(false)}});await settle();await h.visibility(true);
    const hidden=h.requests.length;await h.advance(30000);assert.equal(h.requests.length,hidden);
    h.overrides['/api/me']=()=>response({},503);await h.visibility(false);await settle();const paused=h.requests.length;
    await h.advance(60000);assert.equal(h.requests.length,paused);assert.match(h.elements['refresh-status'].textContent,/자동 갱신을 잠시 멈췄어요/);
    assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
  }
  // Logout fences a delayed readiness retry and does not allow its old response to restore private rows.
  {
    let verified=true;const h=harness({ignoreAbort:new Set(['/api/today']),overrides:{'/api/service':personalService,'/api/settings':()=>accessSettings(verified)}});await settle();
    verified=false;await h.click({action:'refresh'});const late=deferred();h.overrides['/api/today']=()=>late.promise;
    await h.advance(15000);await h.click({action:'logout'});const stopped=h.requests.length;
    late.resolve(response({...h.today,teams:[{...h.item,title:'Old-account hidden Teams row'}]}));await settle();await h.advance(60000);
    assert.match(h.elements.app.innerHTML,/school-signin/);assert.doesNotMatch(h.elements.app.innerHTML,/Old-account hidden Teams row|Fixture assignment/);assert.equal(h.requests.length,stopped);
  }
  // A second navigation during permission recovery supersedes the older deferred destination.
  {
    let verified=true;const h=harness({overrides:{'/api/service':personalService,'/api/settings':()=>accessSettings(verified)}});await settle();await h.click({action:'calendar'});
    h.overrides[calendarUrl(h,'2026-12')]=()=>unverifiedAccess();verified=false;await h.click({action:'next-month'});
    h.overrides[calendarUrl(h,'2027-01')]=()=>verified ? response({items:[{...h.item,title:'Latest requested January',start_date:'2027-01-01',end_date:'2027-01-01'}]}) : unverifiedAccess();
    await h.click({action:'next-month'});verified=true;await h.advance(15000);await settle();
    assert.match(h.elements.app.innerHTML,/2027년 1월/);assert.match(h.elements.app.innerHTML,/Latest requested January/);assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
  }
  // Typed unverified metadata redacts an already-open Teams detail instead of leaving private text behind.
  {
    const h=harness({overrides:{'/api/service':personalService}});await settle();await h.click({detail:'fixture',source:'teams'});assert.match(h.elements.modal.innerHTML,/Fixture assignment/);
    h.overrides['/api/events/fixture/state']=()=>unverifiedAccess();await h.click({action:'hide-detail'});
    assert.equal(h.elements.modal.open,false);assert.doesNotMatch(h.elements.modal.innerHTML,/Fixture assignment/);
    assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);assert.match(h.elements.app.innerHTML,/(일시|잠시)[^<]*(숨|표시)/);assert.equal(writes(h).length,1);
  }
  // A verified connected account never starts a permission loop from an accidentally true retryable flag.
  {
    const h=harness({overrides:{'/api/service':personalService,'/api/settings':()=>accessSettings(true,true)}});await settle();
    const before=h.requests.filter(value=>value.url==='/api/today').length;await h.advance(60000);
    assert.equal(h.requests.filter(value=>value.url==='/api/today').length,before);assert.match(h.elements.app.innerHTML,/Fixture assignment/);
  }
  // Verified connected state with retryable=true still runs the ordinary three-second ingestion loop once per tick.
  {
    const h=harness({overrides:{'/api/service':personalService,'/api/settings':()=>response({preferences:{disabled_kinds:[]},
      connection:{state:'connected',teams_verified:true,retryable:true,ingestion_pending:true}})}});await settle();
    const before=h.requests.filter(value=>value.url==='/api/today').length;await h.advance(2999);
    assert.equal(h.requests.filter(value=>value.url==='/api/today').length,before);await h.advance(1);
    assert.equal(h.requests.filter(value=>value.url==='/api/today').length,before+1);await h.advance(12000);
    assert.equal(h.requests.filter(value=>value.url==='/api/today').length,before+5);assert.equal(writes(h).length,0);
  }
  // Connected without verified access does retry after fifteen seconds, then stops once proof is restored.
  {
    let verified=false;const h=harness({overrides:{'/api/service':personalService,'/api/settings':()=>accessSettings(verified,true,'connected')}});await settle();
    const before=h.requests.filter(value=>value.url==='/api/today').length;await h.advance(14999);
    assert.equal(h.requests.filter(value=>value.url==='/api/today').length,before);assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    verified=true;await h.advance(1);await settle();assert.equal(h.requests.filter(value=>value.url==='/api/today').length,before+1);
    assert.match(h.elements.app.innerHTML,/Fixture assignment/);const recovered=h.requests.length;await h.advance(60000);assert.equal(h.requests.length,recovered);
  }
  // Readiness loss from another action fences late channel names and review contents from the current read pass.
  {
    const h=harness({overrides:{'/api/service':personalService,'/api/settings':()=>response({preferences:{disabled_kinds:[]},connection:{state:'connected',teams_verified:true,retryable:false,ingestion_pending:true}})}});await settle();await h.click({detail:'fixture',source:'teams'});
    const channels=deferred(),changes=deferred(),settings=deferred();h.overrides['/api/channels']=()=>channels.promise;h.overrides['/api/changes']=()=>changes.promise;h.overrides['/api/settings']=()=>settings.promise;
    const refreshing=h.click({action:'refresh'});await settle();h.overrides['/api/events/fixture/state']=()=>unverifiedAccess(false,'reauth_required');await h.click({action:'hide-detail'});
    channels.resolve(response([{id:'late-private-channel',name:'Late private channel name',enabled:true}]));
    changes.resolve(response({items:[{id:'late-private-review',before:{title:'Late private review body',start_date:'2026-11-07',end_date:'2026-11-07'},after:{title:'Changed private review',start_date:'2026-11-07',end_date:'2026-11-08'}}]}));
    settings.resolve(accessSettings(true));await refreshing;await settle();assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);assert.match(h.elements.app.innerHTML,/다시 로그인/);
    await h.click({action:'filters'});assert.doesNotMatch(h.elements.modal.innerHTML,/Late private channel name/);await h.click({action:'close'});
    await h.click({action:'changes'});assert.doesNotMatch(h.elements.modal.innerHTML,/Late private review body|Changed private review/);assert.equal(writes(h).length,1);
    const stopped=h.requests.length;await h.advance(60000);assert.equal(h.requests.length,stopped);
  }
  // Temporary renewal preserves only the last displayed calendar; a late result cannot replace it.
  {
    let pending=false;
    const h=harness({overrides:{'/api/service':personalService,'/api/settings':()=>pending
      ? response({preferences:{disabled_kinds:[]},connection:{state:'checking',teams_verified:false,retryable:true,retain_teams_snapshot:true}})
      : accessSettings(true)}});
    await settle();await h.click({action:'calendar'});assert.match(h.elements.app.innerHTML,/Fixture assignment/);
    const late=deferred();h.overrides[calendarUrl(h,'2026-11')]=()=>late.promise;pending=true;
    const refresh=h.click({action:'refresh'});await settle();
    assert.match(h.elements.app.innerHTML,/Fixture assignment/);assert.match(h.elements.app.innerHTML,/마지막으로 확인한 일정을 유지/);
    late.resolve(response({items:[{...h.item,title:'Late unverified replacement'}]}));await refresh;await settle();
    assert.match(h.elements.app.innerHTML,/Fixture assignment/);assert.doesNotMatch(h.elements.app.innerHTML,/Late unverified replacement/);
    pending=false;h.overrides['/api/calendar']=()=>response({items:[]});delete h.overrides[calendarUrl(h,'2026-11')];
    await h.advance(15000);await settle();
    assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment|마지막으로 확인한 일정을 유지/);
    assert.equal(writes(h).length,0);
  }
  // A permission recheck keeps unsaved display settings and only restores freshly allowed channels.
  for (const retain of [true, false]) {
    let channels = [{id:'a',name:'학급 공지',enabled:true},{id:'revoked',name:'Revoked private channel',enabled:true}];
    const h = harness({overrides:{'/api/service':personalService,'/api/settings':()=>accessSettings(true),'/api/channels':()=>response(channels)}});
    await settle(); await h.click({action:'filters'});
    const view = displaySettingsForm(h);
    view.query.value = '학급'; h.input(view.query);
    view.rows[0].input.checked = false; view.rows[1].input.checked = false;
    view.kinds[0].checked = false;
    h.overrides['/api/today'] = () => response({code:'permission_check_pending',teams_access_unverified:true,
      connection_state:'checking',retryable:true,retain_teams_snapshot:retain},503);
    await h.click({action:'refresh'});
    assert.equal(h.elements.modal.open,true,'a retryable permission check must keep the settings dialog open');
    assert.equal(view.rows.length,0); assert.equal(view.apply.disabled,true);
    assert.equal(view.query.value,'학급'); assert.equal(view.kinds[0].checked,false);
    await h.submit(view.form); assert.equal(writes(h).length,0);

    view.query.value = '학급 새'; h.input(view.query);
    assert.match(view.status.textContent,/권한.*확인/);
    channels = [{id:'a',name:'학급 새 공지',enabled:true},{id:'new',name:'New allowed channel',enabled:false}];
    const settings = deferred();
    h.overrides['/api/today'] = () => response(h.today);
    h.overrides['/api/settings'] = () => settings.promise;
    const recovery = h.click({action:'refresh'}); await settle();
    assert.equal(view.rows.length,0,'channel response must wait for current permission metadata');
    settings.resolve(accessSettings(true)); await recovery;
    assert.equal(h.elements.modal.open,true); assert.equal(view.apply.disabled,false);
    assert.equal(view.query.value,'학급 새'); assert.equal(view.kinds[0].checked,false);
    assert.deepEqual(view.rows.map(row=>[row.input.value,row.input.checked,row.hidden]),[['a',false,false],['new',false,true]]);
    assert.doesNotMatch(view.list.innerHTML,/Revoked private channel/);
    h.overrides['/api/channels/a/preference'] = () => response({});
    h.overrides['/api/settings'] = () => accessSettings(true);
    await h.submit(view.form);
    assert.deepEqual(writes(h).map(write=>write.url),['/api/channels/a/preference','/api/settings']);
    assert.equal(JSON.parse(writes(h)[0].options.body).enabled,false);
    assert.deepEqual(JSON.parse(writes(h)[1].options.body).disabled_kinds,['assignment']);
  }
  // A recheck that ends in required sign-in must not keep the channel settings draft open.
  {
    const h = harness({overrides:{'/api/service':personalService,'/api/settings':()=>accessSettings(true)}});
    await settle(); await h.click({action:'filters'});
    const view = displaySettingsForm(h);
    view.query.value = 'Fixture'; h.input(view.query); view.rows[0].input.checked = false;
    h.overrides['/api/today'] = () => unverifiedAccess(true,'checking');
    await h.click({action:'refresh'});
    assert.equal(h.elements.modal.open,true);
    h.overrides['/api/today'] = () => unverifiedAccess(false,'reauth_required');
    await h.click({action:'refresh'});
    assert.equal(h.elements.modal.open,false);
    assert.equal(h.elements.modal.innerHTML,''); assert.equal(writes(h).length,0);
  }
  // The typed renewal response retains rows; subsequent definitive revocation still clears them.
  {
    const h=harness({overrides:{'/api/service':personalService}});await settle();await h.click({action:'calendar'});
    h.overrides['/api/today']=()=>response({code:'permission_check_pending',teams_access_unverified:true,
      connection_state:'checking',retryable:true,retain_teams_snapshot:true},503);
    await h.click({action:'refresh'});assert.match(h.elements.app.innerHTML,/Fixture assignment/);
    h.overrides['/api/today']=()=>unverifiedAccess(false,'reauth_required');await h.click({action:'refresh'});
    assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);assert.match(h.elements.app.innerHTML,/다시 로그인/);
  }
  // Logout clears a retained calendar instead of persisting source data across accounts.
  {
    const h=harness({overrides:{'/api/service':personalService}});await settle();await h.click({action:'calendar'});
    h.overrides['/api/today']=()=>response({code:'permission_check_pending',teams_access_unverified:true,
      connection_state:'checking',retryable:true,retain_teams_snapshot:true},503);
    await h.click({action:'refresh'});assert.match(h.elements.app.innerHTML,/Fixture assignment/);
    await h.click({action:'logout'});assert.doesNotMatch(h.elements.app.innerHTML,/Fixture assignment/);
    assert.match(h.elements.app.innerHTML,/school-signin/);
  }
  // A completed scope check removes the revoked channel while retaining the other channel's snapshot.
  {
    const h=harness({overrides:{'/api/service':personalService}});await settle();
    const allowed={...h.item,channel_id:'allowed',title:'Still authorized schedule'};
    const revoked={...h.item,id:'revoked',channel_id:'revoked',title:'Revoked channel schedule'};
    h.overrides['/api/calendar']=()=>response({items:[allowed,revoked]});await h.click({action:'refresh'});await h.click({action:'calendar'});
    h.overrides['/api/today']=()=>response({code:'permission_check_pending',teams_access_unverified:true,
      connection_state:'connected',retryable:true,retain_teams_snapshot:true,snapshot_channel_ids:['allowed']},503);
    await h.click({action:'refresh'});
    assert.match(h.elements.app.innerHTML,/Still authorized schedule/);assert.doesNotMatch(h.elements.app.innerHTML,/Revoked channel schedule/);
  }
  // A new Classroom snapshot replaces the card without redrawing the agenda.
  {
    const oldTime = Date.UTC(2026,10,7,0,0);
    let snapshot = {captured_at:oldTime,items:[{kind:'announcement',course:'Science',title:'Old Classroom notice'}]};
    const h = harness({overrides:{
      '/api/service':() => response({mode:'test-server',idle_seconds:1800,classroom_enabled:true}),
      '/api/classroom':() => response({connected:true,snapshot})
    }});
    await settle();
    const before = h.elements.app.innerHTML;
    assert.match(before,/Old Classroom notice/);
    snapshot = {captured_at:oldTime+900000,items:[
      {kind:'announcement',course:'Science',title:'New Classroom notice'},
      {kind:'assignment',course:'Science',title:'New Classroom assignment'}
    ]};
    await h.advance(30000);
    assert.match(h.elements.classroomCard.outerHTML,/New Classroom notice/);
    assert.match(h.elements.classroomCard.outerHTML,/New Classroom assignment/);
    assert.match(h.elements.classroomCard.outerHTML,/최근 동기화.*9:15/);
    assert.equal(h.elements.app.innerHTML,before);
    h.visibility(true);
    const requests = h.requests.length;
    await h.advance(60000);
    assert.equal(h.requests.length,requests);
    h.visibility(false);
    await settle();
    await h.advance(0);
    assert.equal(h.requests.at(-1).url,'/api/classroom');
  }
  // A configured Google client offers a consent button even for an existing Apps Script user.
  {
    const consentUrl = 'https://accounts.google.com/o/oauth2/v2/auth?scope=classroom';
    let navigated;
    const h = harness({overrides:{
      '/api/service':() => response({mode:'test-server',idle_seconds:1800,classroom_enabled:true,classroom_google_enabled:true}),
      '/api/classroom':() => response({connected:true,snapshot:null}),
      '/api/classroom/google/connect':() => response({authorization_url:consentUrl})
    }});
    h.context.location.assign = url => {navigated = url;};
    await settle();
    assert.match(h.elements.app.innerHTML,/Google Classroom 연결/);
    await h.click({action:'classroom-google-connect'});
    assert.equal(navigated,consentUrl);
    assert.equal(writes(h).at(-1).url,'/api/classroom/google/connect');
    assert.doesNotMatch(h.elements.modal.innerHTML,/CALENDARSYNC_TOKEN/);
  }
  console.log('Usability UI: synthetic scenarios passed (incremental snapshots, permission renewal, redaction and prior regressions).');
})().catch(error => {console.error(error); process.exitCode = 1;});
function displaySettingsForm(h) {
  let rows = [];
  const status = {textContent:''}, apply = {disabled:false,dataset:{}}, retry = {hidden:true};
  const kinds = ['assignment','application','event'].map(value=>({value,checked:true}));
  const list = {
    html:'',
    get innerHTML() {return this.html;},
    set innerHTML(value) {
      this.html = value;
      rows = [...value.matchAll(/<span>([^<]*)<\/span><input type="checkbox" name="channel" value="([^"]*)"([^>]*)>/g)].map(([,textContent,value,flags])=>{
        const checked = /\bchecked\b/.test(flags);
        return {textContent,hidden:false,input:{value,checked,defaultChecked:checked}};
      });
    },
    replaceChildren() {this.innerHTML = '';}
  };
  const query = {value:'',dataset:{channelQuery:''},closest:()=>form};
  const elements = {'#channel-search-status':status,'#filter-channel-list':list,'#filter-channel-retry':retry,'[data-channel-query]':query,'button[type=submit]':apply};
  const form = {id:'filters-form',querySelector:selector=>elements[selector],querySelectorAll(selector) {
    if (selector === '[data-channel-row]') return rows;
    if (selector === '[name=channel]') return rows.map(row=>row.input);
    return kinds;
  }};
  list.innerHTML = h.elements.modal.innerHTML;
  h.elements['filters-form'] = form;
  return {form,query,status,apply,kinds,list,get rows() {return rows;}};
}
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
  const elements = Object.fromEntries(['app', 'modal', 'topbar', 'account-name', 'nav-today', 'nav-calendar', 'toast', 'login-error', 'sync-status', 'sync-button', 'refresh-status', 'refresh-button', 'mutation-status'].map(id => {
    let html = '';
    return [id, {textContent: '', hidden: true, dataset: {}, attributes: {},
      get innerHTML() {return html;},
      set innerHTML(value) {html = value; history.push({id, html: value});},
      classList: {toggle() {}, add() {}, remove() {}},
      setAttribute(name, value) {this.attributes[name] = value;},
      removeAttribute(name) {delete this.attributes[name];},
      replaceChildren() {this.innerHTML = '';}}];
  }));
  elements.classroomCard = {outerHTML:''};
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
      querySelector(selector) {return selector === '.classroom-card' ? elements.classroomCard : selector === '[data-action="sync-teams"]' ? elements['sync-button'] : selector === '[data-action="refresh"]' ? elements['refresh-button'] : elements[selector.slice(1)] || null;},
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
    change(target) {return listeners.change({target});},
    input(target) {return listeners.input({target});},
    escape() {listeners.cancel?.({target:elements.modal}); elements.modal.close();},
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
