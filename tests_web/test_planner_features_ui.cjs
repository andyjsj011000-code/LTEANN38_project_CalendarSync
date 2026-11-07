'use strict';
// Planner integration regressions with synthetic accounts and in-memory HTTP responses.
// The fixture is adapted from test_usability_ui.cjs; existing tests remain unchanged.
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
  const addListener=(type,handler)=>(listeners[type] ||= []).push(handler);
  const dispatch=async(type,event={})=>Promise.all((listeners[type]||[]).map(fn=>fn(event)));
  const epoch = Date.UTC(2026, 10, 7, 0, 0);
  class ClockDate extends Date {
    constructor(...args) {super(...(args.length ? args : [epoch + now]));}
    static now() {return epoch + now;}
  }
  const elements = Object.fromEntries(['app', 'modal', 'topbar', 'account-name', 'nav-today', 'nav-calendar', 'toast', 'login-error', 'sync-status', 'sync-button', 'refresh-status', 'refresh-button', 'mutation-status', 'planner-sync-status', 'task-draft-status', 'task-draft-actions', 'pick-date'].map(id => {
    let html = '';
    return [id, {id, textContent: '', hidden: true, dataset: {}, attributes: {}, isConnected:true,
      addEventListener: addListener, removeEventListener(type,fn){listeners[type]=(listeners[type]||[]).filter(x=>x!==fn);},
      contains(target){return target?.isConnected !== false;},
      querySelector(selector){return context.document.querySelector(selector);},
      get innerHTML() {return html;},
      set innerHTML(value) {html = value; history.push({id, html: value}); if (id==='app') updateControls(value);},
      classList: {toggle() {}, add() {}, remove() {}},
      setAttribute(name, value) {this.attributes[name] = value;},
      removeAttribute(name) {delete this.attributes[name];},
      replaceChildren() {this.innerHTML = '';}}];
  }));
  function field(id,value='',dataset={}){return {id,value,dataset,isConnected:true,checked:false,selectionStart:0,selectionEnd:0,selectionDirection:'none',closest(){return this;},focus(){context.document.activeElement=this;},setSelectionRange(a,b,d){this.selectionStart=a;this.selectionEnd=b;this.selectionDirection=d;}};}
  function updateControls(html){
    for(const id of ['list-query','list-unfinished','list-clear'])if(elements[id]){elements[id].isConnected=false;delete elements[id];}
    if(html.includes('data-list-query')){
      const value=(html.match(/data-list-query[^>]*value="([^"]*)"/)||[])[1]||'';
      elements['list-query']=field('list-query',value,{listQuery:''});
      elements['list-unfinished']=field('list-unfinished','',{listUnfinished:''});elements['list-unfinished'].checked=/data-list-unfinished checked/.test(html);
      elements['list-clear']=field('list-clear','',{listClear:''});
    }
  }
  elements.modal.close = function () {this.open = false;};
  elements.modal.showModal = function () {this.open = true;};
  const item = {id: 'fixture', source: 'teams', title: 'Fixture assignment', notes: 'Fixture notes', channel: 'Fixture channel',
    start_date: '2026-11-07', end_date: '2026-11-07', time: null, kind: 'assignment', completed: false, cancelled: false};
  const today = {date: '2026-11-07', teams: [item], personal: []};
  const context = {window: {}, location: {protocol: 'https:', search: ''}, URL, console, Intl, Date: ClockDate, Set, Map,
    AbortController, DOMException,
    FormData:class{constructor(form){this.rows=Object.entries(form.elements).map(([k,v])=>[k,v.value]);}[Symbol.iterator](){return this.rows[Symbol.iterator]();}},
    setTimeout(callback, delay = 0) {const id = ++timerId; timers.set(id, {callback, at: now + Number(delay)}); return id;},
    clearTimeout(id) {timers.delete(id);},
    document: {hidden: false, activeElement:null, contains:target=>target?.isConnected!==false, documentElement: {dataset: {mode: 'school'}},
      querySelector(selector) {return selector === '[data-action="sync-teams"]' ? elements['sync-button'] : selector === '[data-action="refresh"]' ? elements['refresh-button'] : selector === '[data-list-query]' ? elements['list-query'] : selector === '[data-list-unfinished]' ? elements['list-unfinished'] : elements[selector.slice(1)] || null;},
      querySelectorAll(selector) {return selector === '[data-locked]' ? controls.filter(control => control.dataset.locked) : selector === 'button,input,select,textarea' ? controls : [];},
      addEventListener: addListener, removeEventListener(type,fn){listeners[type]=(listeners[type]||[]).filter(x=>x!==fn);}},
    async fetch(url, options = {}) {
      requests.push({url, options});
      let result;
      if (overrides[url]) result = overrides[url](options);
      else if (url.startsWith('/api/calendar?') && overrides['/api/calendar']) result = overrides['/api/calendar'](options, url);
      else if (url === '/api/service') result = response({mode: 'test-server', idle_seconds: 1800, personal_agenda:true, planner_enhancements:true});
      else if (url === '/api/me') result = response({id: 'fixture-user', name: 'Fixture student', csrf_token: 'fixture-csrf'});
      else if (url === '/api/personal/today') result = response({date:today.date,personal:today.personal});
      else if (url === '/api/task-draft') result = response({draft:null,version:0,updated_at:null,expires_at:null});
      else if (url === '/api/task-trash') result = response({items:[],retention_days:7});
      else if (url === '/api/teams/ingestion-status') result = response({state:'idle',last_success_at:null,channels_total:0,channels_checked:0});
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
  for(const node of Object.values(elements))node.ownerDocument=context.document;
  vm.createContext(context);
  vm.runInContext(source('beta-calendar.js'), context);
  vm.runInContext(source('beta-list-controls.js'), context);
  vm.runInContext(source('beta-task-drafts.js'), context);
  vm.runInContext(source('beta.js'), context);
  return {elements, requests, history, controls, overrides, item, today, context,
    async query(value){const target=elements['list-query'];assert.ok(target,'Search controls must be rendered');target.value=value;return dispatch('input',{target});},
    async unfinished(value=true){const target=elements['list-unfinished'];assert.ok(target,'Unfinished control must be rendered');target.checked=value;return dispatch('change',{target});},
    async input(target,value){target.value=value;return dispatch('input',{target});},
    form(values={}){const form={id:'task-form',isConnected:true,elements:{}};for(const [name,value] of Object.entries({title:'Created fixture',notes:'Draft notes',start_date:'2026-11-07',end_date:'2026-11-07',time:'',display_mode:'daily',...values})){const node=field(name,value);node.form=form;node.closest=()=>form;form.elements[name]=node;}elements['task-form']=form;elements['form-error']={textContent:''};elements['date-chips']={innerHTML:''};elements['selected-date-field']={hidden:true};elements['pick-date']={value:''};return form;},
    click(data) {return dispatch('click',{preventDefault() {}, target: {closest() {return {dataset: data, disabled: false};}}});},
    submit(form) {return dispatch('submit',{preventDefault() {}, target: form});},
    change(target) {return dispatch('change',{target});},
    escape() {dispatch('cancel',{target:elements.modal}); elements.modal.close();},
    visibility(hidden) {context.document.hidden = hidden; return dispatch('visibilitychange');},
    activity(type = 'pointerdown') {return dispatch(type,{});},
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

const personalTask=(id,values={})=>({id,source:'personal',kind:'personal',title:'Personal '+id,notes:'Fixture notes',
  start_date:'2026-11-07',end_date:'2026-11-07',time:null,display_mode:'daily',selected_dates:[],version:1,
  hidden:false,completed:false,cancelled:false,...values});
function planner(items,extra={}){
  const data={items};
  const h=harness({overrides:{
    '/api/personal/today':()=>response({date:'2026-11-07',personal:data.items.filter(x=>x.source==='personal')}),
    '/api/today':()=>response({date:'2026-11-07',teams:data.items.filter(x=>x.source==='teams'),personal:data.items.filter(x=>x.source==='personal')}),
    '/api/calendar':()=>response({items:data.items}),...extra.overrides},...Object.fromEntries(Object.entries(extra).filter(([k])=>k!=='overrides'))});
  return {...h,data};
}
const cases=[];
const test=(name,body)=>cases.push({name,body});

test('search keeps the original collection and identifies the actual selected item',async()=>{
  const items=[personalTask('a',{title:'Alpha homework'}),personalTask('b',{title:'Beta essay'})],h=planner(items);await settle();
  const before=h.requests.length;await h.query('Beta');
  assert.match(h.elements.app.innerHTML,/Beta essay/);assert.doesNotMatch(h.elements.app.innerHTML,/Alpha homework/);
  assert.equal(h.requests.length,before);assert.equal(items.length,2);assert.equal(items[0].id,'a');
  await h.click({detail:'b',source:'personal'});assert.match(h.elements.modal.innerHTML,/Beta essay/);
  await h.click({action:'close'});await h.query('');assert.match(h.elements.app.innerHTML,/Alpha homework/);assert.match(h.elements.app.innerHTML,/Beta essay/);
});
test('unfinished completion removes only the confirmed ID and leaves the other row available',async()=>{
  const first=personalTask('a'),second=personalTask('b'),h=planner([first,second]);await settle();await h.unfinished();
  h.overrides['/api/tasks/a/state']=options=>{const result={...first,...JSON.parse(options.body),version:2};h.data.items=[result,second];return response(result);};
  await h.change({dataset:{complete:'a',source:'personal'},checked:true});
  assert.doesNotMatch(h.elements.app.innerHTML,/data-complete="a"/);assert.match(h.elements.app.innerHTML,/data-complete="b"/);
  await h.unfinished(false);assert.match(h.elements.app.innerHTML,/data-complete="a" data-source="personal" checked/);
  assert.equal(writes(h).filter(x=>x.url==='/api/tasks/a/state').length,1);
});
test('hidden items never appear in filtered Today or calendar rows',async()=>{
  const h=planner([personalTask('a'),personalTask('secret',{title:'Hidden private item',hidden:true})]);await settle();
  assert.doesNotMatch(h.elements.app.innerHTML,/Hidden private item/);await h.query('Hidden');assert.doesNotMatch(h.elements.app.innerHTML,/Hidden private item/);
  await h.click({action:'calendar'});assert.doesNotMatch(h.elements.app.innerHTML,/Hidden private item/);
});
test('calendar search covers both range bars and single-day rows and keeps the chosen month',async()=>{
  const h=planner([personalTask('a',{title:'Alpha single'}),personalTask('b',{title:'Beta range',start_date:'2026-11-06',end_date:'2026-11-09'})]);await settle();
  await h.click({action:'calendar'});await h.query('Beta');assert.match(h.elements.app.innerHTML,/Beta range/);assert.doesNotMatch(h.elements.app.innerHTML,/Alpha single/);
  h.overrides['/api/calendar']=(_o,url)=>{assert.match(url,/start=2026-11-29&end=2027-01-09/);return response({items:[personalTask('dec',{title:'Beta December',start_date:'2026-12-01',end_date:'2026-12-01'})]});};
  await h.click({action:'next-month'});assert.match(h.elements.app.innerHTML,/Beta December/);assert.doesNotMatch(h.elements.app.innerHTML,/Beta range/);
});
test('logout clears search and detached input cannot publish into the signed-out screen',async()=>{
  const h=planner([personalTask('a')]);await settle();await h.query('Personal');const old=h.elements['list-query'];
  await h.click({action:'logout'});const before=h.requests.length;await h.input(old,'Old private query');
  assert.match(h.elements.app.innerHTML,/school-signin/);assert.doesNotMatch(h.elements.app.innerHTML,/Old private query|data-list-query/);assert.equal(h.requests.length,before);
});
test('confirmed trash deletion immediately removes the task while the following read is pending',async()=>{
  const task=personalTask('a'),h=planner([task]);await settle();await h.click({detail:'a',source:'personal'});await h.click({action:'delete-detail'});
  const read=deferred();h.overrides['/api/today']=()=>read.promise;
  h.overrides['/api/tasks/a/trash']=()=>{h.data.items=[];return response({id:'trash-a',task_id:'a',deleted_at:1794009600,expires_at:1794614400});};
  const deletion=h.click({action:'confirm-delete'});await settle();
  assert.doesNotMatch(h.elements.app.innerHTML,/data-complete="a"/);assert.match(h.elements.app.innerHTML,/undo-trash/);
  read.resolve(response({date:'2026-11-07',teams:[],personal:[]}));await deletion;
  assert.equal(writes(h).filter(x=>x.url==='/api/tasks/a/trash').length,1);
});
test('undo uses the confirmed trash nonce and restored task version in the next write',async()=>{
  const original=personalTask('a'),h=planner([original]);await settle();
  h.overrides['/api/tasks/a/trash']=()=>{h.data.items=[];return response({id:'nonce-one',task_id:'a'});};
  await h.click({detail:'a',source:'personal'});await h.click({action:'delete-detail'});await h.click({action:'confirm-delete'});
  h.overrides['/api/task-trash/nonce-one/restore']=()=>{const result={...original,version:2};h.data.items=[result];return response(result);};
  await h.click({action:'undo-trash'});assert.match(h.elements.app.innerHTML,/data-complete="a"/);
  h.overrides['/api/tasks/a/state']=options=>{assert.equal(JSON.parse(options.body).version,2);const result={...original,completed:true,version:3};h.data.items=[result];return response(result);};
  await h.change({dataset:{complete:'a',source:'personal'},checked:true});
  assert.equal(writes(h).filter(x=>x.url==='/api/task-trash/nonce-one/restore').length,1);
});
test('a delayed trash list cannot replace a newly opened personal form',async()=>{
  const list=deferred(),h=planner([personalTask('a')],{overrides:{'/api/task-trash':()=>list.promise}});await settle();
  const opening=h.click({action:'task-trash'});await settle();await h.click({action:'new'});h.elements.modal.innerHTML+='<textarea>Unsent newer draft</textarea>';
  list.resolve(response({items:[{id:'old',task_id:'old-task',title:'Older deleted item'}],retention_days:7}));await opening;
  assert.match(h.elements.modal.innerHTML,/Unsent newer draft/);assert.doesNotMatch(h.elements.modal.innerHTML,/Older deleted item/);
});
test('logout rejects a delayed trash list even when fetch ignores cancellation',async()=>{
  const list=deferred(),h=planner([personalTask('a')],{ignoreAbort:new Set(['/api/task-trash']),overrides:{'/api/task-trash':()=>list.promise}});await settle();
  const opening=h.click({action:'task-trash'});await settle();await h.click({action:'logout'});
  list.resolve(response({items:[{id:'old',task_id:'old-task',title:'Private deleted item'}]}));await opening;
  assert.match(h.elements.app.innerHTML,/school-signin/);assert.doesNotMatch(h.elements.modal.innerHTML,/Private deleted item/);assert.notEqual(h.elements.modal.open,true);
});
test('logout rejects a confirmed restore arriving after account clear',async()=>{
  const task=personalTask('a'),pending=deferred(),h=planner([],{ignoreAbort:new Set(['/api/task-trash/t1/restore']),overrides:{
    '/api/task-trash':()=>response({items:[{id:'t1',task_id:'a',title:task.title,start_date:task.start_date,end_date:task.end_date,deleted_at:1794009600,expires_at:1794614400}],retention_days:7}),
    '/api/task-trash/t1/restore':()=>pending.promise}});await settle();await h.click({action:'task-trash'});
  const restored=h.click({action:'restore-trash',trashId:'t1'});await settle();await h.click({action:'logout'});
  pending.resolve(response({...task,version:2}));await restored;
  assert.match(h.elements.app.innerHTML,/school-signin/);assert.doesNotMatch(h.elements.app.innerHTML,/Personal a/);
});

test('status displays confirmed channel counts and the last whole-collection success',async()=>{
  const success=1794009600,h=planner([personalTask('a')],{overrides:{'/api/teams/ingestion-status':()=>response({state:'partial',last_success_at:success,counts:{channels_checked:2,channels_total:5,messages_failed:1}})}});await settle();
  const text=h.elements['planner-sync-status'].textContent;
  assert.match(text,/2\/5/);assert.match(text,/공지 1개/);assert.match(text,/마지막 전체 수집 성공/);
  assert.ok(text.includes(new Intl.DateTimeFormat('ko-KR',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Seoul'}).format(new Date(success*1000))));
});
test('a late status response cannot update the old account after logout',async()=>{
  const pending=deferred(),h=planner([personalTask('a')],{ignoreAbort:new Set(['/api/teams/ingestion-status']),overrides:{'/api/teams/ingestion-status':()=>pending.promise}});await settle();
  assert.ok(h.requests.some(x=>x.url==='/api/teams/ingestion-status'));await h.click({action:'logout'});
  const previous=h.elements['planner-sync-status'].textContent;pending.resolve(response({state:'running',last_success_at:1794009600,counts:{channels_checked:987,channels_total:999}}));await settle();
  assert.equal(h.elements['planner-sync-status'].textContent,previous);assert.match(h.elements.app.innerHTML,/school-signin/);
});
test('form submission waits for draft persistence and clears that revision only after the task succeeds',async()=>{
  const pending=deferred(),h=planner([]);await settle();const form=h.form({title:''});await h.click({action:'new'});await settle();
  let submitted=null;
  h.overrides['/api/task-draft']=options=>{if(!options.method)return response({draft:null,version:0});submitted=JSON.parse(options.body);return pending.promise;};
  h.overrides['/api/tasks']=options=>{const task=personalTask('created',JSON.parse(options.body));h.data.items=[task];return response(task,201);};
  h.overrides['/api/task-draft?version=1']=options=>{assert.equal(options.method,'DELETE');assert.ok(h.requests.some(x=>x.url==='/api/tasks'&&x.options.method==='POST'));return response({draft:null,version:2});};
  await h.input(form.elements.title,'Saved after draft');const saving=h.submit(form);await settle();
  assert.ok(submitted);assert.equal(submitted.draft.fields.title,'Saved after draft');assert.equal(h.requests.filter(x=>x.url==='/api/tasks').length,0);
  pending.resolve(response({draft:submitted.draft,version:1,updated_at:1794009600,expires_at:1794614400}));await saving;await settle();
  assert.match(h.elements.app.innerHTML,/Saved after draft/);assert.equal(h.requests.filter(x=>x.url==='/api/task-draft?version=1').length,1);
});
test('stale personal edit preserves its draft and never sends the clear request',async()=>{
  const item=personalTask('a'),h=planner([item]);await settle();const form=h.form({title:item.title});
  await h.click({detail:'a',source:'personal'});await h.click({action:'edit-detail'});await settle();
  h.overrides['/api/task-draft']=options=>{if(!options.method)return response({draft:null,version:0});const body=JSON.parse(options.body);return response({draft:body.draft,version:1,updated_at:1794009600,expires_at:1794614400});};
  h.overrides['/api/tasks/a']=options=>{assert.equal(JSON.parse(options.body).version,1);return response({detail:'changed'},409);};
  await h.input(form.elements.title,'Keep rejected edit');await h.submit(form);await settle();
  assert.equal(form.elements.title.value,'Keep rejected edit');assert.equal(h.elements.modal.open,true);
  assert.equal(h.requests.filter(x=>x.url.startsWith('/api/task-draft?')).length,0);
  assert.ok(h.requests.some(x=>x.url==='/api/task-draft'&&x.options.method==='PUT'));
});
test('logout during a draft flush prevents both the task write and late draft resurrection',async()=>{
  const pending=deferred(),h=planner([],{ignoreAbort:new Set(['/api/task-draft'])});await settle();const form=h.form({title:''});await h.click({action:'new'});await settle();
  let content;h.overrides['/api/task-draft']=options=>{content=JSON.parse(options.body).draft;return pending.promise;};
  await h.input(form.elements.title,'Private before logout');const saving=h.submit(form);await settle();await h.click({action:'logout'});
  pending.resolve(response({draft:content,version:1}));await saving;await settle();
  assert.equal(h.requests.filter(x=>x.url==='/api/tasks').length,0);assert.match(h.elements.app.innerHTML,/school-signin/);
  assert.doesNotMatch(h.elements.app.innerHTML,/Private before logout|draft-resume/);
});

test('feature-disabled deployments keep the old UI and make no planner requests',async()=>{
  const h=planner([personalTask('a')],{overrides:{'/api/service':()=>response({mode:'test-server',idle_seconds:1800,personal_agenda:true,planner_enhancements:false})}});await settle();
  assert.doesNotMatch(h.elements.app.innerHTML,/data-list-query|task-trash|planner-sync-status/);
  assert.equal(h.requests.filter(x=>/task-draft|task-trash|ingestion-status/.test(x.url)).length,0);
});
test('a newer-tab draft clear conflict preserves the confirmed task and does not retry the clear',async()=>{
  const h=planner([]);await settle();const form=h.form({title:''});await h.click({action:'new'});await settle();
  h.overrides['/api/task-draft']=options=>{if(!options.method)return response({draft:null,version:0});const body=JSON.parse(options.body);return response({draft:body.draft,version:1});};
  h.overrides['/api/tasks']=options=>{const task=personalTask('saved',JSON.parse(options.body));h.data.items=[task];return response(task,201);};
  h.overrides['/api/task-draft?version=1']=()=>response({detail:'Another tab wrote a newer draft'},409);
  await h.input(form.elements.title,'Confirmed task despite draft conflict');await h.submit(form);await settle();
  assert.match(h.elements.app.innerHTML,/Confirmed task despite draft conflict/);
  assert.equal(h.requests.filter(x=>x.url==='/api/task-draft?version=1').length,1);
  assert.equal(h.requests.filter(x=>x.url==='/api/tasks'&&x.options.method==='POST').length,1);
  await h.advance(5000);assert.equal(h.requests.filter(x=>x.url==='/api/task-draft?version=1').length,1);
});

(async()=>{
  let passed=0;
  for(const {name,body} of cases){try{await body();passed++;}catch(error){console.error('FAIL: '+name);console.error(error);process.exitCode=1;}}
  console.log(`Planner UI integration: ${passed}/${cases.length} synthetic scenarios passed.`);
})();
