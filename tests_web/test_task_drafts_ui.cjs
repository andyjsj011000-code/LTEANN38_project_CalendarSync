// Synthetic personal-draft controller tests. No real accounts, storage, browser or network.
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.resolve(__dirname,'../web_service/static/beta-task-drafts.js'),'utf8');
const copy=value=>JSON.parse(JSON.stringify(value));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const settle=async()=>{for(let i=0;i<8;i++)await tick();};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const failure=status=>Object.assign(new Error(status===409?'Other tab changed draft':'Draft request failed'),{status});
const content=(title='',values={})=>({task_id:null,base_version:null,fields:{title,notes:'',start_date:'2026-11-07',end_date:'2026-11-07',time:'',display_mode:'daily',selected_dates:[]},...values});
function harness(){
  let identity={id:'account-a',epoch:1},allowed=true,dialog=1,form=content(),now=0,timerId=0,activeWrites=0,maxWrites=0;
  const timers=new Map(),stores=new Map(),requests=[],states=[],errors=[],overrides={},ignored=new Set();
  const store=id=>{if(!stores.has(id))stores.set(id,{draft:null,version:0,updated_at:null,expires_at:null});return stores.get(id);};
  const commit=request=>{
    const current=store(request.owner);
    if(request.method==='GET')return copy(current);
    const version=request.method==='DELETE'?Number(new URL('https://fixture.invalid'+request.url).searchParams.get('version')):request.options.body.version;
    if(version!==current.version)throw failure(409);
    current.version++;current.draft=request.method==='DELETE'?null:copy(request.options.body.draft);current.updated_at=now;current.expires_at=now+604800000;
    return copy(current);
  };
  const context={window:{},AbortController,setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,at:now+delay});return id;},clearTimeout:id=>timers.delete(id)};
  vm.createContext(context);vm.runInContext(source,context);
  const controller=context.window.BetaTaskDrafts.create({getIdentity:()=>identity,canPersist:()=>allowed,readDraft:()=>form,hasForm:()=>!!form,
    isDialogCurrent:value=>value===dialog,onStateChange:value=>states.push(copy(value)),onError:(error,scope)=>errors.push({error,scope}),
    async api(url,options={}){
      const request={url,options,method:options.method||'GET',owner:identity?.id};requests.push(request);
      if(request.method!=='GET'){activeWrites++;maxWrites=Math.max(maxWrites,activeWrites);}
      try {
        const value=overrides[request.method]?overrides[request.method](request):commit(request);
        if(ignored.has(request.method))return await value;
        return await new Promise((resolve,reject)=>{
          const abort=()=>reject(Object.assign(new Error('Aborted'),{name:'AbortError'}));
          if(options.signal.aborted)return abort();
          options.signal.addEventListener('abort',abort,{once:true});
          Promise.resolve(value).then(resolve,reject).finally(()=>options.signal.removeEventListener('abort',abort));
        });
      }finally{if(request.method!=='GET')activeWrites--;}
    }});
  return {controller,requests,states,errors,overrides,ignored,commit,context,
    get server(){return store(identity.id);},get form(){return form;},set form(value){form=value;},get identity(){return identity;},set identity(value){identity=value;},
    get maxWrites(){return maxWrites;},get writes(){return requests.filter(r=>r.method!=='GET');},
    allowed(value){allowed=value;},dialog(value){dialog=value;},
    async start(){return controller.start({context:dialog});},
    change(title){form.fields.title=title;controller.changed();},
    async advance(ms){const end=now+ms;for(let n=0;n<100;n++){const next=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!next){now=end;await settle();return;}const[id,t]=next;timers.delete(id);now=t.at;t.fn();await settle();}throw new Error('Timer loop');}
  };
}
let passed=0;
async function test(name,body){try{await body();passed++;}catch(error){error.message=name+': '+error.message;throw error;}}
(async()=>{
await test('defaults load metadata but never save until meaningful input changes',async()=>{
  const h=harness();await h.start();await h.advance(1000);assert.equal(h.writes.length,0);assert.equal(h.controller.getState().loaded,true);assert.equal(h.controller.hasUnsaved(),false);
  h.change('Typed');await h.advance(499);assert.equal(h.writes.length,0);assert.equal(h.controller.hasUnsaved(),true);
  await h.advance(1);assert.equal(h.writes.length,1);assert.equal(h.server.draft.fields.title,'Typed');assert.equal(h.controller.getState().phase,'saved');assert.equal(h.controller.hasUnsaved(),false);
});
await test('a prefilled new task marked unsaved is persisted without requiring another input event',async()=>{
  const h=harness();h.form=content('Recovered deleted task');h.form.fields.notes='Keep all copied details';h.form.fields.display_mode='selected';h.form.fields.selected_dates=['2026-11-07'];
  const copied=copy(h.form);await h.controller.start({context:1,unsaved:true});
  assert.equal(h.controller.hasUnsaved(),true);await h.advance(499);assert.equal(h.writes.length,0);
  await h.advance(1);assert.equal(h.writes.length,1);assert.equal(h.writes[0].options.body.version,0);
  assert.deepEqual(h.server.draft,copied);assert.equal(h.server.draft.task_id,null);assert.equal(h.server.draft.base_version,null);assert.equal(h.controller.hasUnsaved(),false);
});
await test('a prefilled unsaved copy preserves another stored draft until explicit replacement',async()=>{
  const h=harness();h.server.draft=content('Unrelated stored draft');h.server.version=4;h.form=content('Recovered deleted task');
  await h.controller.start({context:1,unsaved:true});await h.advance(1000);
  assert.equal(h.controller.getState().phase,'conflict');assert.equal(h.controller.hasUnsaved(),true);assert.equal(h.writes.length,0);
  assert.equal(h.form.fields.title,'Recovered deleted task');assert.equal(h.server.draft.fields.title,'Unrelated stored draft');
  assert.equal((await h.controller.replace()).ok,true);assert.equal(h.writes.length,1);assert.equal(h.writes[0].options.body.version,4);assert.equal(h.server.draft.fields.title,'Recovered deleted task');
});
await test('typing debounces for 500ms and only the latest fields are saved',async()=>{
  const h=harness();await h.start();h.change('A');await h.advance(400);h.change('AB');await h.advance(499);assert.equal(h.writes.length,0);
  await h.advance(1);assert.equal(h.writes.length,1);assert.equal(h.server.draft.fields.title,'AB');
});
await test('late metadata never replaces an already edited form and existing draft blocks autosave',async()=>{
  const h=harness(),read=deferred();h.server.draft=content('Stored other draft');h.server.version=4;h.overrides.GET=()=>read.promise;
  const start=h.start();h.change('Current unsaved input');await h.advance(500);assert.equal(h.writes.length,0);
  read.resolve(copy(h.server));await start;await settle();assert.equal(h.form.fields.title,'Current unsaved input');assert.equal(h.controller.getState().phase,'conflict');
  assert.equal(h.controller.getState().available,true);assert.equal(h.writes.length,0);assert.equal((await h.controller.flush()).ok,false);
});
await test('late empty metadata allows the edited snapshot without writing form defaults',async()=>{
  const h=harness(),read=deferred();h.overrides.GET=()=>read.promise;const start=h.start();h.change('Typed before GET');await h.advance(500);
  read.resolve(copy(h.server));await start;await settle();assert.equal(h.server.draft.fields.title,'Typed before GET');assert.equal(h.writes.length,1);
});
await test('resume is explicit and its token adopts only the selected server version',async()=>{
  const h=harness();h.server.draft=content('Stored draft');h.server.version=2;await h.start();
  const result=await h.controller.resume();assert.equal(h.form.fields.title,'');assert.equal(h.controller.isCurrentResume(result.token),true);
  h.form=copy(result.draft);await h.controller.start({context:1,resumeToken:result.token});h.change('Resumed edit');await h.advance(500);
  assert.equal(h.server.version,3);assert.equal(h.server.draft.fields.title,'Resumed edit');assert.equal(h.writes[0].options.body.version,2);
});
await test('replace is an explicit CAS save preserving current local input',async()=>{
  const h=harness();h.server.draft=content('Other stored draft');h.server.version=7;await h.start();h.change('My current input');assert.equal(h.controller.getState().phase,'conflict');
  const result=await h.controller.replace();assert.equal(result.ok,true);assert.equal(h.server.version,8);assert.equal(h.server.draft.fields.title,'My current input');
});
await test('overlapping edits are serialized and the second save uses the acknowledged version',async()=>{
  const h=harness(),first=deferred();await h.start();let request;
  h.overrides.PUT=value=>{request=value;delete h.overrides.PUT;return first.promise;};h.change('First');await h.advance(500);h.change('Latest');await h.advance(500);assert.equal(h.writes.length,1);
  first.resolve(h.commit(request));await settle();await h.advance(500);assert.equal(h.writes.length,2);assert.equal(h.maxWrites,1);assert.equal(h.server.draft.fields.title,'Latest');assert.equal(h.writes[1].options.body.version,1);
});
await test('flush waits for an in-flight autosave when input returns to its original baseline',async()=>{
  const h=harness(),put=deferred();h.form=content('Original task');await h.start();let request;
  h.overrides.PUT=value=>{request=value;delete h.overrides.PUT;return put.promise;};
  h.change('Intermediate edit');await h.advance(500);h.change('Original task');
  let finished=false;const flushing=h.controller.flush().then(value=>{finished=true;return value;});await settle();
  assert.equal(finished,false);assert.equal(h.writes.length,1);
  put.resolve(h.commit(request));const saved=await flushing;
  assert.equal(saved.ok,true);assert.equal(saved.clearToken.version,2);assert.equal(h.maxWrites,1);
  assert.equal(h.server.draft.fields.title,'Original task');assert.equal(h.writes.length,2);
  assert.equal((await h.controller.clearConfirmed(saved)).cleared,true);assert.equal(h.server.draft,null);
});
await test('waiting for an autosave does not authorize a flush after its form is replaced',async()=>{
  const h=harness(),put=deferred();h.form=content('Original task');await h.start();let request;
  h.overrides.PUT=value=>{request=value;return put.promise;};
  h.change('Intermediate edit');await h.advance(500);h.change('Original task');const flushing=h.controller.flush();await settle();
  h.dialog(2);h.form=content('Replacement form');await h.start();put.resolve(h.commit(request));
  const result=await flushing;assert.equal(result.ok,false);assert.equal(result.reason,'stale');
  assert.equal(h.writes.length,1);assert.equal(h.form.fields.title,'Replacement form');
});
await test('CAS409 leaves current input, stops automatic writes and requires explicit conflict handling',async()=>{
  const h=harness();await h.start();h.server.draft=content('Other tab');h.server.version=1;h.change('Keep current input');await h.advance(500);
  assert.equal(h.controller.getState().phase,'conflict');assert.equal(h.form.fields.title,'Keep current input');await h.advance(5000);assert.equal(h.writes.length,1);
  assert.equal(h.errors[0].error.status,409);assert.equal(h.controller.hasUnsaved(),true);
});
await test('ordinary failed autosave never claims saved or retries the write automatically',async()=>{
  const h=harness();await h.start();h.overrides.PUT=()=>Promise.reject(failure(503));h.change('Keep after outage');await h.advance(500);
  assert.equal(h.controller.getState().phase,'error');assert.equal(h.controller.hasUnsaved(),true);assert.equal(h.form.fields.title,'Keep after outage');await h.advance(5000);assert.equal(h.writes.length,1);
});
await test('clear after confirmed task write uses the successful draft version',async()=>{
  const h=harness();await h.start();h.change('Ready to submit');const saved=await h.controller.flush();assert.equal(saved.ok,true);assert.equal(saved.clearToken.version,1);
  const cleared=await h.controller.clearConfirmed(saved);assert.equal(cleared.ok,true);assert.equal(h.server.draft,null);assert.equal(h.server.version,2);assert.equal(h.writes[1].url,'/api/task-draft?version=1');
});
await test('a different tab newer draft survives confirmed-write cleanup',async()=>{
  const h=harness();await h.start();h.change('Submitted task');const saved=await h.controller.flush();h.server.draft=content('Newer different tab');h.server.version=2;
  const cleared=await h.controller.clearConfirmed(saved);assert.equal(cleared.ok,false);assert.equal(h.server.draft.fields.title,'Newer different tab');assert.equal(h.server.version,2);assert.equal(h.writes.length,2);
});
await test('an unrelated stored draft is never cleared when the active form has not claimed it',async()=>{
  const h=harness();h.server.draft=content('Unrelated');h.server.version=4;await h.start();const saved=await h.controller.flush();assert.equal(saved.clearToken,null);
  await h.controller.clearConfirmed(saved);assert.equal(h.writes.length,0);assert.equal(h.server.draft.fields.title,'Unrelated');
});
await test('old GET cannot replace metadata published by a newer successful save',async()=>{
  const h=harness(),put=deferred(),get=deferred();await h.start();let request;
  h.overrides.PUT=value=>{request=value;return put.promise;};h.change('New version');const saving=h.controller.flush();await settle();
  h.overrides.GET=()=>get.promise;const reading=h.controller.load({force:true});put.resolve(h.commit(request));await saving;get.resolve({draft:null,version:0});await reading;
  assert.equal(h.controller.getState().version,1);assert.equal(h.controller.getState().available,true);
});
await test('account reset aborts reads and rejects late private metadata or resume tickets',async()=>{
  const h=harness(),get=deferred();h.ignored.add('GET');h.overrides.GET=()=>get.promise;const resuming=h.controller.resume();await settle();const request=h.requests[0];
  h.controller.reset();h.identity={id:'account-b',epoch:2};get.resolve({draft:content('Old private draft'),version:8});assert.equal(await resuming,null);
  assert.equal(request.options.signal.aborted,true);assert.equal(h.controller.getState().available,false);assert.equal(h.controller.getState().loaded,false);
});
await test('late old-account PUT response cannot publish saved state or issue a new-account write',async()=>{
  const h=harness(),put=deferred();await h.start();h.ignored.add('PUT');let request;h.overrides.PUT=value=>{request=value;return put.promise;};h.change('Account A');const saved=h.controller.flush();await settle();
  h.controller.reset();h.identity={id:'account-b',epoch:2};put.resolve(h.commit(request));const result=await saved;
  assert.equal(result.ok,false);assert.equal(h.controller.getState().loaded,false);assert.equal(h.server.draft,null);assert.equal(h.writes.length,1);
});
await test('waiting old flush never continues against a replacement account form',async()=>{
  const h=harness(),get=deferred();h.ignored.add('GET');h.overrides.GET=()=>get.promise;const start=h.start();h.change('Old input');const flushing=h.controller.flush();await settle();
  h.controller.reset();h.identity={id:'account-b',epoch:2};delete h.overrides.GET;h.form=content();await h.start();
  get.resolve({draft:null,version:0});await start;assert.equal((await flushing).ok,false);assert.equal(h.writes.length,0);assert.equal(h.server.draft,null);
});
await test('hidden or unverified identity pauses persistence without losing the current fields',async()=>{
  const h=harness();await h.start();h.allowed(false);h.change('Keep while paused');await h.advance(1000);
  assert.equal(h.writes.length,0);assert.equal(h.controller.getState().phase,'paused');assert.equal(h.controller.hasUnsaved(),true);
  h.allowed(true);assert.equal((await h.controller.flush()).ok,true);assert.equal(h.server.draft.fields.title,'Keep while paused');
});
await test('identity becoming unverified while a GET waits cannot publish its draft',async()=>{
  const h=harness(),get=deferred();h.overrides.GET=()=>get.promise;const start=h.start();h.allowed(false);get.resolve({draft:content('Unverified content'),version:1});await start;
  assert.equal(h.controller.getState().available,false);assert.equal(h.controller.getState().phase,'paused');assert.equal(h.form.fields.title,'');
});
await test('Teams forms and unrelated dialogs cannot create personal draft writes',async()=>{
  const h=harness();h.form={...content('Teams text'),source:'teams'};await h.start();h.controller.changed();await h.advance(1000);assert.equal(h.writes.length,0);
  h.form=content();await h.start();h.dialog(2);h.change('Different dialog text');await h.advance(1000);assert.equal(h.writes.length,0);
});
await test('same-account new form fences an old metadata-waiting flush',async()=>{
  const h=harness(),get=deferred();h.overrides.GET=()=>get.promise;const first=h.start();h.change('First form');const flushing=h.controller.flush();await settle();
  h.dialog(2);h.form=content('Second baseline');const second=h.start();get.resolve({draft:null,version:0});await first;await second;
  assert.equal((await flushing).ok,false);assert.equal(h.writes.length,0);assert.equal(h.form.fields.title,'Second baseline');
});
await test('confirmed cleanup does not mark edits made after the submitted snapshot as saved',async()=>{
  const h=harness();await h.start();h.change('Submitted snapshot');const saved=await h.controller.flush();h.change('New unsaved text');
  await h.controller.clearConfirmed(saved);assert.equal(h.controller.hasUnsaved(),true);assert.equal(h.form.fields.title,'New unsaved text');assert.equal(h.server.draft,null);
});
await test('explicit discard deletes only the CAS version read for that action',async()=>{
  const h=harness();h.server.draft=content('Discard me');h.server.version=3;await h.controller.load();const discarded=await h.controller.discard();
  assert.equal(discarded.ok,true);assert.equal(h.server.draft,null);assert.equal(h.server.version,4);
});
await test('expired resume token is rejected after account reset',async()=>{
  const h=harness();h.server.draft=content('Old resume');h.server.version=1;const result=await h.controller.resume();h.controller.reset();h.identity={id:'account-b',epoch:2};
  assert.equal(h.controller.isCurrentResume(result.token),false);
});
await test('authentication failure is surfaced with the originating dialog context',async()=>{
  const h=harness();await h.start();h.overrides.PUT=()=>Promise.reject(failure(401));h.change('Keep until auth reset');await h.controller.flush();
  assert.equal(h.errors[0].error.status,401);assert.equal(h.errors[0].scope.context,1);h.controller.reset();assert.equal(h.controller.hasUnsaved(),false);
});

await test('old PUT acknowledgement cannot authorize submission of a replaced form',async()=>{
  const h=harness(),put=deferred();await h.start();let request;h.overrides.PUT=value=>{request=value;return put.promise;};
  h.change('Old form submitting');const flushing=h.controller.flush();await settle();h.dialog(2);h.form=content('New form baseline');await h.start();
  put.resolve(h.commit(request));const result=await flushing;assert.equal(result.ok,false);assert.equal(result.reason,'stale');assert.equal(h.form.fields.title,'New form baseline');
  assert.equal(h.controller.getState().phase,'conflict');assert.equal(h.writes.length,1);
});
await test('discard waiting for a response cannot mark a newer form edit as saved',async()=>{
  const h=harness(),remove=deferred();await h.start();h.change('Old saved');await h.controller.flush();const restored=await h.controller.resume();
  let request;h.overrides.DELETE=value=>{request=value;return remove.promise;};const discarding=h.controller.discard();await settle();
  h.dialog(2);h.form=copy(restored.draft);await h.controller.start({context:2,resumeToken:restored.token});h.change('New form unsaved');
  remove.resolve(h.commit(request));await discarding;assert.equal(h.controller.hasUnsaved(),true);assert.equal(h.form.fields.title,'New form unsaved');
});

await test('explicit resume converted to a new task persists that changed ownership context',async()=>{
  const h=harness();h.server.draft=content('Deleted original task',{task_id:'old-task',base_version:3});h.server.version=6;
  const restored=await h.controller.resume();h.form={...copy(restored.draft),task_id:null,base_version:null};
  await h.controller.start({context:1,resumeToken:restored.token});await h.advance(500);
  assert.equal(h.server.version,7);assert.equal(h.server.draft.task_id,null);assert.equal(h.server.draft.base_version,null);assert.equal(h.server.draft.fields.title,'Deleted original task');
});
console.log(`Task drafts UI: ${passed} synthetic scenarios passed (CAS, explicit restore, serial debounce, privacy and stale-response fences).`);
})().catch(error=>{console.error(error);process.exitCode=1;});
