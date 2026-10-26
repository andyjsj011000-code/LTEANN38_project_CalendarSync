"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const dir = path.resolve(__dirname, '../web_service/static');
const source = name => fs.readFileSync(path.join(dir, name), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const settle = async () => {for (let n=0; n<5; n++) await tick();};
const response = (data, status=200) => ({status, ok:status<400, async json(){return data;}});
function deferred() {let resolve; const promise = new Promise(done => {resolve=done;}); return {promise,resolve};}
function harness({signedIn=true, overrides={}}={}) {
  const listeners={}, requests=[];
  const elements=Object.fromEntries(['app','modal','topbar','account-name','nav-today','nav-calendar','toast','login-error'].map(id => [id, {innerHTML:'',textContent:'',hidden:true,classList:{toggle(){}},replaceChildren(){this.innerHTML='';}}]));
  elements.modal.close=function(){this.open=false;}; elements.modal.showModal=function(){this.open=true;};
  const item={id:'sample',source:'teams',title:'Private homework',notes:'Private notes',channel:'Class',start_date:'2026-10-20',end_date:'2026-10-25',time:null,kind:'assignment',completed:false,cancelled:false};
  const context={window:{},location:{protocol:'https:',search:''},URL,console,Intl,Date,Set,
    setTimeout(){return 1;},clearTimeout(){},
    document:{hidden:false,documentElement:{dataset:{mode:'school'}},querySelector(s){return elements[s.slice(1)]||null;},querySelectorAll(){return [];},addEventListener(type,handler){listeners[type]=handler;}},
    async fetch(url,options){
      requests.push({url,options}); if (overrides[url]) return overrides[url](options);
      if(url==='/api/service') return response({mode:'test-server',idle_seconds:1800});
      if(url==='/api/me') return response(signedIn ? {id:'user',name:'Student',csrf_token:'csrf'}:{detail:'expired'},signedIn?200:401);
      if(url==='/auth/logout') return response(null,204);
      if(url==='/api/today') return response({date:'2026-10-22',teams:[item],personal:[]});
      if(url.startsWith('/api/calendar')) return response({items:[item]});
      if(url==='/api/channels') return response([{id:'class',name:'Class',enabled:true}]);
      if(url==='/api/settings') return response({preferences:{disabled_kinds:[],morning_enabled:true,morning_time:'07:00'},connection:{state:'connected'}});
      if(url==='/api/changes') return response({items:[]});
      if(url==='/api/push') return response({configured:false,registered:false,background_enabled:true});
      throw Error('Unexpected request '+url);
    }};
  vm.createContext(context); vm.runInContext(source('beta-calendar.js'),context); vm.runInContext(source('beta.js'),context);
  return {context,listeners,requests,elements,item,
    click: data => listeners.click({target:{closest(){return {dataset:data};}}}),
    hide(){context.document.hidden=true; listeners.visibilitychange();}};
}
(async()=>{
  // A reminder response that arrives after a privacy lock must not reinsert private text.
  {
    const pending=deferred(); const h=harness({overrides:{'/api/reminders/teams/sample':()=>pending.promise}}); await settle();
    await h.click({detail:'sample',source:'teams'}); const opening=h.click({action:'reminders'}); await tick(); h.hide();
    pending.resolve(response({offsets:[-60],custom_times:[]})); await opening;
    assert.equal(h.elements.modal.open,false); assert.ok(!h.elements.modal.innerHTML.includes('Private homework'));
    assert.equal(h.elements.topbar.hidden,true);
  }
  // Late bootstrap identity responses cannot create a new load after the tab was locked.
  {
    const pending=deferred(); const h=harness({overrides:{'/api/me':()=>pending.promise}}); await settle(); h.hide();
    pending.resolve(response({id:'user',name:'Private student',csrf_token:'csrf'})); await settle();
    assert.equal(h.elements.topbar.hidden,true); assert.equal(h.elements['account-name'].textContent,'');
    assert.ok(!h.requests.some(r=>r.url==='/api/today'));
  }
  // Async click failures must reach the same 401 handler as ordinary page requests.
  {
    const h=harness({overrides:{'/api/push':()=>response({detail:'expired'},401)}}); await settle();
    await h.click({action:'notifications'});
    assert.match(h.elements.app.innerHTML,/school-signin/); assert.equal(h.elements.topbar.hidden,true);
    assert.ok(!h.elements.app.innerHTML.includes('Private homework'));
  }
  // Re-saving default checkboxes must not silently clear existing custom reminders or offsets.
  {
    const rules={offsets:[-1440,-60,-45],custom_times:['2026-12-10T12:34:56+00:00','2026-12-11T13:30:00+00:00']};
    const h=harness({overrides:{'/api/reminders/teams/sample':options=>response(options.method==='PUT'?{saved:true}:rules)}}); await settle();
    await h.click({detail:'sample',source:'teams'}); await h.click({action:'reminders'});
    const input=h.elements.modal.innerHTML.match(/name="custom" type="datetime-local" value="([^"]+)"/); assert.ok(input && input[1]);
    const form={id:'reminder-form',elements:{day:{checked:true},hour:{checked:false},custom:{value:input[1]}}};
    await h.listeners.submit({preventDefault(){},target:form});
    const posted=JSON.parse(h.requests.find(r=>r.url==='/api/reminders/teams/sample' && r.options.method==='PUT').options.body);
    assert.deepEqual(posted.custom_times,rules.custom_times); assert.deepEqual(posted.offsets,[-45,-1440]);
  }
  // Late POST completions must not start another authenticated load after a privacy lock.
  for (const kind of ['sync', 'task']) {
    const pending=deferred(), endpoint=kind==='sync'?'/api/teams/sync':'/api/tasks';
    const h=harness({overrides:{[endpoint]:()=>pending.promise}}); await settle();
    const before=h.requests.filter(r=>r.url==='/api/today').length;
    const operation=kind==='sync' ? h.click({action:'sync-teams'}) : h.listeners.submit({preventDefault(){},target:{id:'quick-form',elements:{title:{value:'Private new task'}}}});
    await tick(); h.hide(); pending.resolve(response(kind==='sync'?{state:'connected'}:{id:'new'},kind==='sync'?200:201)); await operation;
    assert.equal(h.requests.filter(r=>r.url==='/api/today').length,before);
    assert.equal(h.elements.topbar.hidden,true); assert.equal(h.elements['account-name'].textContent,'');
    assert.ok(!h.elements.app.innerHTML.includes('Private')); assert.ok(!h.elements.modal.innerHTML.includes('Private'));
  }
  // A delayed GET cannot continue its fan-out or render after a lock either.
  {
    const overrides={},h=harness({overrides}); await settle(); const pending=deferred();
    overrides['/api/today']=()=>pending.promise;
    const loading=h.click({action:'refresh'}); await tick(); h.hide(); const before=h.requests.length;
    pending.resolve(response({date:'2026-10-22',teams:[h.item],personal:[]})); await loading;
    assert.equal(h.requests.length,before); assert.equal(h.elements.topbar.hidden,true);
    assert.ok(!h.elements.app.innerHTML.includes('Private'));
  }
  // A device can be disconnected without a viewing session; the non-simple header is mandatory.
  {
    const h=harness({signedIn:false}); await settle(); assert.match(h.elements.app.innerHTML,/data-action="disconnect-device"/);
    await h.click({action:'disconnect-device'});
    const logout=h.requests.find(r=>r.url==='/auth/logout'); assert.equal(logout.options.headers['X-CalendarSync-Logout'],'1');
    assert.equal(logout.options.headers['x-csrf-token'],undefined);
  }
  // Personal is a row label, not an unsupported Teams-kind filter.
  {
    const h=harness(); await settle(); await h.click({action:'filters'});
    assert.ok(!h.elements.modal.innerHTML.includes('name="kind" value="personal"'));
  }
  console.log('UI review: delayed-response privacy locks, async 401 handling, reminder preservation and expired-session device disconnect passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
