// Isolated synthetic tests: no service requests, browser storage, or real account data.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = {window:{},fetch(){throw new Error('List controls must never query the server');}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../web_service/static/beta-list-controls.js'),'utf8'),context);
const L=context.window.BetaListControls;
const item=(id,values={})=>Object.freeze({id,source:'teams',title:'과제 제출',notes:'준비물 안내',channel:'국어',completed:false,cancelled:false,hidden:false,...values});
const ids=items=>Array.from(items,x=>x.id);
let count=0;
function test(name,body){body();count++;}

test('title, notes, channel, Unicode normalization and AND terms',()=>{
  const items=Object.freeze([item('a',{title:'Essay ＡＢＣ',notes:'수요일 준비물',channel:'영어'}),item('b',{title:'국어 과제',notes:'수요일 독후감'})]);
  assert.deepEqual(ids(L.filter(items,{query:'abc'})),['a']);
  assert.deepEqual(ids(L.filter(items,{query:'영어 수요일'})),['a']);
  assert.deepEqual(ids(L.filter(items,{query:'  국어\n독후감  '})),['b']);
  assert.deepEqual(ids(L.filter(items,{query:'abc 국어'})),[]);
});
test('only supplied loaded items are searched; hidden items never leak',()=>{
  const loaded=Object.freeze([item('allowed'),item('hidden',{title:'Private hidden match',hidden:true})]);
  assert.deepEqual(ids(L.filter(loaded,{query:'Private'})),[]);
  assert.deepEqual(ids(L.filter(loaded,L.createState())),['allowed']);
  assert.deepEqual(ids(L.filter([], {query:'과제'})),[]);
  assert.deepEqual(ids(L.filter(null,{})),[]);
});
test('unfinished excludes completed and cancelled but default view keeps them',()=>{
  const items=Object.freeze([item('open'),item('done',{completed:true}),item('cancelled',{cancelled:true})]);
  assert.deepEqual(ids(L.filter(items,{})),['open','done','cancelled']);
  assert.deepEqual(ids(L.filter(items,{unfinishedOnly:true})),['open']);
  assert.equal(items[1].completed,true);
});
test('search and unfinished combine without reordering or mutating references',()=>{
  const items=Object.freeze([item('second',{title:'Essay 2'}),item('first',{title:'Essay 1'}),item('done',{title:'Essay 0',completed:true})]);
  const result=L.filter(items,{query:'essay',unfinishedOnly:true});
  assert.deepEqual(ids(result),['second','first']);assert.equal(result[0],items[0]);assert.equal(result[1],items[1]);
  assert.equal(items.length,3);
});
test('completing one filtered ID removes that item and preserves the next original ID',()=>{
  const first=item('first'),second=item('second'),original=Object.freeze([first,second]);
  const target=L.filter(original,{unfinishedOnly:true})[0];
  const confirmed=original.map(value=>value.id===target.id ? {...value,completed:true} : value);
  const visible=L.filter(confirmed,{unfinishedOnly:true});
  assert.deepEqual(ids(visible),['second']);assert.equal(visible[0],second);assert.equal(first.completed,false);
});
test('new account state is independent and contains no loaded item cache',()=>{
  const first=L.createState(),next=L.createState();first.query='Old account query';first.unfinishedOnly=true;
  assert.equal(next.query,'');assert.equal(next.unfinishedOnly,false);assert.deepEqual(Object.keys(next),['query','unfinishedOnly']);
});
test('empty-source and zero-search-result messages remain distinct',()=>{
  assert.equal(L.emptyText({},'No schedules',0),'No schedules');
  assert.equal(L.emptyText({query:'essay'},'No schedules',0),'No schedules');
  assert.match(L.emptyText({query:'essay'},'No schedules',3),/조건에 맞는 일정이 없어요/);
  assert.match(L.emptyText({unfinishedOnly:true},'No schedules',3),/조건을 지우면/);
  assert.equal(L.active({query:'  '}),false);
});
test('controls escape query text and describe only loaded search scope',()=>{
  const html=L.render({query:'"><img src=x onerror=alert(1)>',unfinishedOnly:true},{scope:'calendar',matched:0,total:4});
  assert.doesNotMatch(html,/<img|value="">/);assert.match(html,/&quot;&gt;&lt;img/);
  assert.match(html,/지금 불러온 달력 화면/);assert.match(html,/현재 불러온 4개 중 0개 표시/);assert.match(html,/data-list-unfinished checked/);
  const today=L.render(L.createState());assert.match(today,/오늘·다가오는 일정/);assert.match(today,/data-list-clear disabled/);
  assert.doesNotMatch(today,/현재 불러온 0개/);
});

