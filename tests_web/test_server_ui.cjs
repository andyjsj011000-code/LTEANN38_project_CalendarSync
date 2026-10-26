'use strict';
// Controller smoke test with API/DOM doubles. Does not claim browser layout QA.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const dir = path.resolve(__dirname, '../web_service/static');
const source = name => fs.readFileSync(path.join(dir, name), 'utf8');
const listeners = {};
const elements = Object.fromEntries(['app', 'modal', 'topbar', 'account-name', 'nav-today', 'nav-calendar', 'toast', 'login-error'].map(id => [id, {innerHTML: '', textContent: '', hidden: true, classList: {toggle() {}}, replaceChildren() {this.innerHTML = '';}}]));
elements.modal.close = function () {this.open = false;};
elements.modal.showModal = function () {this.open = true;};
const requests = [];
let signedIn = false;
const item = {id: 'sample', source: 'teams', title: '<img src=x onerror=alert(1)>', notes: '<script>bad()</script>', channel: '예시 공지', start_date: '2026-10-20', end_date: '2026-10-25', time: null, kind: 'assignment', completed: false, cancelled: false};
const context = {
  window: {}, location: {protocol: 'https:', search: ''}, URL, console, Intl, Date, Set,
  setTimeout() {return 1;}, clearTimeout() {},
  document: {documentElement: {dataset: {mode: 'school'}}, querySelector(s) {return elements[s.slice(1)] || null;}, querySelectorAll() {return [];}, addEventListener(type, handler) {listeners[type] = handler;}},
  async fetch(url, options) {
    requests.push({url, options});
    let status = 200, data;
    if (url === '/api/service') data = {mode:'test-server',idle_seconds:1800};
    else if (url === '/api/me') {status = signedIn ? 200 : 401; data = signedIn ? {id: 'user', name: '테스터', csrf_token: 'test-csrf'} : {detail: 'not signed in'};}
    else if (url === '/auth/logout-all') {signedIn = false; status = 204;}
    else if (url === '/auth/logout') {signedIn = false; status = 204;}
    else if (url === '/api/today') data = {date: '2026-10-22', teams: [item], personal: []};
    else if (url.startsWith('/api/calendar')) data = {items: [item]};
    else if (url === '/api/channels') data = [{id: 'class', name: '예시', enabled: true}];
    else if (url === '/api/changes') data = {items:[]};
    else if (url === '/api/push') data = {configured:false,registered:false,background_enabled:true};
    else if (url === '/api/teams/sync') data = {state:'connected'};
    else if (url === '/api/settings') data = {preferences: {disabled_kinds: []}, connection: {state:'connected'}};
    else if (url === '/api/tasks' && options.method === 'POST') {status = 201; data = {id: 'saved-task'};}
    else throw new Error('Unexpected request: ' + url);
    return {status, ok: status < 400, async json() {return data;}};
  }
};
vm.createContext(context);
vm.runInContext(source('beta-calendar.js'), context);
vm.runInContext(source('beta.js'), context);
const tick = () => new Promise(resolve => setImmediate(resolve));
const click = data => listeners.click({target: {closest() {return {dataset: data};}}});
(async () => {
  for (let i = 0; i < 4; i++) await tick();
  assert.match(elements.app.innerHTML, /href="\/auth\/login"/);
  assert.doesNotMatch(elements.app.innerHTML, /name="code"/);
  signedIn = true;
  vm.runInContext(source('beta.js'), context);
  for (let i = 0; i < 5; i++) await tick();
  assert.match(elements.app.innerHTML, /오늘 할 일/);
  assert.match(elements.app.innerHTML, /&lt;img/); assert.ok(!elements.app.innerHTML.includes('<img'));
  assert.equal(elements['account-name'].textContent, '테스터');
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
  assert.match(elements.app.innerHTML, /변경사항/);
  await click({action:'changes'}); assert.match(elements.modal.innerHTML, /검토할 변경사항/);
  await click({action:'notifications'}); assert.match(elements.modal.innerHTML, /Web Push 키/);
  await click({action: 'security'});
  assert.match(elements.modal.innerHTML, /logout-all/);
  assert.ok(!requests.some(r => r.url.startsWith('/api/beta/') || r.url === '/auth/beta'));
  await click({action: 'logout-all'});
  assert.equal(signedIn, false); assert.match(elements.app.innerHTML, /href="\/auth\/login"/); assert.ok(!elements.app.innerHTML.includes('&lt;img'));
  assert.equal(elements.topbar.hidden, true); assert.equal(elements['account-name'].textContent, '');
  console.log('Test-server UI: login, review and notification panels, escaped data, tasks and logout passed (DOM doubles).');
})().catch(error => {console.error(error); process.exitCode = 1;});
