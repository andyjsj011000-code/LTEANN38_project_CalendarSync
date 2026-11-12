'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const dir = path.resolve(__dirname,'../web_service/static');
const listeners = {}, requests = [], timers = new Map();
let timerId = 0;
const elements = Object.fromEntries(['app','modal','topbar','account-name','nav-today','nav-calendar','nav-assignments','toast','login-error'].map(id => [id,{innerHTML:'',textContent:'',hidden:true,classList:{toggle(){}},replaceChildren(){this.innerHTML='';}}]));
elements.modal.close = function(){this.open=false;};
elements.modal.showModal = function(){this.open=true;};
let signedIn = true, resolveAssignments, result = null;
const now = Date.parse('2026-11-08T05:00:00Z');
const classroomCourse = {key:'id:101',name:'학급',hidden:false};
let classroom = {connected:true,method:'google',courses:[classroomCourse],snapshot:{captured_at:now,items:[
 {kind:'assignment',title:'Classroom November assignment',course:'학급',due:'2026-11-29T15:30:00Z',url:'https://classroom.google.com/c/course/a/assignment/details'},
 {kind:'announcement',title:'Classroom announcement',course:'학급'}
]}};
class Clock extends Date {constructor(...a){super(...(a.length ? a : [now]));} static now(){return now;}}
const item = (id,day,extra={}) => ({id,source:'assignment',kind:'assignment',title:id,channel:'물리실험',start_date:day,end_date:day,time:'23:59',due_at:day+'T14:59:00Z',completed:false,submission_status:'working',source_url:'https://teams.microsoft.com/l/entity/test',...extra});
const context = {
 window:{},location:{protocol:'https:',search:''},console,Intl,Date:Clock,Set,Map,AbortController,DOMException,URL,
 setTimeout(fn,delay){timers.set(++timerId,{fn,delay});return timerId;},clearTimeout(id){timers.delete(id);},
 document:{documentElement:{dataset:{mode:'school'}},hidden:false,querySelector(s){return s === '.classroom-card' ? elements.classroomCard : elements[s.slice(1)] || null;},querySelectorAll(){return [];},addEventListener(t,h){listeners[t]=h;}},
 async fetch(url,options){
  requests.push(url);
  let status=200,data;
  if(url==='/api/service') data={mode:'test-server',school_login:true,assignments_enabled:true,personal_agenda:true,classroom_enabled:true};
  else if(url==='/api/me'){status=signedIn?200:401;data={id:'student',name:'학생',csrf_token:'csrf'};}
  else if(url.startsWith('/api/assignments')) data=result || await new Promise(r=>{resolveAssignments=r;});
  else if(url==='/api/classroom') data=classroom;
  else if(url==='/api/classroom/courses/preference') {
   const body = JSON.parse(options.body);
   assert.deepEqual(body,{key:classroomCourse.key,hidden:true});
   data = classroom = {...classroom,courses:[{...classroomCourse,hidden:true}],snapshot:{...classroom.snapshot,items:[]}};
  }
  else if(url==='/api/today') data={date:'2026-11-08',teams:[],personal:[]};
  else if(url==='/api/personal/today') data={date:'2026-11-08',personal:[]};
  else if(url.startsWith('/api/calendar')) data={items:[]};
  else if(url==='/api/channels') data=[];
  else if(url==='/api/settings') data={preferences:{disabled_kinds:[]},connection:{state:'connected',background_enabled:true}};
  else if(url==='/api/changes') data={items:[]};
  else if(url==='/auth/logout'){signedIn=false;status=204;}
  else throw new Error('Unexpected '+url);
  return {status,ok:status<400,async json(){return data;}};
 }
};
vm.createContext(context);
for(const name of ['beta-calendar.js','beta-assignments.js','beta.js']) vm.runInContext(fs.readFileSync(path.join(dir,name),'utf8'),context);
const tick=()=>new Promise(r=>setImmediate(r));
const flush=async()=>{for(let i=0;i<12;i++) await tick();};
const click=dataset=>listeners.click({target:{closest(){return {dataset};}}});
(async()=>{
 await flush();
 assert.match(elements.app.innerHTML,/오늘 할 일/); // assignments must not block the agenda
 await click({action:'assignments'});
 assert.match(elements.app.innerHTML,/불러오는 중/);
 assert.match(elements.app.innerHTML,/Google Classroom 과제/);
 assert.match(elements.app.innerHTML,/Classroom November assignment/);
 assert.match(elements.app.innerHTML,/11(?:월|\.)\s*30/);
 assert.match(elements.app.innerHTML,/https:\/\/classroom\.google\.com\/c\/course\/a\/assignment\/details/);
 assert.doesNotMatch(elements.app.innerHTML,/Classroom announcement/);
 elements.classroomCard = {outerHTML:''};
 classroom = {...classroom,snapshot:{captured_at:now+1,items:[
  {...classroom.snapshot.items[0],title:'Classroom updated assignment'},classroom.snapshot.items[1]
 ]}};
 const [timer,{fn}] = [...timers].find(([,t])=>t.delay===30000);
 timers.delete(timer);
 await fn();
 assert.match(elements.classroomCard.outerHTML,/Classroom updated assignment/);
 assert.doesNotMatch(elements.classroomCard.outerHTML,/Classroom announcement/);
 resolveAssignments({items:[],state:'permission_required',checked_at:0});
 await flush();
 assert.match(elements.app.innerHTML,/Teams 과제 연결하기/);
 assert.match(elements.app.innerHTML,/assignments=1&amp;background=1/);
 result={state:'connected',checked_at:now/1000,items:[
  item('upcoming','2026-11-12',{title:'<img src=x onerror=alert(1)>'}),
  item('today','2026-11-08'), item('overdue','2026-11-07'),
  item('done','2026-11-09',{completed:true,submission_status:'submitted'}),
  item('undated',null,{due_at:null,time:null})
 ]};
 await click({action:'refresh-assignments'});
 assert.match(elements.app.innerHTML,/&lt;img/);
 assert.ok(!elements.app.innerHTML.includes('<img'));
 assert.ok(!elements.app.innerHTML.includes('data-complete'));
 assert.match(elements.app.innerHTML,/기한 없음/);
 await click({assignmentFilter:'completed'});
 assert.match(elements.app.innerHTML,/제출됨/);
 assert.ok(!elements.app.innerHTML.includes('data-detail="overdue"'));
 await click({assignmentFilter:'overdue'});
 assert.match(elements.app.innerHTML,/data-detail="overdue"/);
 await click({action:'calendar'});
 assert.match(elements.app.innerHTML,/data-detail="upcoming"/);
 assert.match(elements.app.innerHTML,/data-source="classroom"[^>]*>.*Classroom updated assignment/);
 await click({day:'2026-11-30'});
 assert.match(elements.app.innerHTML,/class="date-detail".*Classroom updated assignment/);
 await click({detail:'https://classroom.google.com/c/course/a/assignment/details',source:'classroom'});
 assert.match(elements.modal.innerHTML,/Google Classroom 원문/);
 assert.doesNotMatch(elements.modal.innerHTML,/hide-detail/);
 await click({detail:'upcoming',source:'assignment'});
 assert.match(elements.modal.innerHTML,/제출과 수정은 Teams에서/);
 assert.ok(!elements.modal.innerHTML.includes('hide-detail'));
 await click({action:'today'});
 assert.match(elements.app.innerHTML,/오늘 마감 과제/);
 assert.match(elements.app.innerHTML,/data-detail="today"/);
 await click({action:'classroom-courses'});
 assert.match(elements.modal.innerHTML,/학급/);
 await click({action:'classroom-course-visibility',courseKey:classroomCourse.key});
 assert.doesNotMatch(elements.app.innerHTML,/Classroom updated assignment/);
 assert.match(elements.modal.innerHTML,/다시 표시/);
 await click({action:'calendar'});
 assert.doesNotMatch(elements.app.innerHTML,/Classroom updated assignment/);
 await click({action:'assignments'});
 assert.doesNotMatch(elements.app.innerHTML,/Classroom updated assignment/);
 result=null;
 const pending=click({action:'refresh-assignments'});
 await flush();
 const late=resolveAssignments;
 await click({action:'logout'});
 late({items:[item('private-late','2026-11-12')],state:'connected'});
 await pending; await flush();
 assert.match(elements.app.innerHTML,/Microsoft 로그인/);
 assert.ok(!elements.app.innerHTML.includes('private-late'));
 console.log('Assignments UI: independent loading, consent, status tabs, escaping, calendar, details and logout race passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});

