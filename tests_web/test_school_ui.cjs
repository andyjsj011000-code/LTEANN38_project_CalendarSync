'use strict';
// Controller smoke test with API/DOM doubles. Does not claim browser layout QA.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const dir = path.resolve(__dirname, '../web_service/static');
const source = name => fs.readFileSync(path.join(dir, name), 'utf8');
const listeners = {};
const elements = Object.fromEntries(['app', 'modal', 'topbar', 'account-name', 'nav-today', 'nav-calendar', 'nav-profile', 'toast', 'login-error'].map(id => [id, {innerHTML: '', textContent: '', hidden: true, classList: {toggle() {}}, setAttribute() {}, removeAttribute() {}, replaceChildren() {this.innerHTML = '';}}]));
elements.modal.close = function () {this.open = false;};
elements.modal.showModal = function () {this.open = true;};
const requests = [];
let signedIn = false;
let ingestion = {state: 'partial', counts: {channels_checked: 15, channels_total: 62, messages_failed: 2}};
let classroom = {connected: true, method: 'google', snapshot: null, announcement_analysis: false,
  courses: [{key: 'id:101', name: '실험 수업', hidden: false}]};
const storage = new Map();
const item = {id: 'sample', source: 'teams', title: '<img src=x onerror=alert(1)>', notes: '<script>bad()</script>', channel: '예시 공지', start_date: '2026-10-20', end_date: '2026-10-25', time: null, kind: 'assignment', completed: false, cancelled: false};
const context = {
  window: {localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)}}, location: {protocol: 'https:', search: ''}, URL, console, Intl, Date, Set, AbortController, DOMException,
  setTimeout() {return 1;}, clearTimeout() {},
  document: {documentElement: {dataset: {mode: 'school'}}, querySelector(s) {return elements[s.slice(1)] || null;}, querySelectorAll() {return [];}, addEventListener(type, handler) {listeners[type] = handler;}},
  async fetch(url, options) {
    requests.push({url, options});
    let status = 200, data;
    if (url === '/api/service') data = {synthetic_data_only: true, assignments_enabled: true, classroom_enabled: true, classroom_google_enabled: true, planner_enhancements: true, teams_notice_processing: 'openai', mode: 'test-server'};
    else if (url === '/api/me') {status = signedIn ? 200 : 401; data = signedIn ? {id: 'user', name: '테스터', email: 'student@school.example', csrf_token: 'test-csrf'} : {detail: 'not signed in'};}
    else if (url === '/auth/logout-all') {signedIn = false; status = 204;}
    else if (url === '/auth/logout') {signedIn = false; status = 204;}
    else if (url === '/api/today') data = {date: '2026-10-22', teams: [item], personal: []};
    else if (url.startsWith('/api/calendar')) data = {items: [item]};
    else if (url === '/api/channels') data = [{id: 'class', name: '예시', enabled: true}];
    else if (url === '/api/settings') data = {preferences: {disabled_kinds: []}, connection: {state:'connected', background_enabled: true}};
    else if (url === '/api/teams/ingestion-status') data = ingestion;
    else if (url === '/api/assignments') data = {items: [], state: 'permission_required'};
    else if (url === '/api/classroom') data = classroom;
    else if (url === '/api/push') data = {configured: true, registered: false, background_enabled: true};
    else if (url === '/api/changes') data = {items: []};
    else if (url === '/api/tasks' && options.method === 'POST') {status = 201; data = {id: 'saved-task'};}
    else throw new Error('Unexpected request: ' + url);
    return {status, ok: status < 400, async json() {return data;}};
  }
};
vm.createContext(context);
vm.runInContext(source('beta-calendar.js'), context);
vm.runInContext(source('beta-assignments.js'), context);
vm.runInContext(source('beta-profile.js'), context);
vm.runInContext(source('beta.js'), context);
const tick = () => new Promise(resolve => setImmediate(resolve));
const click = data => listeners.click({target: {closest() {return {dataset: data};}}});
(async () => {
  for (let i = 0; i < 4; i++) await tick();
  assert.match(elements.app.innerHTML, /href="\/auth\/login"/);
  assert.doesNotMatch(elements.app.innerHTML, /name="code"/);
  const beforeLogin = elements.app.innerHTML.split('href="/auth/login?background=1"')[0];
  assert.match(beforeLogin, /Teams.*OpenAI/);
  assert.match(beforeLogin, /학교 이메일.*Google.*읽기 권한/);
  assert.match(beforeLogin, /href="\/privacy"/);
  signedIn = true;
  vm.runInContext(source('beta.js'), context);
  for (let i = 0; i < 5; i++) await tick();
  assert.match(elements.app.innerHTML, /오늘 할 일/);
  assert.match(elements.app.innerHTML, /&lt;img/); assert.ok(!elements.app.innerHTML.includes('<img'));
  assert.equal(elements['account-name'].textContent, '테스터');
  await click({action: 'profile'});
  assert.match(elements.app.innerHTML, /마이페이지/);
  assert.match(elements.app.innerHTML, /student@school\.example/);
  const teamsNotice = /data-profile-section="teams"[\s\S]+?<\/summary>/.exec(elements.app.innerHTML)[0];
  assert.match(teamsNotice, /읽기 권한 · 연결됨/);
  assert.match(teamsNotice, /일부 공지 확인.*15\/62개 채널 확인.*공지 2개 처리 재시도 필요/);
  ingestion = {state: 'completed', counts: {channels_checked: 62, channels_total: 62, messages_failed: 0}};
  elements['profile-teams-ingestion'] = {textContent: ''};
  await click({action: 'profile-refresh'});
  assert.match(elements['profile-teams-ingestion'].textContent, /최근 수집 완료.*62\/62개 채널 확인/);
  assert.match(elements.app.innerHTML, /Teams 과제 읽기[\s\S]*?승인 필요/);
  assert.match(elements.app.innerHTML, /Google 클래스룸 읽기[\s\S]*?연결됨/);
  const classroomSettings = /data-profile-section="classroom"[\s\S]+?<\/details>/.exec(elements.app.innerHTML)[0];
  assert.match(classroomSettings, /data-action="classroom-courses"/);
  assert.match(classroomSettings, /data-action="classroom-disconnect"/);
  assert.match(elements.app.innerHTML, /data-action="classroom-analysis"/);
  await click({action: 'classroom-disconnect'});
  assert.match(elements.modal.innerHTML, /data-action="classroom-disconnect-confirm" data-google="1"/);
  await click({action: 'close'});
  assert.match(elements.app.innerHTML, /브라우저 알림[\s\S]*?꺼짐/);
  const permissionRows = [...elements.app.innerHTML.matchAll(/data-profile-section="[\w-]+"[\s\S]+?<\/details>/g)];
  assert.equal(permissionRows.length, 6);
  for (const [row] of permissionRows) {
    assert.match(row, /사용하는 데이터/);
    assert.match(row, /끄거나 해제하면/);
  }
  assert.match(elements.app.innerHTML, /href="\/privacy#connections"/);
  classroom = {connected: false, snapshot: null};
  await click({action: 'profile-refresh'});
  assert.match(elements.app.innerHTML, /data-action="classroom-google-connect"/);
  await click({action: 'classroom-google-connect'});
  assert.match(elements.modal.innerHTML, /수업 목록·공지·내 과제/);
  assert.match(elements.modal.innerHTML, /Google에서 읽기 권한 확인/);
  assert.ok(!requests.some(request => request.url === '/api/classroom/google/connect'));
  await click({action: 'close'});
  assert.ok(requests.some(request => request.url === '/api/push'));
  listeners.change({target: {name: 'profile-theme', value: 'dark', dataset: {}}});
  assert.equal(context.document.documentElement.dataset.theme, 'dark');
  vm.runInContext(source('beta-profile.js'), context);
  assert.equal(context.document.documentElement.dataset.theme, 'dark');
  await click({action: 'calendar'});
  assert.match(elements.app.innerHTML, /range-event/); assert.match(elements.app.innerHTML, /마감/);
  assert.match(elements.app.innerHTML, /2026년 10월/);
  await click({detail: 'sample', source: 'teams'});
  assert.equal(elements.modal.open, true); assert.match(elements.modal.innerHTML, /&lt;script&gt;/);
  const quick = {id: 'quick-form', elements: {title: {value: '직접 작성한 내용'}}};
  await listeners.submit({preventDefault() {}, target: quick});
  const post = requests.find(r => r.url === '/api/tasks');
  assert.equal(JSON.parse(post.options.body).title, '직접 작성한 내용');
  assert.equal(post.options.headers['x-csrf-token'], 'test-csrf');
  await click({action: 'security'});
  assert.match(elements.modal.innerHTML, /logout-all/);
  assert.ok(!requests.some(r => r.url.startsWith('/api/beta/') || r.url === '/auth/beta'));
  await click({action: 'logout-all'});
  assert.equal(signedIn, false); assert.match(elements.app.innerHTML, /href="\/auth\/login"/); assert.ok(!elements.app.innerHTML.includes('&lt;img'));
  assert.equal(elements.topbar.hidden, true); assert.equal(elements['account-name'].textContent, '');
  console.log('School UI: Microsoft login, escaped data, calendar, personal tasks and all-device logout passed (DOM doubles).');
})().catch(error => {console.error(error); process.exitCode = 1;});