function harness(){
  const events={},nodes=new Set(),document={activeElement:null};let state=L.createState(),enabled=true,changes=0,settled=0;
  const root={ownerDocument:document,contains:node=>nodes.has(node),addEventListener:(name,fn)=>events[name]=fn,
    removeEventListener:(name,fn)=>{if(events[name]===fn)delete events[name];},querySelector:()=>root.query};
  function field(data,value=''){
    const node={dataset:data,value,checked:false,selectionStart:0,selectionEnd:0,selectionDirection:'none',
      focus(options){this.focusOptions=options;document.activeElement=this;},setSelectionRange(start,end,direction){this.selectionStart=start;this.selectionEnd=end;this.selectionDirection=direction;},
      closest(){return this.dataset.listClear!==undefined ? this : null;}};
    nodes.add(node);return node;
  }
  root.query=field({listQuery:''});const unfinished=field({listUnfinished:''}),clear=field({listClear:''});
  const controller=L.bind(root,{getState:()=>state,onChange:value=>{state=value;changes++;},enabled:()=>enabled,onCompositionEnd:()=>settled++});
  return {root,controller,field,unfinished,clear,document,nodes,
    get state(){return state;},get changes(){return changes;},get settled(){return settled;},
    enabled(value){enabled=value;},resetAccount(){state=L.createState();controller.reset();},
    event(type,target=root.query,values={}){events[type]?.({type,target,preventDefault(){},...values});},
    replaceQuery(){nodes.delete(root.query);root.query=field({listQuery:''},state.query);return root.query;},events};
}
test('ordinary typing and toggles publish once and retain the other condition',()=>{
  const h=harness();h.root.query.value='Essay';h.event('input');h.event('input');assert.equal(h.changes,1);
  h.unfinished.checked=true;h.event('change',h.unfinished);assert.equal(h.changes,2);
  assert.equal(h.state.query,'Essay');assert.equal(h.state.unfinishedOnly,true);
  h.event('input',h.field({other:''},'Unsent task draft'));assert.equal(h.changes,2);
});
test('Korean IME composition publishes only once after completion',()=>{
  const h=harness();h.event('compositionstart');h.root.query.value='ㄱ';h.event('input',h.root.query,{isComposing:true});
  h.root.query.value='과';h.event('input');h.root.query.value='과제';assert.equal(h.changes,0);assert.equal(h.controller.isComposing(),true);
  h.event('compositionend');h.event('input');assert.equal(h.changes,1);assert.equal(h.state.query,'과제');assert.equal(h.controller.isComposing(),false);
});
test('composition ending without a state change flushes any deferred render',()=>{
  const h=harness();h.root.query.value='과제';h.event('input');h.event('compositionstart');h.event('compositionend');
  assert.equal(h.changes,1);assert.equal(h.settled,1);
});
test('search focus and selection restore to the new DOM input after rendering',()=>{
  const h=harness(),original=h.root.query;original.value='Essay draft';original.selectionStart=2;original.selectionEnd=8;original.selectionDirection='backward';original.focus();
  const selection=h.controller.captureFocus(),next=h.replaceQuery();h.controller.restoreFocus(selection);
  assert.equal(h.document.activeElement,next);assert.notEqual(next,original);assert.equal(next.selectionStart,2);assert.equal(next.selectionEnd,8);assert.equal(next.selectionDirection,'backward');
  assert.equal(next.focusOptions.preventScroll,true);
});
test('reset during composition discards detached old-account input events immediately',()=>{
  const h=harness(),old=h.root.query;h.event('compositionstart');old.value='Old private query';
  h.enabled(false);h.resetAccount();h.replaceQuery();h.event('compositionend',old);assert.equal(h.controller.isComposing(),false);assert.equal(h.changes,0);assert.equal(h.state.query,'');
  h.enabled(true);h.event('input',old);assert.equal(h.changes,0);assert.equal(h.state.query,'');
});
test('clear resets both conditions and disabled controls do not change state',()=>{
  const h=harness();h.root.query.value='Essay';h.event('input');h.unfinished.checked=true;h.event('change',h.unfinished);
  h.clear.disabled=true;h.event('click',h.clear);assert.equal(h.state.query,'Essay');
  h.clear.disabled=false;h.event('click',h.clear);assert.equal(h.state.query,'');assert.equal(h.state.unfinishedOnly,false);
});
test('account-disabled controller neither publishes nor steals focus',()=>{
  const h=harness();h.enabled(false);h.root.query.value='Unverified account';h.event('input');h.event('compositionstart');
  h.controller.restoreFocus({start:0,end:1,direction:'none'});assert.equal(h.changes,0);assert.equal(h.document.activeElement,null);assert.equal(h.controller.isComposing(),false);
});
test('destroy releases all delegated handlers',()=>{
  const h=harness();h.event('compositionstart');h.controller.destroy();assert.equal(Object.keys(h.events).length,0);assert.equal(h.controller.isComposing(),false);
});
console.log(`List controls: ${count} synthetic scenarios passed (loaded-only search, completion filters, privacy reset, IME and focus).`);
