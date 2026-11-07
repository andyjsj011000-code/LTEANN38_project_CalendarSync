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
  const listeners = {}, requests = [], history = [], timers = new Map();
  let timerId = 0, now = 0;
  const epoch = Date.UTC(2026, 10, 7, 0, 0);
  class ClockDate extends Date {
    constructor(...args) {super(...(args.length ? args : [epoch + now]));}
    static now() {return epoch + now;}
  }
  const elements = Object.fromEntries(['app', 'modal', 'topbar', 'account-name', 'nav-today', 'nav-calendar', 'toast', 'login-error'].map(id => {
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
      querySelector(selector) {return elements[selector.slice(1)] || null;},
      querySelectorAll() {return [];},
      addEventListener(type, handler) {listeners[type] = handler;}},
    async fetch(url, options = {}) {
      requests.push({url, options});
      let result;
      if (overrides[url]) result = overrides[url](options);
      else if (url.startsWith('/api/calendar?') && overrides['/api/calendar']) result = overrides['/api/calendar'](options);
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
  return {elements, requests, history, overrides, item, today, context,
    click(data) {return listeners.click({preventDefault() {}, target: {closest() {return {dataset: data, disabled: false};}}});},
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
const loading = h => assert.match(h.elements.app.innerHTML, /data-load-state="loading"/);
const loadError = h => {
  assert.match(h.elements.app.innerHTML, /data-load-state="error"/);
  assert.match(h.elements.app.innerHTML, /role="alert"/);
  assert.match(h.elements.app.innerHTML, /data-action="refresh"/);
};
const signedOut = h => {
  assert.match(h.elements.app.innerHTML, /school-signin/);
  assert.equal(h.elements.topbar.hidden, true);
  assert.equal(h.elements['account-name'].textContent, '');
  assert.ok(!h.elements.app.innerHTML.includes('Fixture assignment'));
};

(async () => {
  // A real first frame is available while the permission-protected request waits.
  {
    const pending = deferred();
    const h = harness({overrides: {'/api/today': () => pending.promise}}); await settle();
    loading(h); assert.equal(h.elements.topbar.hidden, false);
    assert.equal(h.elements['account-name'].textContent, 'Fixture student');
    assert.ok(!h.elements.app.innerHTML.includes('Fixture assignment'));
    assert.doesNotMatch(h.elements.app.innerHTML, /data-action="(?:new|filters|sync-teams)"/);
    assert.doesNotMatch(h.elements.app.innerHTML, /class="count">0/);
    await h.click({action: 'calendar'}); loading(h);
    pending.resolve(response(h.today)); await settle();
    assert.ok(h.elements.app.innerHTML.includes('Fixture assignment'));
    assert.doesNotMatch(h.elements.app.innerHTML, /data-load-state="(?:loading|error)"/);
  }
  // A permanently pending GET becomes a persistent, actionable error. Retry reads only.
  {
    const pending = deferred();
    const h = harness({overrides: {'/api/today': () => pending.promise}}); await settle(); loading(h);
    await h.advance(100001); loadError(h);
    const before = h.requests.filter(r => r.url === '/api/today').length;
    await h.advance(20000); loadError(h);
    assert.equal(h.requests.filter(r => r.url === '/api/today').length, before);
    assert.equal(h.requests.find(r => r.url === '/api/today').options.signal.aborted, true);
    delete h.overrides['/api/today']; await h.click({action: 'refresh'}); await settle();
    assert.ok(h.elements.app.innerHTML.includes('Fixture assignment'));
    assert.equal(h.requests.filter(r => r.url === '/api/today').length, before + 1);
    assert.ok(h.requests.every(r => !r.options.method || r.options.method === 'GET'));
    assert.ok(!h.requests.some(r => r.url === '/api/teams/sync'));
  }
  // Receiving HTTP headers must not remove the deadline while response.json hangs.
  {
    const body = deferred(); let bodyReads = 0;
    const h = harness({ignoreBodyAbort: new Set(['/api/today']), overrides: {'/api/today': () => ({status: 200, ok: true, json() {bodyReads++; return body.promise;}})}});
    await settle(); loading(h); assert.equal(bodyReads, 1);
    await h.advance(100001); loadError(h);
    body.resolve(h.today); await settle(); loadError(h);
    assert.ok(!h.elements.app.innerHTML.includes('Fixture assignment'));
  }
  // Permission/session loss clears the authenticated shell and every private field.
  {
    const h = harness({overrides: {'/api/today': () => response({detail: 'Fixture session expired'}, 401)}});
    await settle(); signedOut(h);
    assert.ok(!h.requests.some(r => r.url.startsWith('/api/calendar?')));
  }
  // A 401 must lock the view immediately even when its response body never arrives.
  {
    const body = deferred(); let bodyReads = 0;
    const h = harness({overrides: {'/api/today': () => ({status: 401, ok: false, json() {bodyReads++; return body.promise;}})}});
    await settle(); signedOut(h);
    assert.equal(bodyReads, 0);
    assert.ok(!h.requests.some(r => r.url.startsWith('/api/calendar?')));
  }
  // Several individually bounded reads must also obey one overall load deadline.
  {
    const first = deferred(), calendar = deferred();
    const h = harness({overrides: {'/api/today': () => first.promise, '/api/calendar': () => calendar.promise}});
    await settle(); await h.advance(45000); loading(h);
    first.resolve(response(h.today)); await settle();
    assert.ok(h.requests.some(r => r.url.startsWith('/api/calendar?')));
    await h.advance(55001); loadError(h);
    assert.ok(h.requests.filter(r => r.url.startsWith('/api/calendar?')).every(r => r.options.signal.aborted));
  }
  // Logging out works during loading. Even an abort-ignoring late response is discarded.
  {
    const pending = deferred();
    const h = harness({overrides: {'/api/today': () => pending.promise}, ignoreAbort: new Set(['/api/today'])});
    await settle(); loading(h); await h.click({action: 'logout'}); await settle(); signedOut(h);
    const historyStart = h.history.length, requestCount = h.requests.length;
    pending.resolve(response(h.today)); await settle(); signedOut(h);
    assert.equal(h.requests.length, requestCount);
    assert.ok(h.history.slice(historyStart).every(change => !change.html.includes('Fixture assignment')));
  }
  // Ordinary HTTP failures also replace the spinner without discarding a valid login.
  {
    const h = harness({overrides: {'/api/today': () => response({detail: 'Fixture read unavailable'}, 503)}});
    await settle(); loadError(h); assert.equal(h.elements.topbar.hidden, false);
    assert.equal(h.elements['account-name'].textContent, 'Fixture student');
    await h.advance(10000); loadError(h);
  }
  // A 401 before identity is established never exposes an authenticated shell.
  {
    const h = harness({overrides: {'/api/me': () => response({detail: 'Fixture sign-in required'}, 401)}});
    await settle(); signedOut(h);
    assert.ok(!h.requests.some(r => r.url === '/api/today'));
  }
  // A stalled service check has its own bounded, persistent sign-in-safe retry panel.
  {
    const pending = deferred();
    const h = harness({overrides: {'/api/service': () => pending.promise}});
    await settle(); await h.advance(20001);
    assert.match(h.elements.app.innerHTML, /role="alert"/);
    assert.match(h.elements.app.innerHTML, /data-action="retry-boot"/);
    assert.equal(h.elements.topbar.hidden, true);
    assert.equal(h.elements['account-name'].textContent, '');
    assert.ok(!h.requests.some(r => r.url === '/api/me'));
    await h.advance(10000);
    assert.match(h.elements.app.innerHTML, /data-action="retry-boot"/);
    delete h.overrides['/api/service']; await h.click({action: 'retry-boot'}); await settle();
    assert.ok(h.elements.app.innerHTML.includes('Fixture assignment'));
    assert.ok(h.requests.every(r => !r.options.method || r.options.method === 'GET'));
  }
  // An HTTP 200 intermediary HTML page is not a valid signed-in identity.
  {
    const h = harness({overrides: {'/api/me': () => ({status: 200, ok: true, async json() {throw new SyntaxError('Fixture HTML instead of JSON');}})}});
    await settle();
    assert.match(h.elements.app.innerHTML, /role="alert"/);
    assert.match(h.elements.app.innerHTML, /data-action="retry-boot"/);
    assert.equal(h.elements.topbar.hidden, true);
    assert.equal(h.elements['account-name'].textContent, '');
    assert.ok(!h.requests.some(r => r.url === '/api/today'));
  }
  // A newer load wins even when the old transport ignores cancellation.
  {
    const old = deferred(); let reads = 0;
    const h = harness({ignoreAbort: new Set(['/api/today']), overrides: {'/api/today': () => ++reads === 1 ? old.promise : response({date: '2026-11-07', teams: [{id: 'new', source: 'teams', title: 'Newest fixture', channel: 'Fixture', start_date: '2026-11-07', end_date: '2026-11-07', kind: 'assignment', completed: false}], personal: []})}});
    await settle(); await h.click({action: 'refresh'}); await settle();
    assert.ok(h.elements.app.innerHTML.includes('Newest fixture'));
    const historyStart = h.history.length, requestCount = h.requests.length;
    old.resolve(response(h.today)); await settle();
    assert.ok(h.elements.app.innerHTML.includes('Newest fixture'));
    assert.equal(h.requests.length, requestCount);
    assert.ok(h.history.slice(historyStart).every(change => !change.html.includes('Fixture assignment')));
  }
  // An unverified empty Teams result is not reported as a verified zero-event day.
  {
    const h = harness({overrides: {
      '/api/today': () => response({date: '2026-11-07', teams: [], personal: []}),
      '/api/settings': () => response({preferences: {disabled_kinds: []}, connection: {state: 'checking'}})
    }});
    await settle();
    assert.doesNotMatch(h.elements.app.innerHTML, /data-load-state="error"/);
    assert.ok(!h.elements.app.innerHTML.includes('\uC624\uB298\uC740 Teams \uC77C\uC815\uC774 \uC5C6\uC5B4\uC694.'));
    assert.doesNotMatch(h.elements.app.innerHTML, /Teams \uC77C\uC815\s*<span class="count">0/);
  }
  console.log('Initial-load UI: 13 synthetic scenarios passed (delayed shell, GET/body/overall deadlines, persistent errors, read-only retries, immediate 401/invalid identity, logout/overlap privacy, success and unverified empty-state).');
})().catch(error => {console.error(error); process.exitCode = 1;});
