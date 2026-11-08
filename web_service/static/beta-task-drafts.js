/* Server-backed personal-task drafts. No browser storage and no automatic form replacement. */
(() => {
  'use strict';
  const copy = value => value == null ? value : JSON.parse(JSON.stringify(value));
  const key = value => JSON.stringify(value);
  const sameIdentity = (a,b) => !!a && !!b && a.id === b.id && a.epoch === b.epoch;
  function draftOf(value) {
    if (!value || (value.source && value.source !== 'personal') || !value.fields || typeof value.fields !== 'object') return null;
    const fields = {};
    for (const name of ['title','notes','start_date','end_date','time','display_mode']) fields[name] = value.fields[name] ?? '';
    fields.selected_dates = Array.isArray(value.fields.selected_dates) ? [...value.fields.selected_dates] : [];
    return {task_id:value.task_id ?? null,base_version:value.base_version ?? null,fields};
  }
  function create({api,getIdentity,canPersist,readDraft,hasForm,isDialogCurrent=()=>true,onStateChange=()=>{},onError=()=>{}}) {
    let identity=null,generation=0,metadata=null,metadataRevision=0,readSerial=0,readPromise=null,writePromise=null;
    let form=null,formSerial=0,timer=null,phase='idle',lastError=null,forcedConflict=false;
    let controllers=new Set(),claims=new WeakMap(),resumes=new WeakMap();
    const current = token => token.generation === generation && sameIdentity(token.identity,identity) && sameIdentity(identity,getIdentity());
    const token = () => ({generation,identity:copy(identity)});
    const permitted = () => sameIdentity(identity,getIdentity()) && !!canPersist();
    const formCurrent = value => value && value === form && hasForm() && isDialogCurrent(value.context);
    const dirty = () => !!form && (form.force || key(form.desired) !== key(form.baseline));
    const conflict = () => forcedConflict || !!(form && metadata?.draft && form.ownedVersion !== metadata.version);
    const messages = {
      idle:'',loading:'임시저장 상태를 확인하고 있어요.',ready:'입력하면 임시저장돼요.',saving:'임시저장 중…',saved:'임시저장됐어요.',
      conflict:'다른 임시저장이 있어요. 현재 입력은 유지했어요. 이어 쓰거나 현재 내용으로 바꿀 수 있어요.',
      paused:'지금은 임시저장을 잠시 멈췄어요. 화면과 로그인 상태를 확인한 뒤 다시 저장해 주세요.',
      error:'임시저장하지 못했어요. 현재 입력은 유지했어요. 다시 저장해 주세요.'
    };
    function snapshot() {
      const state = conflict() ? 'conflict' : phase;
      return {phase:state,message:messages[state],loaded:metadata!==null,available:!!metadata?.draft,version:metadata?.version ?? null,
        dirty:dirty(),pending:['loading','saving'].includes(state) || !!(readPromise || writePromise || timer),blocked:['conflict','paused','error'].includes(state),context:form?.context ?? null};
    }
    const notify = () => onStateChange(snapshot());
    function reset() {
      generation++;readSerial++;clearTimeout(timer);timer=null;
      for(const controller of controllers)controller.abort();controllers=new Set();
      identity=null;metadata=null;metadataRevision++;readPromise=null;writePromise=null;form=null;phase='idle';lastError=null;forcedConflict=false;
      claims=new WeakMap();resumes=new WeakMap();notify();
    }
    function establish() {
      const value=getIdentity();
      if(!value?.id)return false;
      if(!sameIdentity(value,identity)){reset();identity={id:value.id,epoch:value.epoch};}
      return true;
    }
    function capture() {
      if(formCurrent(form)){const value=draftOf(readDraft());if(value)form.desired=value;}
    }
    function issue(error,scope,context=form?.context ?? null) {
      if(!current(scope))return;
      lastError=error;phase=error.draftPaused ? 'paused' : error.status===409 ? 'conflict' : 'error';
      if(error.status===409)forcedConflict=true;
      notify();
      if(!error.draftPaused && error.name!=='AbortError')onError(error,{context,phase});
    }
    function pausedError(){const error=new Error(messages.paused);error.draftPaused=true;return error;}
    async function request(path,options,scope) {
      if(!current(scope)||!permitted())throw pausedError();
      const controller=new AbortController();controllers.add(controller);
      try {
        const result=await api(path,{...options,signal:controller.signal});
        if(!current(scope)||!permitted())throw pausedError();
        return result;
      } finally {controllers.delete(controller);}
    }
    function applyMetadata(value) {
      if(!value || !Number.isInteger(value.version) || value.version<0 || (value.draft!==null && !draftOf(value.draft)))throw new Error('임시저장 응답을 확인하지 못했어요.');
      if(metadata && value.version<metadata.version)return;
      metadata={...value,draft:draftOf(value.draft)};metadataRevision++;
    }
    async function load({force=false}={}) {
      if(!establish())return snapshot();
      if(!permitted()){phase='paused';notify();return snapshot();}
      if(readPromise)return readPromise;
      if(metadata && !force)return snapshot();
      const scope=token(),serial=++readSerial,revision=metadataRevision;
      phase='loading';lastError=null;notify();
      const operation=(async()=>{
        try {
          const value=await request('/api/task-draft',{},scope);
          if(!current(scope)||serial!==readSerial)return snapshot();
          if(revision===metadataRevision)applyMetadata(value);
          forcedConflict=false;phase=conflict() ? 'conflict' : 'ready';notify();return snapshot();
        } catch(error){issue(error,scope);return snapshot();}
        finally {if(current(scope)&&serial===readSerial){readPromise=null;notify();}}
      })();
      readPromise=operation;return operation;
    }
    function result(ok,reason) {
      let clearToken=null;
      if(ok && form && metadata?.draft && form.ownedVersion===metadata.version) {
        clearToken=Object.freeze({version:metadata.version});claims.set(clearToken,{...token(),version:metadata.version,formSerial:form.serial,contentKey:key(form.baseline)});
      }
      return {ok,...(reason ? {reason} : {}),clearToken};
    }
    async function serialWrite(operation) {
      while(writePromise)await writePromise;
      const promise=operation();writePromise=promise;notify();
      try{return await promise;}
      finally {if(writePromise===promise){writePromise=null;notify();}}
    }
    async function saveOne() {
      const originalScope=token(),originalForm=form;
      if(!metadata)await load();
      return serialWrite(async()=>{
        if(!current(originalScope)||form!==originalForm)return result(false,'stale');
        capture();
        if(!form || !dirty())return result(true);
        if(!metadata || lastError)return result(false,'unavailable');
        if(conflict())return result(false,'conflict');
        if(!permitted() || !formCurrent(form)){phase='paused';notify();return result(false,'paused');}
        const owner=form,scope=token(),content=copy(form.desired),version=metadata.version;
        phase='saving';notify();
        try {
          const value=await request('/api/task-draft',{method:'PUT',body:{version,draft:content}},scope);
          if(!current(scope))return result(false,'stale');
          applyMetadata(value);
          if(form===owner){owner.baseline=content;owner.force=false;owner.ownedVersion=value.version;}
          phase='saved';lastError=null;forcedConflict=false;notify();return form===owner && formCurrent(owner) ? result(true) : result(false,'stale');
        } catch(error){issue(error,scope,owner.context);return result(false,error.status===409 ? 'conflict' : 'error');}
      });
    }
    function schedule() {
      clearTimeout(timer);timer=null;
      if(!dirty() || conflict() || lastError || !formCurrent(form))return;
      if(!permitted()){phase='paused';notify();return;}
      const scope=token(),owner=form;
      timer=setTimeout(async()=>{
        timer=null;
        if(!current(scope)||form!==owner)return;
        await saveOne();
        if(current(scope)&&form===owner){schedule();notify();}
      },500);
      notify();
    }
    async function start({context,resumeToken=null,unsaved=false}={}) {
      if(!establish())return snapshot();
      clearTimeout(timer);timer=null;
      const value=draftOf(readDraft());
      if(!hasForm() || !value){form=null;notify();return snapshot();}
      const resumed=resumes.get(resumeToken),adopt=resumed && current(resumed) && metadata?.version===resumed.version;
      form={serial:++formSerial,context,baseline:adopt ? copy(resumed.draft) : copy(value),desired:copy(value),force:unsaved===true,ownedVersion:adopt ? resumed.version : null};
      lastError=null;forcedConflict=false;phase=adopt ? 'saved' : 'ready';notify();
      const scope=token(),owner=form;
      await load();if(current(scope)&&form===owner)schedule();return snapshot();
    }
    function changed(){if(!establish() || !formCurrent(form))return;capture();schedule();notify();}
    async function flush() {
      if(!establish())return result(false,'signed-out');
      const initialScope=token(),initialForm=form;
      clearTimeout(timer);timer=null;capture();
      while(writePromise) {
        await writePromise;
        if(!current(initialScope)||form!==initialForm)return result(false,'stale');
      }
      capture();
      if(lastError){lastError=null;await load({force:true});}
      if(!metadata)await load();
      if(!current(initialScope)||form!==initialForm)return result(false,'stale');
      if(!form || !dirty())return result(true);
      if(conflict())return result(false,'conflict');
      const owner=form,scope=token();
      do {
        const saved=await saveOne();
        if(!saved.ok)return saved;
        capture();
      } while(current(scope)&&form===owner&&dirty());
      return current(scope) && form===owner && formCurrent(owner) ? result(true) : result(false,'stale');
    }
    async function resume() {
      if(!establish())return null;
      const scope=token();
      await load({force:true});
      if(!current(scope))return null;
      if(!metadata?.draft || lastError || !permitted())return null;
      const resumeToken=Object.freeze({version:metadata.version});
      resumes.set(resumeToken,{...token(),version:metadata.version,draft:copy(metadata.draft)});
      return {draft:copy(metadata.draft),version:metadata.version,token:resumeToken};
    }
    const isCurrentResume = value => {const saved=resumes.get(value);return !!saved && current(saved) && permitted();};
    async function replace() {
      if(!establish() || !formCurrent(form))return result(false,'no-form');
      const scope=token(),owner=form;
      capture();await load({force:true});
      if(!current(scope)||form!==owner)return result(false,'stale');
      if(!metadata || lastError || !permitted())return result(false,'unavailable');
      form.ownedVersion=metadata.version;form.force=true;forcedConflict=false;lastError=null;
      return flush();
    }
    async function clearVersion(saved,confirmed=false) {
      clearTimeout(timer);timer=null;
      return serialWrite(async()=>{
        if(!current(saved) || !permitted())return {ok:false,reason:'stale'};
        if(metadata && metadata.version!==saved.version){forcedConflict=true;phase='conflict';notify();return {ok:false,reason:'conflict'};}
        const owner=form;
        phase='saving';notify();
        try {
          const value=await request(`/api/task-draft?version=${saved.version}`,{method:'DELETE'},saved);
          if(!current(saved))return {ok:false,reason:'stale'};
          applyMetadata(value);lastError=null;forcedConflict=false;phase='ready';
          if(form===owner && owner && owner.ownedVersion===saved.version){
            owner.ownedVersion=null;
            if(owner.serial===saved.formSerial && key(owner.desired)===saved.contentKey){owner.baseline=copy(owner.desired);owner.force=false;}
          }
          notify();return {ok:true,cleared:true};
        } catch(error){issue(error,saved,owner?.context);return {ok:false,reason:error.status===409 ? 'conflict' : 'error',confirmed};}
      });
    }
    async function clearConfirmed(value) {
      const clearToken=value?.clearToken || value,saved=claims.get(clearToken);
      if(!saved)return {ok:true,cleared:false};
      return clearVersion(saved,true);
    }
    async function discard() {
      if(!establish())return {ok:false,reason:'signed-out'};
      capture();
      const scope=token(),owner=form,contentKey=key(form?.desired);
      await load({force:true});
      if(!current(scope))return {ok:false,reason:'stale'};
      if(!metadata || lastError || !permitted())return {ok:false,reason:'unavailable'};
      if(!metadata.draft)return {ok:true,cleared:false};
      return clearVersion({...token(),version:metadata.version,formSerial:owner?.serial,contentKey});
    }
    return {load,start,changed,flush,resume,replace,discard,clearConfirmed,isCurrentResume,reset,
      getState:snapshot,hasUnsaved:()=>dirty() || !!writePromise};
  }
  window.BetaTaskDrafts=Object.freeze({create});
})();
