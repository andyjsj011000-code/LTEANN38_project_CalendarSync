'use strict';
(() => {
  const $ = s => document.querySelector(s);
  const SCHOOL = document.documentElement?.dataset.mode === 'school';
  let idleTimer, idleDeadline = 0, authEpoch = 0, resumeCheck = null, lastSessionCheckAt = 0, autoReadsPaused = false;
  let activeLoad = null, monthRequest = null, monthEpoch = 0;
  let mutationSettled = Promise.resolve(), mutationRevision = 0;
  let dialogEpoch = 0, dialogRequestId = 0, lastRenderKey = null, dialogContainsTeams = false;
  const pausedFilters = new WeakMap();
  let ingestionTimer = null, ingestionAttempts = 0, ingestionDeadline = 0, ingestionRefresh = false, ingestionFinalNeeded = false;
  let checkingTimer = null, checkingAttempts = 0, checkingDeadline = 0, checkingRefresh = false, teamsAccessRevision = 0;
  const readRequests = new Set();
  let reminderRules = null, reminderCustomInitial = '';
  const activeView = epoch => epoch === authEpoch && !!S.me;
  const testServer = () => ['test-server','preview'].includes(S.service?.mode);
  let reviewItems = [], reviewDialogItems = new Map();
  const connectionText = {refresh_unavailable:'일시적으로 연결을 확인하지 못했어요. 잠시 후 다시 시도해 주세요.', connected:'Teams 권한과 일정 연결을 확인했어요.', checking:'Teams 접근 권한을 확인하고 있어요.', not_connected:'Teams 연결을 확인해 주세요.', source_not_configured:'공지 수집 설정이 아직 준비되지 않았어요. 연결 상태에서 확인해 주세요.', source_unavailable:'학교 일정을 가져오지 못해 Teams 표시를 중단했어요. 개인 할 일은 사용할 수 있어요.', verification_failed:'Teams 접근 권한을 확인하지 못해 표시를 중단했어요.', reauth_required:'Microsoft 계정으로 다시 로그인해 주세요.', account_not_allowed:'학교 소속 계정인지 확인해 주세요.', refresh_busy:'Microsoft 연동을 갱신 중이에요. 잠시 후 다시 확인해 주세요.'};
  const root = $('#app'), modal = $('#modal'), topbar = $('#topbar');
  const C = window.BetaCalendar;
  const L = window.BetaListControls;
  const A = window.BetaAssignments;
  let assignmentRequest = null, assignmentSequence = 0;
  const assignmentsEnabled = () => !!(S.service?.assignments_enabled && A);
  const assignmentCalendar = () => assignmentsEnabled() ? (S.assignments?.items || []).filter(e => e.start_date && e.end_date) : [];
  let listControls = null, taskDrafts = null, draftUi = null;
  let plannerStatus = null, statusRequest = null, statusReadAt = 0, statusRevision = 0;
  let trashItems = [], undoTrash = null, draftRestoring = false, taskMissing = false, taskFormRevision = 0;
  const S = {me: null, view: 'today', day: '', month: '', selected: '', today: null, items: [], upcoming: [], channels: [], prefs: null, busy: false, loading: false, loadError: false, epoch: 0};
  S.assignments = {items:[],state:'idle'}; S.assignmentFilter = 'upcoming';
  S.listFilters = L?.createState() || {query:'',unfinishedOnly:false};
  S.classroom = null;
  let classroomRevision = 0, classroomTimer = null, classroomRequest = null;
  const plannerEnabled = () => !!S.service?.planner_enhancements;
  const shownItems = items => plannerEnabled() && L ? L.filter(items,S.listFilters) : items;
  const partNames = ['personal','today','calendar','upcoming','channels','settings','changes'];
  S.ready = Object.fromEntries(partNames.map(name => [name,false])); S.partErrors = {};
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const kindName = {personal: '개인', assignment: '과제', application: '신청', event: '행사·일정'};
  const pretty = d => new Intl.DateTimeFormat('ko-KR', {month: 'long', day: 'numeric', weekday: 'short', timeZone: 'UTC'}).format(new Date(d + 'T00:00:00Z'));
  const period = e => e.start_date === e.end_date ? pretty(e.start_date) : `${pretty(e.start_date)} ~ ${pretty(e.end_date)}`;
  let toastTimer;
  function toast(message, error = false) {
    const el = $('#toast'); el.textContent = message; el.className = 'toast' + (error ? ' error' : ''); el.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => {el.hidden = true;}, 6000);
  }
  // CSRF 헤더·취소·시간 제한을 공통 적용하며, 쓰기 시간 초과는 서버 처리 실패로 단정하지 않는다.
  async function api(path, options = {}) {
    const {body, signal, timeout, ...rest} = options;
    const read = ['GET', 'HEAD'].includes((rest.method || 'GET').toUpperCase());
    const controller = new AbortController();
    let expired = false, timer, onAbort, abortRead;
    const abortError = () => {const error = new Error(expired ? (read ? '서버 응답이 늦어 요청을 마치지 못했어요. 잠시 후 다시 시도해 주세요.' : '처리 결과를 아직 확인하지 못했어요. 서버에서는 계속 처리 중일 수 있어요. 잠시 후 새로고침으로 결과를 확인해 주세요.') : 'Request aborted'); error.name = expired ? 'TimeoutError' : 'AbortError'; return error;};
    const cancelled = controller ? new Promise((resolve, reject) => {
      abortRead = () => reject(abortError());
      controller.signal.addEventListener('abort', abortRead, {once: true});
    }) : null;
    if (controller) {
      readRequests.add(controller);
      onAbort = () => controller.abort();
      if (signal?.aborted) controller.abort();
      else signal?.addEventListener('abort', onAbort, {once: true});
      timer = setTimeout(() => {expired = true; controller.abort();}, timeout || (['/api/service', '/api/beta/info', '/api/me'].includes(path) ? 20000 : 90000));
    }
    const request = async () => {
      const response = await fetch(path, {...rest, credentials: 'same-origin', ...(controller || signal ? {signal: controller?.signal || signal} : {}), headers: {'Content-Type': 'application/json', ...(testServer() && path === '/auth/logout' ? {'X-CalendarSync-Logout':'1'} : {}), ...(S.me ? {'x-csrf-token': S.me.csrf_token} : {})}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
      if (response.status === 204) return null;
      if ([401, 403].includes(response.status)) {
        const error = new Error(response.status === 403 ? '접근 권한을 확인할 수 없어요. 다시 로그인해 주세요.' : '다시 로그인해 주세요.'); error.status = response.status; throw error;
      }
      let invalidJson = false;
      const value = await response.json().catch(error => {if (controller?.signal.aborted) throw abortError(); invalidJson = true; return {};});
      if (!response.ok) {
        const message = response.status === 409 ? '다른 화면에서 변경되었어요. 새로고침한 뒤 다시 시도해 주세요.' : response.status === 404 ? '일정이 변경되었거나 접근 권한을 다시 확인해야 해요. 최신 목록에서 다시 선택해 주세요.' : typeof value.detail === 'string' ? value.detail : '입력 내용과 날짜를 확인해 주세요.';
        const error = new Error(message); error.status = response.status; error.code = value.code; error.teamsAccessUnverified = value.teams_access_unverified === true; error.retainTeamsSnapshot = value.retain_teams_snapshot === true; if (Array.isArray(value.snapshot_channel_ids)) error.snapshotChannels = value.snapshot_channel_ids; if (typeof value.retryable === 'boolean') error.retryable = value.retryable; if (typeof value.connection_state === 'string') error.connectionState = value.connection_state; throw error;
      }
      if (invalidJson) throw new Error(read ? '서버 응답을 확인하지 못했어요. 잠시 후 다시 연결해 주세요.' : '처리 결과를 확인하지 못했어요. 다시 저장하기 전에 새로고침으로 결과를 확인해 주세요.');
      return value;
    };
    try {return await (controller ? Promise.race([request(), cancelled]) : request());}
    finally {
      clearTimeout(timer);
      if (controller) {readRequests.delete(controller); controller.signal.removeEventListener('abort', abortRead); signal?.removeEventListener('abort', onAbort);}
    }
  }
  function failure(error, ownerDialog = null) {
    if ([401, 403].includes(error.status)) {clearPrivate(); loginScreen(); toast(error.status === 403 ? '접근 권한을 확인할 수 없어요. 다시 로그인해 주세요.' : '다시 로그인해 주세요.', true);}
    else {
      const inline = ownerDialog !== null && modal.open && ownerDialog === dialogEpoch;
      if (inline) {noteMissingTask(error, ownerDialog); mutationStatus(error.message || '요청을 마치지 못했어요. 다시 확인해 주세요.', ownerDialog, true);}
      if (presentPermissionPending(error) || error.loadPresented) return;
      if (!inline && (ownerDialog === null || !modal.open)) toast(error.message || '연결하지 못했어요. 실행 창이 열려 있는지 확인해 주세요.', true);
    }
  }
  function clearPrivate() {
    assignmentRequest?.abort(); assignmentRequest = null; assignmentSequence++;
    S.assignments = {items:[],state:'idle'}; S.assignmentFilter = 'upcoming';
    stopClassroomRefresh(); S.classroom = null; classroomRevision++;
    taskDrafts?.reset(); draftUi = null; plannerStatus = null; statusRequest?.abort(); statusRequest = null; statusReadAt = 0; statusRevision++;
    trashItems = []; undoTrash = null; draftRestoring = false; taskMissing = false; taskFormRevision++;
    S.listFilters = L?.createState() || {query:'',unfinishedOnly:false}; listControls?.reset();
    clearTimeout(ingestionTimer); ingestionTimer = null; ingestionAttempts = 0; ingestionDeadline = 0; ingestionRefresh = false; ingestionFinalNeeded = false; lastRenderKey = null;
    S.ready = Object.fromEntries(partNames.map(name => [name,false])); S.partErrors = {};
    dialogEpoch++; dialogRequestId++;
    clearTimeout(checkingTimer); checkingTimer = null; checkingAttempts = 0; checkingDeadline = 0; checkingRefresh = false;
    clearTimeout(idleTimer); idleDeadline = 0; resumeCheck = null; lastSessionCheckAt = 0; autoReadsPaused = false; authEpoch++;
    activeLoad?.abort(); activeLoad = null;
    monthRequest?.abort(); monthRequest = null; monthEpoch++;
    S.pendingMonth = ''; S.syncing = false; S.syncUncertain = false; S.syncMessage = ''; S.quickDraft = ''; reviewDialogItems.clear();
    S.refreshing = false; S.refreshError = ''; S.permissionPending = false; S.teamsUnverified = false; S.teamsSnapshotPending = false; S.permissionMonth = ''; S.permissionMonthSelected = ''; teamsAccessRevision++;
    for (const controller of readRequests) controller.abort();
    reviewItems = []; reminderRules = null; reminderCustomInitial = ''; S.connection = null;
    S.epoch++; S.me = null; S.items = []; S.upcoming = []; S.today = null; S.channels = []; S.prefs = null; S.busy = false; S.loading = false; S.loadError = false;
    detailItem = null; hiddenItems = []; selectedDates.clear();
    if (modal.open) modal.close(); modal.replaceChildren(); topbar.hidden = true; $('#account-name').textContent = '';
  }
  function loginScreen() {
    lastRenderKey = null;
    if (SCHOOL) {root.innerHTML = `<section class="login-wrap"><div class="login-card"><div class="login-logo">C</div><p class="eyebrow">CALENDARSYNC</p><h1>학교 일정과 할 일,<br>한눈에.</h1><p>${S.service?.mode === 'preview' ? '학교 계정 없이 예시 화면을 확인하세요.' : '학교 Microsoft 계정으로 로그인하세요.'}</p><a class="primary school-signin" href="/auth/login?background=1">${S.service?.mode === 'preview' ? '예시 데이터로 시작하기 →' : 'Microsoft 로그인 · 이 기기 연동 유지 →'}</a><p class="fine muted">공유 기기에서는 <a href="/auth/login">이번 사용만 로그인</a>을 선택하세요.</p><p class="login-note">Microsoft 로그인 화면에서 인증해요. 이 서비스는 계정 비밀번호를 받지 않아요. 기기 연동을 유지하면 최대 30일 동안 서버가 공지를 갱신하며, 직접 로그아웃하면 이 기기의 연동과 알림을 끊어요. 본인이 접근할 수 있는 Teams 일정만 표시하며, 웹 알림은 로그인 후 이 기기에서 따로 켤 수 있어요. 외부 캘린더 등록은 아직 제공하지 않아요.</p><p><a href="/privacy">개인정보 안내</a></p>${testServer() ? '<button class="secondary" type="button" data-action="disconnect-device">이 기기 연결 해제</button>' : ''}<p class="error-message" id="login-error" role="alert">${location.search?.includes('login=failed') ? '로그인을 완료하지 못했어요. 학교 계정과 앱 권한을 확인하고 다시 시도해 주세요.' : ''}</p></div></section>`; return;}
    root.innerHTML = `<section class="login-wrap"><div class="login-card"><div class="login-logo">C</div><p class="eyebrow">CALENDARSYNC · BETA</p><h1>오늘 해야 할 일,<br>한눈에.</h1><p class="muted">학교 로그인 없이 초대 코드로 먼저 써보세요.</p><form id="login-form"><label class="field"><span class="field-label">테스트 계정 초대 코드</span><input name="code" type="password" required minlength="16" maxlength="128" autocomplete="off" spellcheck="false" placeholder="실행 창에 표시된 코드를 입력하세요"></label><p class="error-message" id="login-error" role="alert"></p><button class="primary" type="submit">베타 시작하기 →</button></form><p class="login-note">Teams 공지는 예시 데이터입니다. 개인 할 일은 계정별로 서버에 저장됩니다. 같은 코드로 로그인하면 같은 계정이 열려요.</p></div></section>`;
  }
  function renderLoadState(error = S.loadError) {
    lastRenderKey = null;
    if (!S.me) return;
    if (assignmentsEnabled() && S.view === 'assignments') return renderContent();
    if ($('#nav-assignments')) $('#nav-assignments').hidden = !assignmentsEnabled();
    const pending = !!S.permissionPending, failed = error && !pending;
    topbar.hidden = false; $('#account-name').textContent = S.me.name;
    $('#nav-today').classList.toggle('active', S.view === 'today'); $('#nav-calendar').classList.toggle('active', S.view === 'calendar');
    root.innerHTML = `<section data-load-state="${pending ? 'checking' : failed ? 'error' : 'loading'}" aria-busy="${!failed}"><div class="intro"><div><p class="eyebrow">나의 학교생활 플래너</p><h1>${S.view === 'calendar' ? '캘린더' : '오늘 할 일'}</h1><p class="muted">${failed ? '로그인한 계정의 일정을 다시 불러와 주세요.' : '로그인을 확인했어요. 일정 정보를 불러오고 있어요.'}</p></div><div class="actions"><button class="${failed ? 'primary' : 'secondary'}" data-action="refresh" ${failed || pending ? '' : 'disabled'}>${pending ? '다시 확인' : failed ? '다시 불러오기' : '불러오는 중'}</button></div></div><section class="card" role="${failed ? 'alert' : 'status'}" aria-live="polite"><div class="card-head"><h2>${pending ? 'Teams 권한 확인을 기다리고 있어요' : failed ? '일정을 아직 불러오지 못했어요' : '일정을 확인하고 있어요'}</h2></div><div class="empty">${pending ? '접근 권한을 확인하면 Teams 일정이 자동으로 표시돼요.' : failed ? '서버 응답이 늦거나 연결이 불안정할 수 있어요. 잠시 후 다시 불러와 주세요.' : '학교 일정의 접근 권한을 확인하는 데 시간이 걸릴 수 있어요. 완료되면 자동으로 표시해요.'}</div></section><div class="today-layout"><section class="card"><div class="card-head"><h2><span class="dot"></span>Teams 일정</h2></div><div class="empty">${failed ? '일정을 불러온 뒤 확인할 수 있어요.' : 'Teams 일정을 불러오는 중이에요.'}</div></section><section class="card"><div class="card-head"><h2><span class="dot personal"></span>개인 할 일</h2></div><div class="empty">${failed ? '할 일을 불러온 뒤 추가하거나 수정할 수 있어요.' : '개인 할 일을 불러오는 중이에요.'}</div></section></div></section>` + connectionNotice();
  }
  async function ensureCollectionReady() {
    if (!S.syncUncertain) return;
    const epoch = authEpoch, settings = await api('/api/settings');
    if (!activeView(epoch)) return;
    if (settings.connection?.state === 'checking') throw new Error('Teams 공지를 아직 확인 중이에요. 현재 일정은 유지했어요. 잠시 후 새로고침해 주세요.');
    S.syncUncertain = false;
  }
  function permissionRetryNeeded() {
    if (S.connection?.retryable === false) return false;
    if (S.permissionPending) return true;
    if (S.connection?.teams_verified === true && S.connection?.state === 'connected') return false;
    return S.connection?.retryable === true || S.connection?.state === 'checking';
  }
  function teamsConnectionEnded() {
    return ['not_connected','reauth_required','account_not_allowed','source_not_configured'].includes(S.connection?.state);
  }
  function protectTeamsDialog() {
    if (!modal.open) return;
    const form = $('#filters-form');
    if (form && permissionRetryNeeded() && !teamsConnectionEnded()) {
      if (!pausedFilters.has(form)) {
        const changed = [...form.querySelectorAll('[name=channel]')].filter(input => input.checked !== input.defaultChecked);
        pausedFilters.set(form, new Map(changed.map(input => [input.value,input.checked])));
        form.querySelector('#filter-channel-list').replaceChildren();
      }
      const apply = form.querySelector('button[type=submit]');
      apply.disabled = true; delete apply.dataset.locked;
      form.querySelector('#filter-channel-retry').hidden = false;
      form.querySelector('#channel-search-status').textContent = 'Teams 권한을 다시 확인하고 있어요. 검색어와 체크는 유지돼요.';
    } else if (dialogContainsTeams || form) closeDialog();
  }
  // Teams 데이터와 관련 팝업만 제거해 사용자의 개인 일정은 화면에 남긴다.
  function redactTeams(unverified = true) {
    S.teamsSnapshotPending = false;
    if (unverified) S.teamsUnverified = true;
    teamsAccessRevision++;
    if (S.today) S.today = {...S.today,teams:[]};
    S.items = S.items.filter(item => item.source === 'personal'); S.upcoming = S.upcoming.filter(item => item.source === 'personal');
    S.channels = []; reviewItems = []; hiddenItems = hiddenItems.filter(item => item.source === 'personal'); reviewDialogItems.clear(); dialogRequestId++;
    protectTeamsDialog();
    if (detailItem?.source === 'teams') detailItem = null;
  }
  // 권한 확인이 지연되면 서버가 허용한 범위에서 현재 세션에 이미 표시한 일정만 임시 유지한다.
  function suspendTeams(retain, channels) {
    if (!retain || teamsConnectionEnded()) {redactTeams(); return;}
    // Retain only the already-rendered schedules in this signed-in session.
    // New reads and writes still require the server's unchanged permission proof.
    S.teamsUnverified = true; S.teamsSnapshotPending = true; teamsAccessRevision++;
    if (Array.isArray(channels)) {
      const allowed = new Set(channels), visible = item => item.source === 'personal' || allowed.has(item.channel_id);
      if (S.today) S.today = {...S.today,teams:S.today.teams.filter(visible)};
      S.items = S.items.filter(visible); S.upcoming = S.upcoming.filter(visible); hiddenItems = hiddenItems.filter(visible);
    }
    S.channels = []; reviewItems = []; reviewDialogItems.clear(); dialogRequestId++;
    protectTeamsDialog();
    if (detailItem?.source === 'teams') detailItem = null;
  }
  function presentPermissionPending(error) {
    if (error.status !== 503 || error.code !== 'permission_check_pending' || !S.me) return false;
    S.permissionPending = error.retryable !== false; S.connection = {...S.connection, state:error.connectionState || 'checking', retryable:error.retryable !== false};
    if (error.teamsAccessUnverified) {S.connection.teams_verified = false; suspendTeams(error.retainTeamsSnapshot && error.retryable !== false,error.snapshotChannels);}
    S.refreshError = error.retryable === false ? connectionText[S.connection.state] || 'Teams 연결 상태를 확인해 주세요.' : 'Teams 접근 권한을 확인하고 있어요. 잠시 후 다시 확인해 주세요.';
    if (!S.today) {S.loadError = true; renderLoadState(true);}
    else {updateRefreshStatus(); render();}
    scheduleCheckingRefresh(); return true;
  }
  function scheduleCheckingRefresh() {
    clearTimeout(checkingTimer); checkingTimer = null;
    if (!S.me || !S.service?.shared_channel_ingestion || document.hidden || autoReadsPaused) return;
    if (!permissionRetryNeeded()) {
      if (S.connection?.state) {checkingAttempts = 0; checkingDeadline = 0;}
      return;
    }
    if (!checkingDeadline) checkingDeadline = Date.now() + 300000;
    if (checkingRefresh || checkingAttempts >= 20 || Date.now() >= checkingDeadline) return;
    const epoch = authEpoch;
    checkingTimer = setTimeout(async () => {
      checkingTimer = null;
      if (!activeView(epoch) || document.hidden || expireIdle() || !permissionRetryNeeded() || Date.now() >= checkingDeadline) return;
      if (autoReadsPaused || resumeCheck || activeLoad || monthRequest || S.busy || S.syncing) {scheduleCheckingRefresh(); return;}
      checkingAttempts++; checkingRefresh = true;
      try {await load({background:true, checkingRetry:true, mutationVersion:mutationRevision});}
      catch (error) {if (activeView(epoch)) {checkingAttempts = 20; failure(error);}}
      finally {if (activeView(epoch)) {checkingRefresh = false; scheduleCheckingRefresh();}}
    }, 15000);
  }
  function scheduleIngestionRefresh() {
    clearTimeout(ingestionTimer); ingestionTimer = null;
    const pending = S.connection?.ingestion_pending === true;
    if (!S.me || document.hidden || autoReadsPaused || S.teamsUnverified || permissionRetryNeeded()) return;
    if (!pending && !ingestionFinalNeeded) {
      if (!ingestionRefresh) {ingestionAttempts = 0; ingestionDeadline = 0;}
      return;
    }
    if (!ingestionDeadline) ingestionDeadline = Date.now() + 180000;
    if (ingestionRefresh || ingestionAttempts >= 60 || Date.now() >= ingestionDeadline) {updateRefreshStatus(); return;}
    const epoch = authEpoch;
    ingestionTimer = setTimeout(async () => {
      ingestionTimer = null;
      if (!activeView(epoch) || document.hidden || expireIdle()) return;
      if (Date.now() >= ingestionDeadline) {updateRefreshStatus(); return;}
      if (S.teamsUnverified || permissionRetryNeeded()) {scheduleCheckingRefresh(); return;}
      if (autoReadsPaused || resumeCheck || activeLoad || monthRequest || S.busy || S.syncing || checkingRefresh) {scheduleIngestionRefresh(); return;}
      ingestionAttempts++; ingestionRefresh = true;
      const finalPass = ingestionFinalNeeded && S.connection?.ingestion_pending !== true;
      try {
        const applied = await load({background:true, ingestionRetry:true, mutationVersion:mutationRevision});
        if (finalPass && applied && S.connection?.ingestion_pending !== true) ingestionFinalNeeded = false;
        if (activeView(epoch) && !S.teamsUnverified && ingestionFinalNeeded && S.connection?.ingestion_pending !== true && S.connection?.state !== 'checking' && !document.hidden && !S.busy && !S.syncing && !activeLoad && ingestionAttempts < 60 && Date.now() < ingestionDeadline) {
          ingestionAttempts++;
          const finalized = await load({background:true, ingestionRetry:true, mutationVersion:mutationRevision});
          if (finalized && S.connection?.ingestion_pending !== true) ingestionFinalNeeded = false;
        }
      } catch (error) {if (activeView(epoch)) {ingestionAttempts = 60; failure(error);}}
      finally {if (activeView(epoch)) {ingestionRefresh = false; scheduleIngestionRefresh();}}
    }, 3000);
  }
  function stopClassroomRefresh() {
    clearTimeout(classroomTimer); classroomTimer = null;
    classroomRequest?.abort(); classroomRequest = null;
  }
  function canRefreshClassroom() {
    return S.me && S.service?.classroom_enabled && S.classroom?.connected !== false &&
      !document.hidden && !autoReadsPaused;
  }
  function scheduleClassroomRefresh(delay = 30000) {
    if (!canRefreshClassroom() || classroomTimer !== null || classroomRequest) return;
    classroomTimer = setTimeout(refreshClassroom, delay);
  }
  async function refreshClassroom() {
    classroomTimer = null;
    if (!canRefreshClassroom() || expireIdle()) return;
    if (activeLoad || S.busy) {scheduleClassroomRefresh(); return;}
    const epoch = authEpoch, version = classroomRevision;
    const controller = new AbortController(); classroomRequest = controller;
    const current = () => activeView(epoch) && version === classroomRevision &&
      !controller.signal.aborted && canRefreshClassroom() && !expireIdle();
    try {
      const value = await api('/api/classroom', {signal:controller.signal, timeout:15000});
      if (!current()) return;
      if (S.classroom?.connected === value.connected && S.classroom?.method === value.method &&
          S.classroom?.reauth === value.reauth &&
          S.classroom?.snapshot?.captured_at === value.snapshot?.captured_at) return;
      S.classroom = value;
      delete S.partErrors.classroom;
      const card = $('.classroom-card');
      if (card) card.outerHTML = classroomCard(S.view === 'assignments');
    } catch (error) {
      if (current() && [401,403].includes(error.status)) failure(error);
    } finally {
      if (classroomRequest === controller) {
        classroomRequest = null;
        scheduleClassroomRefresh();
      }
    }
  }
  // 개인 일정을 별도로 불러오고, 현재 로그인·수정 세대와 맞는 조회 결과만 화면에 반영한다.
  async function load({background = false, afterSync = false, checkingRetry = false, ingestionRetry = false, mutationVersion} = {}) {
    if (!S.me) return;
    if (S.syncing && !afterSync) {if (!S.busy) toast('Teams 공지를 가져온 뒤 화면을 갱신해요. 현재 일정은 계속 볼 수 있어요.'); return;}
    if (assignmentsEnabled()) void loadAssignments();
    const initialSession = authEpoch;
    if (!checkingRetry) await ensureCollectionReady();
    if (!activeView(initialSession) || ((checkingRetry || ingestionRetry) && (autoReadsPaused || resumeCheck))) return;
    background = background || !!S.today;
    const epoch = ++S.epoch, sessionEpoch = authEpoch, revision = mutationRevision;
    if (!background) {monthRequest?.abort(); monthRequest = null; monthEpoch++; S.pendingMonth = '';}
    const calendarEpoch = monthEpoch;
    activeLoad?.abort();
    stopClassroomRefresh();
    const controller = new AbortController(); activeLoad = controller;
    const timer = setTimeout(() => controller.abort(), 100000), errors = [], applied = new Set();
    let teamsRejected = false; const accessRevision = teamsAccessRevision;
    const permittedItems = (items, previous) => teamsRejected || accessRevision !== teamsAccessRevision
      ? [...(S.teamsSnapshotPending ? previous.filter(item => item.source === 'teams') : []),...items.filter(item => item.source === 'personal')]
      : items;
    const current = () => epoch === S.epoch && activeView(sessionEpoch) && revision === mutationRevision;
    const publishable = () => current() && !((checkingRetry || ingestionRetry) && (autoReadsPaused || resumeCheck || document.hidden)) && !((afterSync || checkingRetry || ingestionRetry) && (S.busy || mutationVersion !== mutationRevision));
    S.loading = !S.today; S.loadError = false; S.refreshing = background; S.refreshError = ''; S.partErrors = {}; updateRefreshStatus();
    if (!background) {
      rememberQuickDraft(); S.today = null; S.items = []; S.upcoming = []; S.channels = []; S.prefs = null; reviewItems = [];
      S.ready = Object.fromEntries(partNames.map(name => [name,false]));
      if (modal.open) modal.close(); modal.replaceChildren(); renderLoadState();
    }
    const part = async (name, path, apply) => {
      try {
        const value = await api(path, {signal:controller.signal});
        if (!publishable() || controller.signal.aborted) return;
        if (apply(value) === false) return;
        S.ready[name] = true; applied.add(name); delete S.partErrors[name];
        if ((name === 'channels' || name === 'settings') && applied.has('channels') && applied.has('settings') &&
            accessRevision === teamsAccessRevision && !teamsRejected && !S.teamsUnverified && !permissionRetryNeeded() && !connectionRejectsTeams()) {
          restoreFilterChannels();
        }
        if (S.today) render();
        return value;
      } catch (error) {
        if (!current() || (ingestionRetry && document.hidden)) return;
        if ([401,403].includes(error.status)) {failure(error); return;}
        if (name === 'personal' && applied.has('today')) return;
        S.partErrors[name] = error.code === 'permission_check_pending' ? 'Teams 접근 권한을 확인하고 있어요.' : '불러오지 못했어요. 새로고침으로 다시 확인해 주세요.';
        if (error.teamsAccessUnverified) teamsRejected = true;
        if (presentPermissionPending(error)) {if (S.today) render(); return;}
        errors.push({name,error});
        if (S.today) render();
      }
    };
    const classroomVersion = classroomRevision;
    const classroom = S.service?.classroom_enabled
      ? part('classroom', '/api/classroom', value => {
        if (classroomVersion !== classroomRevision) return false;
        S.classroom = value;
      }) : null;
    const personal = S.service?.personal_agenda ? part('personal', '/api/personal/today', value => {
      // A combined snapshot owns both sources once it has arrived in this load.
      if (applied.has('today') || (S.today?.date && value.date < S.today.date)) return false;
      const sameDay = S.today?.date === value.date;
      if (!sameDay) S.ready.today = false;
      Object.assign(S, {today:{date:value.date,personal:value.personal,teams:sameDay ? S.today.teams : []}, day:value.date,
        month:S.month || value.date.slice(0,7), selected:S.selected || value.date, loading:false, refreshing:true});
    }) : Promise.resolve();
    try {
      const today = await part('today', '/api/today', value => {
        if (S.today?.date && value.date < S.today.date) {
          S.partErrors.today = '날짜가 바뀌었어요. 새로고침으로 오늘의 Teams 일정을 확인해 주세요.';
          render(); return false;
        }
        S.ready.personal = true; delete S.partErrors.personal;
        if (accessRevision === teamsAccessRevision && !teamsRejected) {S.permissionPending = false; S.teamsUnverified = false; S.teamsSnapshotPending = false;}
        else {teamsRejected = true; value = {...value,teams:S.teamsSnapshotPending ? S.today?.teams || [] : []};}
        Object.assign(S, {today:value, day:value.date, month:S.month || value.date.slice(0,7), selected:S.selected || value.date, loading:false, refreshing:true});
      });
      if (!today || !publishable()) {
        // A personal-only response may still make the screen useful.
      } else {
        const month = S.month, grid = C.monthGrid(month);
        const jobs = [
          part('calendar', `/api/calendar?start=${grid.start}&end=${grid.end}`, value => {if (calendarEpoch !== monthEpoch) return false; S.items = permittedItems(value.items,S.items);}),
          part('upcoming', `/api/calendar?start=${today.date}&end=${C.addDays(today.date,7)}`, value => {S.upcoming = permittedItems(value.items,S.upcoming);}),
          part('channels', '/api/channels', value => {S.channels = teamsRejected || accessRevision !== teamsAccessRevision ? [] : value;}),
          part('settings', '/api/settings', value => {
            const wasPending = S.connection?.ingestion_pending === true;
            S.prefs = value.preferences;
            if (accessRevision === teamsAccessRevision) {
              S.connection = S.permissionPending ? {...value.connection,state:S.connection?.state || 'checking',...(S.connection?.teams_verified === false ? {teams_verified:false,retryable:S.connection.retryable} : {})} : value.connection;
              if (value.connection?.teams_verified === false) {S.permissionPending = value.connection.retryable === true; teamsRejected = true; suspendTeams(value.connection.retain_teams_snapshot === true && value.connection.retryable === true);}
            } else teamsRejected = true;
            // Data may predate the final worker commit even when this is our first pending=false response.
            if ((wasPending || (!ingestionRetry && S.service?.shared_channel_ingestion)) && value.connection?.ingestion_pending === false) ingestionFinalNeeded = true;
            if (connectionRejectsTeams()) {teamsRejected = true; if (!S.teamsUnverified) redactTeams(false);}
            if (checkingRetry && S.connection?.state === 'connected') S.syncUncertain = false;
          })
        ];
        if (testServer()) jobs.push(part('changes', '/api/changes', value => {reviewItems = teamsRejected || accessRevision !== teamsAccessRevision ? [] : value.items;}));
        else S.ready.changes = true;
        await Promise.allSettled(jobs);
      }
      if (!applied.has('today')) await personal;
      await classroom;
      const failures = errors.filter(({name}) => name !== 'personal' || !applied.has('today'));
      if (current() && failures.length) {
        if (!S.today) {S.loadError = true; renderLoadState(true);}
        S.refreshError = S.today ? '일부 최신 정보를 불러오지 못했어요. 마지막으로 확인한 일정은 유지했어요. 새로고침으로 다시 확인해 주세요.' : '일정을 불러오지 못했어요. 다시 불러오기를 눌러 주세요.';
        const error = new Error(S.refreshError); error.loadPresented = !S.today; throw error;
      }
      const loaded = publishable() && !S.permissionPending && !S.teamsUnverified && ['today','calendar','upcoming','settings'].every(name => applied.has(name));
      if (loaded && S.permissionMonth && !S.syncing && !document.hidden && !autoReadsPaused && !resumeCheck) await loadMonth(S.permissionMonth,S.permissionMonthSelected);
      return loaded && !S.teamsUnverified;
    } finally {
      clearTimeout(timer);
      if (activeLoad === controller) {
        activeLoad = null; S.loading = false; S.refreshing = false; updateRefreshStatus();
        if (current()) {scheduleCheckingRefresh(); scheduleIngestionRefresh(); scheduleClassroomRefresh(); refreshPlannerStatus();}
      }
    }
  }
  async function loadAssignments(force = false) {
    if (!S.me || !assignmentsEnabled() || assignmentRequest) return;
    const epoch = authEpoch, sequence = ++assignmentSequence;
    const controller = new AbortController(); assignmentRequest = controller;
    S.assignments = {...S.assignments,loading:true}; render();
    try {
      const value = await api('/api/assignments' + (force ? '?refresh=true' : ''), {signal:controller.signal,timeout:95000});
      if (!activeView(epoch) || sequence !== assignmentSequence) return;
      S.assignments = {...value,loading:false};
      if (detailItem?.source === 'assignment' && !value.items.some(e => e.id === detailItem.id)) {detailItem = null; if (modal.open) closeDialog();}
    } catch (error) {
      if (!activeView(epoch) || sequence !== assignmentSequence || error.name === 'AbortError') return;
      if ([401,403].includes(error.status)) {failure(error); return;}
      S.assignments = {items:[],state:'unavailable',loading:false};
    } finally {
      if (assignmentRequest === controller) assignmentRequest = null;
      if (activeView(epoch) && sequence === assignmentSequence) render();
    }
  }
  function assignmentToday() {
    if (!assignmentsEnabled()) return '';
    const items = assignmentCalendar().filter(e => e.end_date === S.day);
    return '<section class="card"><div class="card-head"><h2>오늘 마감 과제 <span class="count">'+items.length+'</span></h2><button class="text-button" data-action="assignments">과제 전체 보기 →</button></div><div class="list">'+(items.length ? items.map(A.row).join('') : '<div class="empty">'+(S.assignments.loading ? '과제를 확인하고 있어요.' : S.assignments.state === 'connected' ? '오늘 마감인 과제가 없어요.' : '과제 탭에서 연결 상태를 확인해 주세요.')+'</div>')+'</div></section>';
  }
  function row(e) {
    if (e.source === 'assignment' && A) return A.row(e);
    return `<div class="task-row ${e.source === 'personal' ? 'personal' : ''} ${e.completed ? 'done' : ''} ${e.cancelled ? 'cancelled' : ''}"><input aria-label="${esc(e.title)} 완료" type="checkbox" data-complete="${esc(e.id)}" data-source="${e.source}" ${e.completed ? 'checked' : ''} ${e.cancelled ? 'disabled' : ''}><button class="task-main" data-detail="${esc(e.id)}" data-source="${e.source}"><span class="task-title">${esc(e.title)} ${e.cancelled ? '<span class="badge">취소</span>' : ''}</span><span class="task-meta">${esc(e.channel || '개인 할 일')} · ${esc(e.start_date !== e.end_date ? period(e) : kindName[e.kind] || e.kind)}</span></button><span class="task-time">${esc(e.time || '종일')}</span></div>`;
  }
  function list(items, empty = '표시할 할 일이 없어요.') {return items.length ? items.map(row).join('') : `<div class="empty">${empty}</div>`;}
  function filteredEmpty(fallback, available) {return plannerEnabled() && L ? L.emptyText(S.listFilters,fallback,available) : fallback;}
  function listTools(scope, items) {return plannerEnabled() && L ? L.render(S.listFilters,{scope}) : '';}
  function plannerTime(value) {
    if (!value) return '아직 없음';
    return new Intl.DateTimeFormat('ko-KR',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Seoul'}).format(new Date(value * 1000));
  }
  function ingestionText() {
    if (!plannerStatus) return 'Teams 수집 상태를 확인하고 있어요.';
    if (plannerStatus.unavailable) return 'Teams 수집 상태를 확인하지 못했어요. 상태 확인을 눌러 다시 확인해 주세요.';
    const labels = {idle:'수집 기록 없음',queued:'수집 대기 중',running:'공지 확인 중',completed:'최근 수집 완료',partial:'일부 공지 확인 · 남은 항목은 재확인 필요',error:'수집 중단 · 재확인 필요'};
    const counts = plannerStatus.counts;
    const progress = counts && Number.isInteger(counts.channels_checked) && Number.isInteger(counts.channels_total) ? ` · ${counts.channels_checked}/${counts.channels_total}개 채널 확인` : '';
    const failed = counts?.messages_failed ? ` · 공지 ${counts.messages_failed}개 처리 재시도 필요` : '';
    return `${labels[plannerStatus.state] || '수집 상태 확인 필요'}${progress}${failed} · 마지막 전체 수집 성공: ${plannerTime(plannerStatus.last_success_at)}`;
  }
  function plannerTools() {
    if (!plannerEnabled()) return '';
    return `<div class="planner-tools"><div class="button-row">${draftUi?.available ? '<button class="secondary small" data-action="draft-resume">임시저장 이어 쓰기</button>' : ''}<button class="secondary small" data-action="task-trash">삭제한 할 일</button>${undoTrash ? '<button class="secondary small" data-action="undo-trash">삭제 취소</button>' : ''}</div><div class="planner-sync"><span id="planner-sync-status" role="status" aria-live="polite">${esc(ingestionText())}</span><button class="text-button" data-action="sync-details">상태 확인</button></div></div>`;
  }
  async function refreshPlannerStatus({force=false}={}) {
    if (!plannerEnabled() || !S.me || document.hidden || autoReadsPaused || resumeCheck) return;
    if (statusRequest) {if (!force) return; statusRequest.abort(); statusRequest = null;}
    if (!force && Date.now() - statusReadAt < 2500) return;
    const controller = new AbortController(), epoch = authEpoch, revision = ++statusRevision;
    statusRequest = controller; statusReadAt = Date.now();
    const current = () => activeView(epoch) && revision === statusRevision && !document.hidden && !autoReadsPaused && !resumeCheck;
    try {const result = await api('/api/teams/ingestion-status',{signal:controller.signal,timeout:15000}); if (current()) plannerStatus = result;}
    catch (error) {if (activeView(epoch) && [401,403].includes(error.status)) failure(error); else if (current() && error.name !== 'AbortError') plannerStatus = {unavailable:true};}
    finally {if (statusRequest === controller) statusRequest = null; if (current()) {const el = $('#planner-sync-status'); if (el) el.textContent = ingestionText();}}
  }
  function personalForm() {return modal.open && !!$('#task-form') && detailItem?.source !== 'teams';}
  function readCurrentDraft() {
    if (!personalForm()) return null;
    const form = $('#task-form'), fields = {};
    for (const name of ['title','notes','start_date','end_date','time','display_mode']) fields[name] = form.elements[name]?.value || '';
    fields.selected_dates = [...selectedDates];
    return {task_id:detailItem?.id || null,base_version:detailItem?.version ?? null,fields};
  }
  function updateDraftStatus() {
    const status = $('#task-draft-status'), controls = $('#task-draft-actions');
    if (!personalForm() || !status || draftUi?.context !== dialogEpoch) return;
    status.textContent = taskMissing ? '원래 할 일을 찾을 수 없어요. 현재 입력을 새 할 일로 옮겨 저장할 수 있어요.' : draftUi.message ? draftUi.message + ' 서버에 7일 동안 보관돼요.' : '';
    if (controls) controls.hidden = !taskMissing && !draftUi.blocked && !draftUi.available;
    const copy = $('#task-copy-input'); if (copy) copy.hidden = !taskMissing;
    const replace = document.querySelector('[data-action="draft-replace"]');
    if (replace) replace.textContent = draftUi.available ? '현재 입력으로 임시저장 바꾸기' : '임시저장 다시 시도';
  }
  function noteMissingTask(error, context) {
    if (error.status === 404 && context === dialogEpoch && personalForm() && detailItem?.id) {taskMissing = true; updateDraftStatus();}
  }
  function draftInputChanged() {taskFormRevision++; taskDrafts?.changed();}
  function copyMissingTask() {
    const current = readCurrentDraft();
    if (!taskMissing || !current?.task_id) return;
    taskForm(null,{fields:current.fields,unsaved:true});
    toast('현재 입력을 새 할 일로 옮겼어요. 저장 버튼으로 추가할 수 있어요.');
  }
  async function leaveTaskForm() {
    if (!plannerEnabled() || !taskDrafts || !personalForm() || !taskDrafts.hasUnsaved()) return true;
    const epoch = authEpoch, dialog = dialogEpoch;
    const result = await taskDrafts.flush();
    if (!activeView(epoch) || dialog !== dialogEpoch) return false;
    if (!result.ok) {toast('임시저장하지 못했어요. 현재 입력은 열어 두었어요. 다시 저장하거나 임시저장을 선택해 주세요.',true); return false;}
    return true;
  }
  function closeDialog() {dialogEpoch++; dialogRequestId++; modal.close(); modal.replaceChildren();}
  async function resumeDraft() {
    if (!plannerEnabled() || !taskDrafts || draftRestoring) return;
    if (personalForm() && taskDrafts.hasUnsaved() && !confirm('현재 입력 대신 서버에 임시저장된 내용을 열까요?')) return;
    const epoch = authEpoch, dialog = dialogEpoch, revision = taskFormRevision, input = JSON.stringify(readCurrentDraft());
    draftRestoring = true;
    const canApply = saved => {
      if (!saved || !activeView(epoch) || dialog !== dialogEpoch || !taskDrafts.isCurrentResume(saved.token)) return false;
      if (revision !== taskFormRevision || input !== JSON.stringify(readCurrentDraft())) {toast('입력이 바뀌어 임시저장을 열지 않았어요. 현재 입력은 유지했어요.'); return false;}
      return true;
    };
    try {
      const saved = await taskDrafts.resume();
      if (!canApply(saved)) return;
      let item = null;
      if (saved.draft.task_id) {
        try {item = await api('/api/tasks/'+encodeURIComponent(saved.draft.task_id),{timeout:15000});}
        catch (error) {if (error.status !== 404) throw error;}
        if (!canApply(saved)) return;
        if (!item || item.version !== saved.draft.base_version) {
          if (!confirm('원래 할 일이 삭제되었거나 변경되었어요. 임시저장을 새로운 할 일로 열까요?')) return;
          item = null;
        }
      }
      taskForm(item,{fields:saved.draft.fields,resumeToken:saved.token});
    } finally {if (epoch === authEpoch) draftRestoring = false;}
  }
  async function openTrash() {
    const result = await readDialog('/api/task-trash'); if (!result) return;
    trashItems = result.items || [];
    openDialog('삭제한 할 일',`<p class="muted">삭제한 개인 할 일은 7일 동안 복구할 수 있어요. 보관 기간이 지나면 자동으로 삭제돼요.</p><div class="trash-list">${trashItems.length ? trashItems.map(item => `<div class="trash-row"><div><strong>${esc(item.title)}</strong><p class="fine muted">${esc(item.start_date)} ~ ${esc(item.end_date)} · ${esc(plannerTime(item.expires_at))}까지 복구 가능</p></div><button class="secondary small" data-action="restore-trash" data-trash-id="${esc(item.id)}">복구</button></div>`).join('') : '<div class="empty">삭제한 할 일이 없어요.</div>'}</div>`);
  }
  async function restoreTrash(id) {
    if (!id) return;
    return mutate(() => api('/api/task-trash/'+encodeURIComponent(id)+'/restore',{method:'POST'}),'할 일을 복구했어요.',false,result => {
      if (undoTrash?.id === id) undoTrash = null;
      trashItems = trashItems.filter(item => item.id !== id);
      applyConfirmedState({...result,source:'personal'},{},result);
    });
  }
  function connectionRejectsTeams() {
    if (teamsConnectionEnded()) return true;
    if (S.teamsSnapshotPending && S.permissionPending) return false;
    return S.connection?.teams_verified === false || ['source_unavailable','verification_failed'].includes(S.connection?.state);
  }
  function connectionNotice() {
    if (S.service?.mode === 'preview') return '<div class="beta-banner" role="status"><strong>예시 화면</strong><span>실제 학교 계정이나 공지와 연결되지 않은 미리보기예요.</span></div>';
    if (S.teamsUnverified) {
      const retry = permissionRetryNeeded(), signIn = ['not_connected','reauth_required','account_not_allowed'].includes(S.connection?.state);
      if (S.teamsSnapshotPending) return '<div class="beta-banner" role="status" data-teams-unverified><strong>Teams 권한 재확인 중</strong><span>마지막으로 확인한 일정을 유지하고 있어요. 새 일정과 변경 내용은 권한 확인 후 반영돼요.</span><button data-action="refresh">다시 확인</button></div>';
      return `<div class="beta-banner" role="status" data-teams-unverified><strong>Teams 일정 ${retry ? '권한 재확인 중' : '연결 확인 필요'}</strong><span>권한을 확인하는 동안 Teams 일정을 잠시 숨겼어요. 이 확인 과정에서 일정을 삭제하지 않아요. 개인 할 일은 계속 사용할 수 있어요.</span>${signIn ? '<a href="/auth/login">다시 로그인</a>' : retry ? '<button data-action="refresh">다시 확인</button>' : '<button data-action="security">연결 및 보안</button>'}</div>`;
    }
    if (!S.ready.settings) return '';
    const state = S.connection?.state || 'not_connected';
    if (['connected', 'checking', 'refresh_busy'].includes(state)) return '';
    const signIn = ['not_connected', 'reauth_required', 'account_not_allowed'].includes(state);
    const retry = ['refresh_unavailable', 'source_unavailable', 'verification_failed'].includes(state);
    return `<div class="beta-banner" role="status"><strong>Teams 연결 확인</strong><span>${esc(connectionText[state] || connectionText.not_connected)}</span>${signIn ? '<a href="/auth/login">다시 로그인</a>' : retry ? '<button data-action="refresh">다시 확인</button>' : ''}<button data-action="security">연결 및 보안</button></div>`;
  }
  function intro(title, subtitle) {
    if (SCHOOL) return `<div class="intro"><div><p class="eyebrow">나의 학교생활 플래너</p><h1>${esc(title)}</h1><p class="muted">${esc(subtitle)}</p></div><div class="actions">${refreshAction()}<button class="secondary" data-action="filters" ${!S.prefs || !S.ready.channels ? 'disabled' : ''}>표시 설정</button><button class="primary" data-action="new">＋ 할 일 추가</button></div></div>${connectionNotice()}${testServer() ? `<div class="test-toolbar"><span class="badge">${S.service.mode === 'preview' ? '예시 전용 미리보기 · 실제 학교 계정 연결 없음' : '테스트 서버'}</span><button class="secondary small" data-action="sync-teams" ${S.syncing ? 'disabled aria-busy="true"' : ''}>${S.syncing ? 'Teams 공지 가져오는 중…' : 'Teams 공지 가져오기'}</button><span id="sync-status" role="status" aria-live="polite">${esc(S.syncMessage || '')}</span><button class="secondary small" data-action="changes" ${S.ready.changes ? '' : 'disabled'}>변경사항 <b>${S.ready.changes ? reviewItems.length : '…'}</b></button><button class="secondary small" data-action="notifications" ${S.prefs ? '' : 'disabled'}>알림 설정</button><button class="secondary small" data-action="feedback">오류·의견 보내기</button></div>` : ''}`;
    return `<div class="intro"><div><p class="eyebrow">나의 학교생활 플래너</p><h1>${esc(title)}</h1><p class="muted">${esc(subtitle)}</p></div><div class="actions">${refreshAction()}<button class="secondary" data-action="filters" ${!S.prefs || !S.ready.channels ? 'disabled' : ''}>표시 설정</button><button class="primary" data-action="new">＋ 할 일 추가</button></div></div><div class="beta-banner"><strong>학교 로그인 전 베타</strong><span>Teams 일정은 예시예요. 직접 추가한 할 일은 저장돼요. 알림 발송과 실제 Teams 연결은 준비 중이에요.</span></div>`;
  }
  function rememberQuickDraft() {
    const input = $('#quick-form')?.elements?.title;
    if (input) S.quickDraft = input.value;
  }
  function refreshMessage() {
    const pending = SCHOOL && S.service?.mode !== 'preview' && ['checking', 'refresh_busy'].includes(S.connection?.state);
    return S.refreshError || (autoReadsPaused && !resumeCheck ? '로그인 상태를 확인하지 못해 자동 갱신을 잠시 멈췄어요. 다시 화면을 열거나 활동하면 재확인해요.' : pending ? connectionText[S.connection.state] : S.connection?.ingestion_pending ? (ingestionAttempts >= 60 || (ingestionDeadline && Date.now() >= ingestionDeadline) ? '확인된 일정은 표시했어요. 나머지는 새로고침으로 확인해 주세요.' : '새 Teams 공지를 확인하는 대로 일정이 추가돼요.') : S.refreshing ? '최신 일정을 확인하고 있어요.' : '');
  }
  function refreshAction() {
    return `<button class="secondary" data-action="refresh" ${S.refreshing ? 'disabled aria-busy="true"' : ''}>${S.refreshing ? '갱신 중…' : '새로고침'}</button><span id="refresh-status" role="status" aria-live="polite">${esc(refreshMessage())}</span>`;
  }
  function updateRefreshStatus() {
    const button = document.querySelector('[data-action="refresh"]'), status = $('#refresh-status');
    if (button) {button.disabled = !!(S.refreshing || S.busy); button.textContent = S.refreshing ? '갱신 중…' : '새로고침'; button.setAttribute('aria-busy', String(!!S.refreshing));}
    if (status) status.textContent = refreshMessage();
  }
  function updateSyncStatus() {
    const button = document.querySelector('[data-action="sync-teams"]'), status = $('#sync-status');
    if (button) {button.disabled = !!(S.syncing || S.busy); button.textContent = S.syncing ? 'Teams 공지 가져오는 중…' : 'Teams 공지 가져오기'; button.setAttribute('aria-busy', String(!!S.syncing));}
    if (status) status.textContent = S.syncMessage || '';
  }
  function render() {
    rememberQuickDraft();
    if (S.me && listControls?.isComposing()) return;
    const key = JSON.stringify([S.me?.id,S.me?.name,S.view,S.day,S.month,S.selected,S.today,S.items,S.upcoming,S.channels,S.prefs,S.connection,S.teamsUnverified,S.ready,S.partErrors,reviewItems,S.pendingMonth,S.syncing,S.syncMessage,S.listFilters,S.assignments,S.assignmentFilter,S.classroom,plannerStatus,!!draftUi?.available,undoTrash?.id]);
    if (key === lastRenderKey) {updateRefreshStatus(); updateSyncStatus(); return;}
    const searchFocus = listControls?.captureFocus();
    const input = $('#quick-form')?.elements?.title;
    const focused = input && document.activeElement === input ? {start:input.selectionStart,end:input.selectionEnd,direction:input.selectionDirection} : null;
    renderContent(); lastRenderKey = key;
    if (S.busy) lockWriteControls();
    if (focused && S.me && S.view === 'today') {const next = $('#quick-form')?.elements?.title; next?.focus?.({preventScroll:true}); if (focused.start !== null && focused.start !== undefined) next?.setSelectionRange?.(focused.start,focused.end,focused.direction);}
    listControls?.restoreFocus(searchFocus); updateDraftStatus();
  }
  function renderContent() {
    if (!S.me) return;
    if ($('#nav-assignments')) {$('#nav-assignments').hidden = !assignmentsEnabled(); $('#nav-assignments').classList.toggle('active',S.view === 'assignments');}
    if (assignmentsEnabled() && S.view === 'assignments') {
      topbar.hidden = false; $('#account-name').textContent = S.me.name;
      $('#nav-today').classList.toggle('active',false); $('#nav-calendar').classList.toggle('active',false);
      const connect = '/auth/login?assignments=1' + (S.connection?.background_enabled === true ? '&background=1' : '');
      root.innerHTML = A.render(S.assignments,S.assignmentFilter,connect,Date.now(),classroomCard(true)); return;
    }
    if (!S.today) return renderLoadState();
    topbar.hidden = false; $('#account-name').textContent = S.me.name;
    $('#nav-today').classList.toggle('active', S.view === 'today'); $('#nav-calendar').classList.toggle('active', S.view === 'calendar');
    if (S.view === 'calendar') return renderCalendar();
    const all = [...S.today.teams, ...S.today.personal].filter(e => !e.cancelled), done = all.filter(e => e.completed).length;
    const teams = shownItems(S.today.teams), personal = shownItems(S.today.personal);
    const upcoming = shownItems(S.upcoming.filter(e => e.end_date > S.day && !e.completed && !e.cancelled)).sort((a, b) => a.end_date.localeCompare(b.end_date) || (a.time || '').localeCompare(b.time || '')).slice(0, 5);
    const teamsPending = S.teamsUnverified || !S.ready.today || (SCHOOL && S.service?.mode !== 'preview' && (!S.ready.settings || S.connection?.ingestion_pending || S.connection?.state !== 'connected'));
    const teamsCount = teamsPending && !S.today.teams.length ? (!S.ready.today || !S.ready.settings || S.connection?.ingestion_pending || S.connection?.state === 'checking' ? '확인 중' : '확인 필요') : teams.length;
    const teamsEmpty = S.teamsUnverified ? '권한을 다시 확인하는 동안 Teams 일정을 잠시 숨겼어요. 확인되면 다시 표시돼요.' : !S.ready.today ? (S.partErrors.today || 'Teams 일정을 확인하는 대로 표시해요.') : teamsPending ? (S.connection?.ingestion_pending || !S.ready.settings ? 'Teams 일정을 확인하는 대로 표시해요.' : connectionText[S.connection?.state] || 'Teams 일정을 확인하고 있어요.') : '오늘의 Teams 일정이 없어요.';
    root.innerHTML = intro('오늘 할 일', pretty(S.day)) + plannerTools() + listTools('today',[...S.today.teams,...S.today.personal,...S.upcoming]) + `<div class="today-layout"><div class="stack">${assignmentToday()}<section class="card"><div class="card-head"><h2><span class="dot"></span>Teams 일정 <span class="count">${esc(teamsCount)}</span></h2><span class="muted fine">${SCHOOL ? '학교 공지' : '예시 공지'}</span></div><div class="list">${!S.ready.today && !S.today.teams.length ? `<div class="empty" role="${S.partErrors.today && !S.permissionPending ? 'alert' : 'status'}">${esc(teamsEmpty)}</div>` : list(teams, filteredEmpty(teamsEmpty,S.today.teams.length))}</div></section>${classroomCard()}<section class="card"><div class="card-head"><h2><span class="dot personal"></span>개인 할 일 <span class="count">${personal.length}</span></h2></div><div class="list">${list(personal, filteredEmpty('오늘 할 일을 가볍게 적어보세요.',S.today.personal.length))}</div><form class="quick-form" id="quick-form"><input value="${esc(S.quickDraft || '')}" name="title" aria-label="오늘 할 일" maxlength="500" required placeholder="오늘 할 일 빠르게 추가"><button class="primary" type="submit">추가</button></form></section></div><aside class="stack"><section class="card progress-card"><p class="eyebrow">오늘의 진행</p><h2>${done}<span class="muted"> / ${all.length} 완료</span></h2><div class="progress-track"><div class="progress-fill" style="width:${all.length ? Math.round(done / all.length * 100) : 0}%"></div></div><p class="muted fine">${S.ready.today ? '완료한 항목은 원래 위치에 남아요.' : '먼저 확인된 개인 할 일 기준이에요. Teams 일정은 확인 중이에요.'}</p></section><section class="card"><div class="card-head"><h2>다가오는 일정</h2><button class="text-button" data-action="calendar">전체 보기 →</button></div><div class="upcoming-list">${!S.ready.upcoming ? `<div class="empty" role="status">${esc(S.partErrors.upcoming || '다가오는 일정을 불러오고 있어요.')}</div>` : upcoming.length ? upcoming.map(e => `<button class="upcoming-item" data-detail="${esc(e.id)}" data-source="${e.source}"><span class="date-stamp">${esc(e.end_date.slice(5).replace('-', '/'))}</span><span class="upcoming-text"><strong>${esc(e.title)}</strong><small>${esc(e.time || '종일')} · ${esc(e.source === 'teams' ? (SCHOOL ? 'Teams' : 'Teams 예시') : '개인')}</small></span></button>`).join('') : `<div class="empty">${esc(filteredEmpty('일주일 안에 다가오는 마감이 없어요.',S.upcoming.length))}</div>`}</div></section><section class="card help-card"><h2>${SCHOOL ? '내 일정 관리' : '함께 시험해 보세요'}</h2><p class="muted">기간을 정해 할 일을 추가하고, 캘린더에서 이어지는 막대를 확인해 보세요.</p><div class="button-row"><button class="secondary small" data-action="hidden">숨긴 일정</button>${SCHOOL ? '<button class="secondary small" data-action="security">연결 및 보안</button>' : '<button class="secondary small" data-action="feedback">의견 남기기</button>'}</div></section></aside></div>`;
  }
  function classroomCard(assignmentsOnly = false) {
    if (!S.service?.classroom_enabled) return '';
    const connection = S.classroom;
    const google = connection?.method === 'google';
    const actions = [];
    if (connection && S.service.classroom_google_enabled && (!google || connection.reauth)) {
      actions.push(`<button class="secondary small" data-action="classroom-google-connect">${google ? 'Google Classroom 다시 연결' : 'Google Classroom 연결'}</button>`);
    }
    if (connection?.connected) {
      actions.push('<button class="text-button" data-action="classroom-disconnect">연결 해제</button>');
    } else if (connection && !S.service.classroom_google_enabled) {
      actions.push('<button class="secondary small" data-action="classroom-connect">Classroom 연결</button>');
    }
    let body;
    if (!connection) {
      body = esc(S.partErrors.classroom || 'Classroom 연결을 확인하고 있어요.');
    } else if (!connection.connected) {
      body = '학교 Google 계정을 연결하면 공지와 과제가 여기에 표시돼요.';
    } else if (connection.reauth) {
      body = 'Google 읽기 권한을 다시 승인하면 공지와 과제를 가져올 수 있어요.';
    } else if (!connection.snapshot) {
      body = google ? 'Google Classroom에서 공지와 과제를 가져오고 있어요.'
        : '최근 동기화 기록이 없어요. Apps Script에서 실행 결과를 확인해 주세요.';
    }
    if (body) body = `<p class="empty" role="status">${body}</p>`;
    else {
      const dateTime = new Intl.DateTimeFormat('ko-KR', {
        dateStyle:'short', timeStyle:'short', timeZone:'Asia/Seoul'
      });
      const items = assignmentsOnly
        ? connection.snapshot.items.filter(item => item.kind === 'assignment')
        : connection.snapshot.items;
      const rows = items.map(item => {
        const type = item.kind === 'announcement' ? '공지' : '과제';
        const deadline = item.due ? (item.due.length === 10 ? pretty(item.due) : dateTime.format(new Date(item.due))) : '';
        const meta = [item.course, type, deadline && '마감 ' + deadline].filter(Boolean).join(' · ');
        const link = item.url ? ` <a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">원문</a>` : '';
        return `<div class="upcoming-item"><div class="upcoming-text"><strong>${esc(item.title)}</strong><small>${esc(meta)}${link}</small>${item.description ? `<p class="fine">${esc(item.description)}</p>` : ''}</div></div>`;
      }).join('');
      const empty = assignmentsOnly ? '표시할 구글 클래스룸 과제가 없어요.' : '표시할 공지나 과제가 없어요.';
      body = `<p class="fine muted">최근 동기화 ${esc(dateTime.format(connection.snapshot.captured_at))}</p>${rows || `<p class="empty">${empty}</p>`}`;
    }
    const heading = assignmentsOnly ? 'Google Classroom 과제' : 'Google Classroom';
    return `<section class="card classroom-card"><div class="card-head"><h2>${heading}</h2><div class="actions">${actions.join('')}</div></div><div class="list">${body}</div></section>`;
  }
  async function connectClassroom() {
    const epoch = authEpoch;
    const result = await api('/api/classroom/connection', {method:'POST'});
    if (!activeView(epoch)) return;
    classroomRevision++;
    S.classroom = {connected:true,snapshot:null};
    render();
    scheduleClassroomRefresh();
    openDialog('Google Classroom 연결', `
      <ol>
        <li>학교 계정의 개인 Apps Script 프로젝트에 <a href="/assets/classroom-sync.gs" download>Code.gs 코드</a>와 <a href="/assets/classroom-appsscript.json" download>appsscript.json 설정</a>을 붙여넣으세요.</li>
        <li>프로젝트 설정의 스크립트 속성에 아래 두 값을 저장하세요.</li>
        <li><strong>startClassroomSync</strong>를 실행하고 Google 권한을 승인하세요. 첫 전송이 성공하면 15분마다 갱신돼요.</li>
      </ol>
      <label class="field"><span class="field-label">CALENDARSYNC_ORIGIN</span><input readonly value="${esc(location.origin)}"></label>
      <label class="field"><span class="field-label">CALENDARSYNC_TOKEN</span><input id="classroom-connection-token" readonly value="${esc(result.token)}"></label>
      <p class="fine muted">연결 키는 이 창에서 한 번만 표시돼요.</p>`,
      '<button class="secondary" data-action="close">닫기</button>');
  }
  function renderCalendar() {
    if (!S.ready.calendar) {
      root.innerHTML = intro('캘린더', '확인된 일정부터 표시해요.') + `<section class="card"><div class="card-head"><h2>${esc(S.month)}</h2></div><div class="empty" role="status">${esc(S.partErrors.calendar || '달력 일정을 불러오고 있어요.')}</div></section>`;
      return;
    }
    const visibleItems = shownItems([...S.items,...assignmentCalendar()]);
    const weeks = C.layoutRanges(visibleItems, S.month);
    const gridHtml = weeks.map(week => {
      const rows = week.lanes + 2;
      return `<div class="calweek" style="grid-template-rows:30px ${week.lanes ? `repeat(${week.lanes},30px) ` : ''}minmax(55px,auto)">${week.days.map((day, i) => `<button aria-label="${esc(pretty(day))}" class="cal-day ${day === S.selected ? 'selected' : ''}" data-day="${day}" style="grid-column:${i + 1};grid-row:1 / span ${rows}"></button><span class="cal-date ${day.slice(0, 7) !== S.month ? 'outside' : ''} ${day === S.day ? 'today' : ''}" style="grid-column:${i + 1};grid-row:1">${Number(day.slice(8))}</span>`).join('')}${week.segments.map(s => `<button title="${esc(s.item.title + ' · ' + period(s.item))}" aria-label="${esc(s.item.title + ' · ' + period(s.item))}" data-detail="${esc(s.item.id)}" data-source="${s.item.source}" class="range-event ${s.item.source === 'personal' ? 'personal' : ''} ${s.before ? 'before' : ''} ${s.after ? 'after' : ''} ${s.item.completed ? 'done' : ''} ${s.item.cancelled ? 'cancelled' : ''}" style="grid-column:${s.first + 1} / ${s.last + 2};grid-row:${s.lane + 2}"><span class="range-title">${s.before ? '‹ ' : ''}${esc(s.item.title)}${s.item.cancelled ? ' · 취소' : ''}${s.after ? ' ›' : ''}</span>${s.endsHere ? '<span class="deadline-tag">마감</span>' : ''}</button>`).join('')}${week.days.map((day, i) => `<div class="single-day" style="grid-column:${i + 1};grid-row:${rows}">${visibleItems.filter(e => e.start_date === day && e.end_date === day).map(e => `<button data-detail="${esc(e.id)}" data-source="${e.source}" title="${esc(e.title)}" class="single-event ${e.source === 'personal' ? 'personal' : ''} ${e.completed ? 'done' : ''} ${e.cancelled ? 'cancelled' : ''}"><span class="single-title">${esc(e.title)}${e.cancelled ? ' · 취소' : ''}</span><span class="single-time">${esc(e.time || '')}</span></button>`).join('')}</div>`).join('')}</div>`;
    }).join('');
    const selected = visibleItems.filter(e => e.start_date <= S.selected && e.end_date >= S.selected);
    const monthLabel = `${S.month.slice(0, 4)}년 ${Number(S.month.slice(5))}월`;
    root.innerHTML = intro('캘린더', '기간이 있는 일정은 시작일부터 마감일까지 이어져요.') + plannerTools() + listTools('calendar',S.items) + `<section class="card calendar-card"><div class="calendar-toolbar"><h2>${monthLabel}</h2><span role="status" aria-live="polite">${S.pendingMonth ? esc(S.pendingMonth + ' 일정을 불러오는 중…') : ''}</span><div class="month-actions"><button class="secondary small" data-action="hidden">숨긴 일정</button><button class="icon-button" aria-label="이전 달" data-action="prev-month">‹</button><button class="secondary small" data-action="this-month">이번 달</button><button class="icon-button" aria-label="다음 달" data-action="next-month">›</button></div></div><div class="calendar-legend"><span><i class="dot"></i>${SCHOOL ? 'Teams' : 'Teams 예시'}</span><span><i class="dot personal"></i>개인 할 일</span><span class="muted">날짜를 누르면 아래에 상세 목록이 보여요.</span></div><div class="calendar-grid"><div class="weekdays">${['일', '월', '화', '수', '목', '금', '토'].map(d => `<div class="weekday">${d}</div>`).join('')}</div>${gridHtml}</div></section><section class="card" style="margin-top:20px"><div class="card-head selected-heading"><h2>${esc(pretty(S.selected))}</h2><span class="muted fine">기간 전체 기준 · 오늘 표시는 선택한 날짜를 따릅니다</span></div><div class="date-detail">${list(selected, S.teamsUnverified ? 'Teams 일정은 권한을 다시 확인한 뒤 표시돼요. 현재 확인된 개인 할 일은 없어요.' : filteredEmpty('이 날짜에는 표시할 일정이 없어요.',S.items.filter(e => e.start_date <= S.selected && e.end_date >= S.selected).length))}</div></section>`;
  }
  async function changeMonth(action) {
    const date = new Date((S.pendingMonth || S.permissionMonth || S.month) + '-01T00:00:00Z');
    if (action !== 'this-month') date.setUTCMonth(date.getUTCMonth() + (action === 'next-month' ? 1 : -1));
    const month = action === 'this-month' ? S.day.slice(0, 7) : date.toISOString().slice(0, 7);
    return loadMonth(month, action === 'this-month' ? S.day : month + '-01');
  }
  async function loadMonth(month, selected) {
    const epoch = ++monthEpoch, sessionEpoch = authEpoch, accessRevision = teamsAccessRevision;
    monthRequest?.abort();
    S.pendingMonth = month; S.pendingMonthSelected = selected; render();
    if (S.syncing) return;
    const controller = new AbortController(); monthRequest = controller;
    try {
      await ensureCollectionReady();
      if (epoch !== monthEpoch || !activeView(sessionEpoch)) return;
      const grid = C.monthGrid(month), result = await api(`/api/calendar?start=${grid.start}&end=${grid.end}`, {signal: controller.signal});
      if (epoch !== monthEpoch || !activeView(sessionEpoch)) return;
      if (accessRevision !== teamsAccessRevision && S.teamsSnapshotPending) return;
      S.month = month; S.items = connectionRejectsTeams() || accessRevision !== teamsAccessRevision ? result.items.filter(item => item.source === 'personal') : result.items; S.selected = selected; S.ready.calendar = true; delete S.partErrors.calendar;
      if (!S.teamsUnverified) {S.permissionMonth = ''; S.permissionMonthSelected = '';}
    } catch (error) {
      if (epoch === monthEpoch && activeView(sessionEpoch)) {if (error.teamsAccessUnverified) {S.permissionMonth = month; S.permissionMonthSelected = selected;} failure(error);}
    } finally {
      if (epoch === monthEpoch && activeView(sessionEpoch)) {monthRequest = null; S.pendingMonth = ''; render();}
    }
  }
  function openDialog(title, content, footer = '', containsTeams = false) {
    dialogContainsTeams = containsTeams;
    dialogEpoch++; dialogRequestId++;
    if (modal.open) modal.close();
    modal.innerHTML = `<div class="dialog-head"><h2 id="dialog-title">${esc(title)}</h2><button class="dialog-close" aria-label="닫기" data-action="close">×</button></div><div class="dialog-body">${content}<p id="mutation-status" class="muted fine" role="status" aria-live="polite" hidden></p></div>${footer ? `<div class="dialog-foot">${footer}</div>` : ''}`;
    modal.showModal();
  }
  async function readDialog(path) {
    const request = {id:++dialogRequestId, dialog:dialogEpoch, session:authEpoch, revision:mutationRevision};
    const current = () => activeView(request.session) && request.id === dialogRequestId && request.dialog === dialogEpoch && request.revision === mutationRevision;
    try {
      const result = await api(path);
      return current() ? result : null;
    } catch (error) {
      if (activeView(request.session) && ([401,403].includes(error.status) || current())) failure(error, request.dialog);
      return null;
    }
  }
  document.addEventListener('cancel', async event => {if (event.target !== modal) return; if (plannerEnabled() && personalForm()) {event.preventDefault(); if (!S.busy && await leaveTaskForm()) closeDialog();} else {dialogEpoch++; dialogRequestId++;}}, true);
  modal.addEventListener?.('close', () => {
    if (!modal.open && $('#classroom-connection-token')) modal.replaceChildren();
  });
  function find(id, source) {return [...(S.today?.teams || []), ...(S.today?.personal || []), ...S.items, ...S.upcoming, ...(S.assignments?.items || [])].find(e => e.id === id && e.source === source);}
  let detailItem = null;
  function detail(e) {
    detailItem = e;
    if (e.source === 'assignment' && A) {
      openDialog(e.title, '<p class="detail-meta">'+esc(e.channel)+' · Teams 과제</p><p>'+esc(e.end_date ? period(e)+' '+(e.time || '') : '기한 없음')+'</p><p>'+esc(A.status(e))+'</p><p class="muted fine">제출 상태는 Teams에서 읽어 와요. 제출과 수정은 Teams에서 진행해 주세요.</p>'+sourceLink(e.source_url)+'<div class="detail-notes">'+esc(e.notes || '자세한 안내는 Teams 과제에서 확인해 주세요.')+'</div>', '<button class="secondary" data-action="close">닫기</button>');
      return;
    }
    openDialog(e.title, `<p class="detail-meta">${esc(e.source === 'personal' ? '직접 작성한 할 일' : e.channel)} · ${esc(e.source === 'personal' ? '개인' : kindName[e.kind] || e.kind)}</p><p>${esc(period(e))} ${esc(e.time || '종일')}</p>${e.cancelled ? '<p class="badge">취소된 일정</p>' : ''}${e.source === 'personal' ? `<p class="muted">오늘 표시: ${esc({daily: '기간 중 매일', selected: '선택한 날짜와 마지막 날', final: '마지막 날만'}[e.display_mode])}</p>${e.display_mode === 'selected' ? `<p class="fine muted">${e.selected_dates.map(esc).join(', ')}</p>` : ''}` : (SCHOOL ? '<p class="muted fine">학교 공지에서 추출한 일정이에요. 정확한 내용은 원문에서 확인해 주세요.</p>' + sourceLink(e.source_url) : '<p class="muted fine">실제 학교 공지가 아닌 베타용 예시입니다.</p>')}<div class="detail-notes">${esc(e.notes || '메모가 없어요.')}</div>`, `<button class="secondary" data-action="hide-detail">숨기기</button>${e.source === 'personal' ? '<button class="danger" data-action="delete-detail">삭제</button><button class="primary" data-action="edit-detail">수정</button>' : (testServer() ? '<button class="secondary" data-action="override-detail">나만 수정</button>' : '')}${testServer() ? '<button class="secondary" data-action="reminders">예약 알림</button>' : ''}`, e.source === 'teams');
  }
  function taskForm(e = null, draft = null) {
    detailItem = e; taskMissing = false; taskFormRevision++;
    const day = S.view === 'calendar' ? S.selected : S.day;
    const val = {...(e || {title: '', notes: '', start_date: day, end_date: day, time: '', display_mode: 'daily', selected_dates: []}),...(draft?.fields || {})};
    const personalFields = e?.source === 'teams' ? '' : `<label class="field"><span class="field-label">오늘 할 일에 표시할 날짜</span><select name="display_mode"><option value="daily" ${val.display_mode === 'daily' ? 'selected' : ''}>기간 중 매일</option><option value="selected" ${val.display_mode === 'selected' ? 'selected' : ''}>선택한 날짜 + 마지막 날</option><option value="final" ${val.display_mode === 'final' ? 'selected' : ''}>마지막 날만</option></select></label><div id="selected-date-field" ${val.display_mode === 'selected' ? '' : 'hidden'}><div class="date-picker-row"><input type="date" id="pick-date" aria-label="표시할 날짜"><button type="button" class="secondary small" data-action="pick-date">날짜 추가</button></div><div class="date-chips" id="date-chips"></div><p class="fine muted">마지막 날에는 항상 표시됩니다.</p></div>`;
    openDialog(e?.source === 'teams' ? '내 일정만 수정' : e ? '개인 할 일 수정' : '개인 할 일 추가', `<form id="task-form"><label class="field"><span class="field-label">무엇을 할까요?</span><input name="title" required maxlength="500" placeholder="예: 1주치 숙제하기, 개발 완료하기" value="${esc(val.title)}"></label><div class="form-two"><label class="field"><span class="field-label">시작 날짜</span><input name="start_date" type="date" required value="${esc(val.start_date)}"></label><label class="field"><span class="field-label">마지막 날짜</span><input name="end_date" type="date" required value="${esc(val.end_date)}"></label></div><label class="field"><span class="field-label">마감 시간 (선택)</span><input name="time" type="time" value="${esc(val.time || '')}"></label>${personalFields}<label class="field"><span class="field-label">메모 (선택)</span><textarea name="notes" rows="4" maxlength="10000">${esc(val.notes)}</textarea></label><p class="fine muted">캘린더에는 기간 전체가 이어져 표시돼요. 지난 미완료 항목은 다음 날로 옮겨지지 않아요.</p>${plannerEnabled() && e?.source !== 'teams' ? '<p id="task-draft-status" class="fine muted" role="status" aria-live="polite"></p><div id="task-draft-actions" class="button-row" hidden><button type="button" class="secondary small" data-action="draft-resume">임시저장 이어 쓰기</button><button type="button" class="secondary small" data-action="draft-replace">현재 입력으로 임시저장 바꾸기</button><button type="button" class="text-button" data-action="draft-discard">서버 임시저장 지우기</button><button type="button" id="task-copy-input" class="secondary small" data-action="task-copy-input" hidden>현재 입력을 새 할 일로 열기</button></div>' : ''}<p class="error-message" id="form-error" role="alert"></p><div class="dialog-foot"><button type="button" class="secondary" data-action="close">닫기</button><button class="primary" type="submit">저장</button></div></form>`, '', e?.source === 'teams');
    selectedDates = new Set(val.selected_dates || []); syncDates();
    if (plannerEnabled() && e?.source !== 'teams') taskDrafts?.start({context:dialogEpoch,resumeToken:draft?.resumeToken || null,unsaved:draft?.unsaved === true});
  }
  let selectedDates = new Set();
  function syncDates() {
    const f = $('#task-form'); if (!f) return;
    const a = f.elements.start_date.value, b = f.elements.end_date.value;
    f.elements.end_date.min = a;
    if (!f.elements.display_mode) return;
    if (a && b && a <= b) selectedDates = new Set([...selectedDates].filter(d => d >= a && d <= b));
    $('#selected-date-field').hidden = f.elements.display_mode.value !== 'selected';
    $('#pick-date').min = a; $('#pick-date').max = b; $('#pick-date').value = a;
    $('#date-chips').innerHTML = [...new Set([...selectedDates, b])].filter(Boolean).sort().map(d => `<button type="button" class="secondary small" data-remove-date="${d}" ${d === b ? 'disabled' : ''}>${esc(d)} ${d === b ? '· 마지막 날' : '×'}</button>`).join('');
  }
  function mutationStatus(message, epoch, error = false) {
    const status = $('#mutation-status');
    if (status && modal.open && epoch === dialogEpoch) {status.textContent = message; status.hidden = !message; status.setAttribute('role', error ? 'alert' : 'status');}
  }
  // 저장 이전에 시작한 목록·월별 조회를 무효화해 늦은 응답이 최신 화면을 덮어쓰지 못하게 한다.
  function invalidateDataReads() {
    S.epoch++; activeLoad?.abort(); activeLoad = null;
    monthEpoch++; monthRequest?.abort(); monthRequest = null; if (!S.syncing) S.pendingMonth = '';
    S.loading = false; S.refreshing = false;
  }
  function lockWriteControls() {
    document.querySelectorAll('button,input,select,textarea').forEach(el => {if (!el.disabled && !['close', 'logout', 'disconnect-device', 'logout-all'].includes(el.dataset.action)) {el.disabled = true; el.dataset.locked = '1';}});
  }
  // 중복 저장을 막고 수정 세대를 갱신하며, 저장 실패와 저장 후 화면 갱신 실패를 구분해 안내한다.
  async function mutate(operation, message, clearQuick = false, onSaved, pendingMessage = '저장 중…') {
    if (S.busy) return;
    S.busy = true;
    let finishMutation;
    mutationSettled = new Promise(resolve => {finishMutation = resolve;});
    const sessionEpoch = authEpoch, originalDialog = dialogEpoch;
    mutationStatus(pendingMessage, originalDialog);
    lockWriteControls();
    let saved = false, released = false;
    const release = () => {
      if (released) return;
      released = true; finishMutation();
      if (sessionEpoch === authEpoch) {
        S.busy = false;
        document.querySelectorAll('[data-locked]').forEach(el => {el.disabled = false; delete el.dataset.locked;});
        updateRefreshStatus();
      }
    };
    try {
      const result = await operation(); saved = true;
      if (!activeView(sessionEpoch)) return;
      mutationRevision++;
      if (onSaved) {invalidateDataReads(); onSaved(result);}
      if (clearQuick) {S.quickDraft = ''; const input = $('#quick-form')?.elements?.title; if (input) input.value = '';}
      if (originalDialog === dialogEpoch) {if (modal.open) modal.close(); modal.replaceChildren();}
      if (onSaved) {
        toast(message);
        const refreshing = load();
        release();
        await refreshing;
      } else {await load(); toast(message);}
    } catch (error) {
      if (sessionEpoch !== authEpoch) return;
      if (saved && error.status !== 401) toast('저장됐지만 화면을 다시 불러오지 못했어요. 새로고침해 주세요.', true);
      else failure(error, originalDialog);
    } finally {
      release();
    }
  }
  function applyConfirmedState(original, values, result) {
    const item = {...original, ...values, ...result};
    const matches = candidate => candidate.id === original.id && candidate.source === original.source;
    const wasVisible = [...(S.today?.teams || []), ...(S.today?.personal || []), ...S.items, ...S.upcoming].some(matches);
    const channel = S.channels.find(candidate => candidate.id === item.channel_id);
    const allowed = !item.hidden && (item.source === 'personal' || (!(S.prefs?.disabled_kinds || []).includes(item.kind) && (channel ? channel.enabled : (!S.ready.channels || !item.channel_id) && wasVisible)));
    const order = (a,b) => (a.time || '99:99').localeCompare(b.time || '99:99') || a.source.localeCompare(b.source) || a.id.localeCompare(b.id);
    const replace = (items, include) => [...items.filter(candidate => !matches(candidate)), ...(include ? [item] : [])].sort(order);
    if (S.today) {
      const appears = allowed && C.appears(item, S.day);
      S.today = {...S.today, teams:replace(S.today.teams, appears && item.source === 'teams'), personal:replace(S.today.personal, appears && item.source === 'personal')};
    }
    const grid = C.monthGrid(S.month);
    S.items = replace(S.items, allowed && item.start_date <= grid.end && item.end_date >= grid.start);
    S.upcoming = replace(S.upcoming, allowed && item.start_date <= C.addDays(S.day,7) && item.end_date >= S.day);
    hiddenItems = replace(hiddenItems, !!item.hidden);
    if (detailItem && matches(detailItem)) detailItem = item;
    render();
  }
  function mutateState(item, values, message) {
    if (!item) return;
    const original = {...item};
    const pending = values.hidden === true ? '숨기는 중…' : values.hidden === false ? '복원하는 중…' : '완료 상태를 저장하는 중…';
    return mutate(() => setState(original, values), message, false, result => applyConfirmedState(original, values, result), pending);
  }
  function applyConfirmedDelete(item) {
    const keep = candidate => candidate.id !== item.id || candidate.source !== item.source;
    if (S.today) S.today = {...S.today, teams:S.today.teams.filter(keep), personal:S.today.personal.filter(keep)};
    S.items = S.items.filter(keep); S.upcoming = S.upcoming.filter(keep); hiddenItems = hiddenItems.filter(keep);
    if (detailItem && !keep(detailItem)) detailItem = null;
    render();
  }
  async function setState(e, values) {
    const epoch = authEpoch;
    try {
      return await api(`/api/${e.source === 'personal' ? 'tasks' : 'events'}/${encodeURIComponent(e.id)}/state`, {method: 'PATCH', body: {...values, ...(e.source === 'personal' ? {version: e.version} : {})}});
    } catch (error) {
      if (error.status === 404 && activeView(epoch)) {
        if (modal.open) modal.close(); modal.replaceChildren(); detailItem = null;
        try {await load();} catch (refreshError) {if (activeView(epoch)) failure(refreshError);}
      }
      throw error;
    }
  }
  function filterChannelRows(form, query) {
    if (pausedFilters.has(form)) return;
    const normalize = text => text.normalize('NFKC').toLocaleLowerCase('ko-KR');
    const words = normalize(query).trim().split(/\s+/).filter(Boolean);
    const rows = form.querySelectorAll('[data-channel-row]');
    let matched = 0;
    for (const row of rows) {
      row.hidden = !words.every(word => normalize(row.textContent).includes(word));
      if (!row.hidden) matched++;
    }
    form.querySelector('#channel-search-status').textContent = !rows.length ? '표시할 Teams 채널이 없어요.' : matched ? `${rows.length}개 중 ${matched}개 채널 표시` : '검색 결과가 없어요. 다른 채널 이름으로 검색해 주세요.';
  }
  function filterChannelMarkup() {
    return S.channels.map(ch => `<label class="toggle-row" data-channel-row><span>${esc(ch.name)}</span><input type="checkbox" name="channel" value="${esc(ch.id)}" ${ch.enabled ? 'checked' : ''}></label>`).join('');
  }
  function restoreFilterChannels() {
    const form = modal.open && $('#filters-form');
    const checks = form && pausedFilters.get(form);
    if (!checks) return;
    form.querySelector('#filter-channel-list').innerHTML = filterChannelMarkup();
    for (const input of form.querySelectorAll('[name=channel]')) {
      if (checks.has(input.value)) input.checked = checks.get(input.value);
    }
    pausedFilters.delete(form);
    form.querySelector('#filter-channel-retry').hidden = true;
    const apply = form.querySelector('button[type=submit]');
    apply.disabled = S.busy;
    if (S.busy) apply.dataset.locked = '1';
    filterChannelRows(form, form.querySelector('[data-channel-query]').value);
  }
  function filters() {
    openDialog('표시 설정', `<form id="filters-form"><h3>Teams 채널</h3><label class="field"><span class="field-label">채널 검색</span><input type="search" data-channel-query placeholder="채널 이름으로 검색" autocomplete="off" aria-describedby="channel-search-status"></label><p id="channel-search-status" class="fine muted" role="status">${S.channels.length ? `${S.channels.length}개 채널 표시` : '표시할 Teams 채널이 없어요.'}</p><button id="filter-channel-retry" type="button" class="secondary small" data-action="refresh" hidden>다시 확인</button><div id="filter-channel-list">${filterChannelMarkup()}</div><h3>일정 종류</h3>${Object.entries(kindName).filter(([key]) => key !== 'personal').map(([key, label]) => `<label class="toggle-row"><span>${label}</span><input type="checkbox" name="kind" value="${key}" ${S.prefs.disabled_kinds.includes(key) ? '' : 'checked'}></label>`).join('')}<p class="fine muted">설정은 내 계정에만 적용돼요.</p><div class="dialog-foot"><button type="button" class="secondary" data-action="close">닫기</button><button class="primary" type="submit">적용</button></div></form>`, '', S.channels.length > 0);
    if (S.teamsUnverified && permissionRetryNeeded()) protectTeamsDialog();
  }
  let hiddenItems = [];
  async function hidden() {
    const year = (S.view === 'calendar' ? S.selected || S.month : S.day).slice(0,4);
    const result = await readDialog(SCHOOL ? `/api/hidden?start=${year}-01-01&end=${year}-12-31` : '/api/beta/hidden');
    if (!result) return;
    hiddenItems = result.items;
    openDialog(SCHOOL ? `숨긴 일정 · ${year}년` : '숨긴 일정', `<p class="muted">개별적으로 숨긴 일정이에요. 복원해도 꺼둔 채널·종류 설정은 유지돼요.</p><div class="hidden-list">${hiddenItems.length ? hiddenItems.map((e, i) => `<div class="hidden-item"><div><strong>${esc(e.title)}</strong><p class="fine muted">${esc(period(e))} · ${esc(e.source === 'teams' ? (SCHOOL ? 'Teams' : 'Teams 예시') : '개인')}</p></div><button class="secondary small" data-restore="${i}">복원</button></div>`).join('') : '<div class="empty">숨긴 일정이 없어요.</div>'}</div>`, '', hiddenItems.some(item => item.source === 'teams'));
  }
  function feedback() {
    openDialog('베타 의견 남기기', `<form id="feedback-form"><p class="muted">불편한 점이나 원하는 기능을 적어주세요. 이 베타 서버에 저장되며, 운영자가 나중에 확인할 수 있어요.</p><label class="field"><span class="field-label">의견</span><textarea name="message" rows="6" required maxlength="4000" placeholder="어느 화면에서 무엇이 불편했나요?"></textarea></label><div class="dialog-foot"><button type="button" class="secondary" data-action="close">닫기</button><button class="primary" type="submit">저장</button></div></form>`);
  }
  document.addEventListener('click', async event => {
    const button = event.target.closest('button'); if (!button || button.disabled) return;
    const action = button.dataset.action;
    if (S.busy && !['logout', 'disconnect-device', 'logout-all', 'close'].includes(action)) return;
    if (!S.today && !['today', 'calendar', 'assignments', 'refresh-assignments', 'refresh', 'retry-boot', 'logout', 'disconnect-device', 'logout-all', 'close'].includes(action)) return;
    const sessionEpoch = authEpoch, ownerDialog = dialogEpoch;
    try {
      const leavesForm = button.dataset.detail || ['close','today','calendar','assignments','new','filters','hidden','feedback','changes','notifications','security','task-trash'].includes(action);
      if (leavesForm && personalForm()) {if (!await leaveTaskForm() || !activeView(sessionEpoch) || ownerDialog !== dialogEpoch) return; if (['today','calendar','assignments'].includes(action)) closeDialog();}
      if (plannerEnabled() && action === 'task-trash') return await openTrash();
      if (plannerEnabled() && action === 'restore-trash') {if (trashItems.some(item => item.id === button.dataset.trashId)) return await restoreTrash(button.dataset.trashId); return;}
      if (plannerEnabled() && action === 'undo-trash') return await restoreTrash(undoTrash?.id);
      if (plannerEnabled() && action === 'sync-details') return await refreshPlannerStatus({force:true});
      if (plannerEnabled() && action === 'draft-resume') return await resumeDraft();
      if (plannerEnabled() && action === 'task-copy-input') return copyMissingTask();
      if (plannerEnabled() && action === 'draft-replace') {const result = await taskDrafts?.replace(); if (result?.ok) toast('현재 내용을 임시저장했어요.'); return;}
      if (plannerEnabled() && action === 'draft-discard') {if (confirm('서버의 임시저장을 지울까요? 현재 입력은 유지돼요.')) {const result = await taskDrafts?.discard(); if (result?.ok) toast('서버의 임시저장을 지웠어요.');} return;}
      if (button.dataset.changeDecision !== undefined) {const item = reviewDialogItems.get(button.dataset.changeId); if (!item) return; return mutate(() => api('/api/changes/'+encodeURIComponent(item.id),{method:'POST',body:{accept:button.dataset.changeDecision==='accept'}}),'변경사항을 처리했어요.');}
      if (button.dataset.assignmentFilter && assignmentsEnabled()) {S.assignmentFilter = ['upcoming','overdue','completed','all'].includes(button.dataset.assignmentFilter) ? button.dataset.assignmentFilter : 'upcoming'; render(); return;}
      if (action === 'refresh-assignments' && assignmentsEnabled()) return await loadAssignments(true);
      if (action === 'classroom-google-connect' && S.service?.classroom_google_enabled) {
        const epoch = authEpoch;
        button.disabled = true;
        try {
          const result = await api('/api/classroom/google/connect', {method:'POST'});
          if (!activeView(epoch)) return;
          const url = new URL(result.authorization_url);
          if (url.origin !== 'https://accounts.google.com' || url.pathname !== '/o/oauth2/v2/auth') {
            throw new Error('Google 연결 주소를 확인하지 못했어요.');
          }
          location.assign(url.href);
        } finally {button.disabled = false;}
        return;
      }
      if (action === 'classroom-connect') {
        button.disabled = true;
        try {return await connectClassroom();} finally {button.disabled = false;}
      }
      if (action === 'classroom-disconnect') {
        const google = S.classroom?.method === 'google';
        const message = google ? '가져온 Classroom 항목을 지우고 CalendarSync의 Google 읽기 권한을 해제해요.'
          : '가져온 Classroom 항목을 지우고 Apps Script 연결 키를 폐기해요.';
        openDialog('Classroom 연결 해제', `<p>${message}</p>`,
          `<button class="secondary" data-action="close">취소</button><button class="danger" data-action="classroom-disconnect-confirm" data-google="${google ? '1' : '0'}">연결 해제</button>`);
        return;
      }
      if (action === 'classroom-disconnect-confirm') {
        const epoch = authEpoch;
        const google = button.dataset.google === '1';
        await api(google ? '/api/classroom/google/connection' : '/api/classroom/connection', {method:'DELETE'});
        if (!activeView(epoch)) return;
        classroomRevision++;
        stopClassroomRefresh();
        S.classroom = {connected:false,snapshot:null};
        closeDialog();
        render();
        if (google) await load();
        return;
      }
      if (button.dataset.detail) {const e = find(button.dataset.detail, button.dataset.source); if (e) detail(e); return;}
      if (button.dataset.day) {S.selected = button.dataset.day; render(); return;}
      if (button.dataset.removeDate) {selectedDates.delete(button.dataset.removeDate); syncDates(); draftInputChanged(); return;}
      if (button.dataset.restore !== undefined) {const e = hiddenItems[Number(button.dataset.restore)]; return mutateState(e, {hidden: false}, '일정을 복원했어요.');}
      if (action === 'close') {closeDialog(); return;}
      if (action === 'today' || action === 'calendar' || (action === 'assignments' && assignmentsEnabled())) {S.view = action; if (S.me) render(); return;}
      if (action === 'refresh') {if (!S.refreshing) return await load(); return;}
      if (action === 'retry-boot') return await boot();
      if (action === 'new') return taskForm();
      if (action === 'filters') {if (S.prefs && S.ready.channels) return filters(); return;}
      if (action === 'hidden') return await hidden();
      if (action === 'feedback') return feedback();
      if (action === 'sync-teams') return syncTeams();
      if (action === 'changes') {if (S.ready.changes) return changesPanel(); return;}
      if (action === 'notifications') {if (S.prefs) return await notificationsPanel(); return;}
      if (action === 'push-enable') return await enablePush();
      if (action === 'push-disable') return await disablePush();
      if (action === 'push-test') {const result = await api('/api/push/test',{method:'POST'}); return toast(result.accepted ? '알림 전송을 요청했어요. 이 기기의 알림을 확인해 주세요.' : '알림 전송에 실패했어요. 서버 연결 상태를 확인해 주세요.', !result.accepted);}
      if (action === 'reminders') return await remindersPanel(detailItem);
      if (action === 'override-detail') return taskForm(detailItem);
      if (action === 'security') return openDialog('연결 및 보안', `<p>${esc(connectionText[S.connection?.state] || connectionText.not_connected)}</p><p>Teams 권한은 최대 60초 동안만 재사용해요. 화면을 오래 사용하지 않으면 다시 로그인이 필요해요. 기기 연동을 유지했다면 화면 세션 만료 후에도 알림은 받을 수 있어요. 직접 로그아웃하면 이 기기의 연동과 알림을 끊어요.</p><p>외부 캘린더 등록과 Teams 채팅 발송은 아직 제공하지 않아요. 브라우저 알림은 알림 설정에서 켜 주세요.</p><p><a href="/privacy">개인정보 안내</a></p>`, '<button class="danger" data-action="logout-all">모든 기기 로그아웃</button><button class="secondary" data-action="close">닫기</button>');
      if (action === 'logout-all') {await api('/auth/logout-all', {method:'POST'}); clearPrivate(); loginScreen(); if (testServer()) await detachBrowserPush(); return;}
      if (action === 'edit-detail') return taskForm(detailItem);
      if (action === 'hide-detail') return mutateState(detailItem, {hidden: true}, '숨겼어요. 숨긴 일정에서 복원할 수 있어요.');
      if (action === 'delete-detail') {
        const e = detailItem;
        openDialog('할 일을 삭제할까요?', `<p>${esc(e.title)}</p><p class="muted">${plannerEnabled() ? '삭제한 개인 할 일은 7일 동안 복구할 수 있어요.' : '삭제한 항목은 복원할 수 없어요.'}</p>`, '<button class="secondary" data-action="close">닫기</button><button class="danger" data-action="confirm-delete">삭제</button>');
        return;
      }
      if (action === 'confirm-delete') {const item = {...detailItem}; return mutate(() => plannerEnabled() ? api(`/api/tasks/${encodeURIComponent(item.id)}/trash`,{method:'POST',body:{version:item.version}}) : api(`/api/tasks/${encodeURIComponent(item.id)}?version=${item.version}`,{method:'DELETE'}), plannerEnabled() ? '삭제했어요. 삭제 취소나 삭제한 할 일에서 복구할 수 있어요.' : '삭제했어요.', false, result => {if (plannerEnabled()) undoTrash = result; applyConfirmedDelete(item);}, '삭제하는 중…');}
      if (action === 'pick-date') {const d = $('#pick-date'); if (d.value && d.checkValidity()) {selectedDates.add(d.value); syncDates(); draftInputChanged();} else d.reportValidity(); return;}
      if (['prev-month', 'next-month', 'this-month'].includes(action)) return await changeMonth(action);
      if (action === 'logout' || action === 'disconnect-device') {await api('/auth/logout', {method: 'POST'}); clearPrivate(); loginScreen(); if (action === 'disconnect-device') toast('이 기기의 연동과 알림을 해제했어요.'); if (testServer()) await detachBrowserPush();}
    } catch (error) {if (sessionEpoch === authEpoch) failure(error, ownerDialog);}
  });
  document.addEventListener('change', event => {
    const el = event.target;
    if (!S.today || S.loading) return;
    if (el.dataset.complete) {
      const e = find(el.dataset.complete, el.dataset.source); if (!e) return; const checked = el.checked; el.checked = e.completed;
      return mutateState(e, {completed: checked}, checked ? '완료했어요.' : '완료 표시를 해제했어요.');
    }
    if (el.closest('#task-form')) {if (['start_date','end_date','display_mode'].includes(el.name)) syncDates(); draftInputChanged();}
  });
  document.addEventListener('input', event => {
    const el = event.target;
    if (el.dataset.channelQuery !== undefined) {
      const form = el.closest('#filters-form');
      if (form) filterChannelRows(form, el.value);
    }
    if (plannerEnabled() && el.closest?.('#task-form')) draftInputChanged();
  });
  document.addEventListener('submit', async event => {
    event.preventDefault(); const form = event.target;
    if (S.busy) return;
    if (form.id !== 'login-form' && (!S.today || S.loading)) return;
    if (form.id === 'notification-form') return mutate(() => api('/api/settings',{method:'PUT',body:{...S.prefs,morning_enabled:form.elements.morning_enabled.checked,morning_time:form.elements.morning_time.value}}),'아침 알림 설정을 저장했어요.');
    if (form.id === 'reminder-form') {const item = detailItem; const offsets = (reminderRules?.offsets || []).filter(value => ![-1440,-60].includes(value)); if (form.elements.day.checked) offsets.push(-1440); if (form.elements.hour.checked) offsets.push(-60); const custom_times = form.elements.custom.value === reminderCustomInitial ? [...(reminderRules?.custom_times || [])] : form.elements.custom.value ? [new Date(form.elements.custom.value).toISOString()] : []; return mutate(() => api(`/api/reminders/${item.source}/${encodeURIComponent(item.id)}`,{method:'PUT',body:{offsets,custom_times}}),'예약 알림을 저장했어요.');}
    if (form.id === 'login-form') {
      S.busy = true; const button = form.querySelector('button'); button.disabled = true; $('#login-error').textContent = '';
      try {
        await api('/auth/beta', {method: 'POST', body: {code: form.elements.code.value.trim()}});
        form.elements.code.value = ''; S.me = await api('/api/me'); lastSessionCheckAt = Date.now(); S.view = 'today'; S.month = ''; S.selected = '';
        if (plannerEnabled()) {taskDrafts?.load(); refreshPlannerStatus({force:true});}
        await load();
      } catch (error) {if (form.isConnected) $('#login-error').textContent = error.message; else failure(error);}
      finally {S.busy = false; button.disabled = false; updateRefreshStatus();}
      return;
    }
    if (form.id === 'quick-form') {
      const title = form.elements.title.value.trim(); if (!title) return;
      const task = {source:'personal', title, start_date:S.day, end_date:S.day, display_mode:'daily', selected_dates:[], hidden:false, completed:false};
      return mutate(() => api('/api/tasks', {method: 'POST', body: {title, start_date: S.day, end_date: S.day, display_mode: 'daily'}}), '오늘 할 일에 추가했어요.', true, result => applyConfirmedState({...task,id:result.id}, {}, result));
    }
    if (form.id === 'task-form') {
      const data = Object.fromEntries(new FormData(form));
      data.time = data.time || null; data.selected_dates = data.display_mode === 'selected' ? [...selectedDates].filter(d => d >= data.start_date && d <= data.end_date) : [];
      const e = detailItem;
      if (!data.title.trim() || data.end_date < data.start_date) {$('#form-error').textContent = '제목과 시작·마지막 날짜를 확인해 주세요.'; return;}
      if (e?.source === 'teams' && testServer()) {delete data.display_mode; delete data.selected_dates; return mutate(() => api(`/api/events/${encodeURIComponent(e.id)}/override`, {method:'PUT',body:data}), '이 계정의 일정만 수정했어요.');}
      return mutate(async () => {const epoch = authEpoch, dialog = dialogEpoch; const draftResult = plannerEnabled() ? await taskDrafts?.flush() : null; if (!activeView(epoch) || dialog !== dialogEpoch || (plannerEnabled() && (autoReadsPaused || resumeCheck))) throw new Error('로그인 상태를 확인한 뒤 다시 저장해 주세요.'); const result = await api(e ? `/api/tasks/${encodeURIComponent(e.id)}` : '/api/tasks',{method:e ? 'PUT' : 'POST',body:{...data,...(e ? {version:e.version} : {})}}); if (activeView(epoch) && plannerEnabled()) await taskDrafts?.clearConfirmed(draftResult); return result;}, '할 일을 저장했어요.', false, result => applyConfirmedState({hidden:false,completed:false,...e,...data,id:e?.id || result.id,source:'personal'}, {}, result));
    }
    if (form.id === 'filters-form') {
      if (pausedFilters.has(form)) return;
      const channels = Object.fromEntries([...form.querySelectorAll('[name=channel]')].map(el => [el.value, el.checked]));
      const disabled_kinds = [...form.querySelectorAll('[name=kind]')].filter(el => !el.checked).map(el => el.value);
      if (SCHOOL) {
        const changed = S.channels.filter(ch => channels[ch.id] !== undefined && channels[ch.id] !== ch.enabled);
        const kindsChanged = JSON.stringify([...disabled_kinds].sort()) !== JSON.stringify([...S.prefs.disabled_kinds].sort());
        if (!changed.length && !kindsChanged) {modal.close(); toast('변경된 설정이 없어요.'); return;}
        return mutate(async () => {
          for (const channel of changed) {
            await api(`/api/channels/${encodeURIComponent(channel.id)}/preference`, {method:'PUT',body:{enabled:channels[channel.id]}});
            channel.enabled = channels[channel.id];
          }
          if (kindsChanged) await api('/api/settings', {method:'PUT',body:{...S.prefs,disabled_kinds}});
        }, '표시 설정을 저장했어요.');
      }
      return mutate(() => api('/api/beta/display-settings', {method: 'PUT', body: {channels, disabled_kinds}}), '표시 설정을 저장했어요.');
    }
    if (form.id === 'feedback-form') {
      const message = form.elements.message.value.trim(); if (!message) return;
      return mutate(() => api(testServer() ? '/api/feedback' : '/api/beta/feedback', {method: 'POST', body: {message}}), '의견을 이 베타 서버에 저장했어요.');
    }
  });
  // 서비스 설정과 로그인 사용자를 확인한 뒤 해당 세션의 초기 일정 화면을 불러온다.
  async function boot() {
    const epoch = authEpoch;
    if (location.protocol === 'file:') {root.innerHTML = '<section class="login-wrap"><div class="login-card"><h1>실행 파일로 시작해 주세요</h1><p>압축을 푼 뒤 start_beta.cmd를 실행하면 브라우저가 열려요. HTML 파일만 열면 데이터를 저장할 서버가 실행되지 않아요.</p></div></section>'; return;}
    root.innerHTML = '<section class="card"><div class="card-head"><h1>계정을 확인하고 있어요</h1></div><p class="empty" role="status">연결이 확인되면 일정 화면으로 이동해요.</p></section>';
    try {
      const service = await api(SCHOOL ? '/api/service' : '/api/beta/info');
      if (epoch !== authEpoch) return; S.service = service;
      if (S.service.mode === 'preview') {const footer = document.querySelector('footer'); if (footer) footer.textContent = 'CalendarSync · 예시 전용 미리보기 · 실제 Microsoft·Teams 연결 없음';}
      try {const me = await api('/api/me'); if (epoch !== authEpoch) return; S.me = me; lastSessionCheckAt = Date.now();} catch (error) {if (epoch !== authEpoch) return; if (error.status === 401) {loginScreen(); return;} throw error;}
      if (plannerEnabled()) {taskDrafts?.load(); refreshPlannerStatus({force:true});}
      await load();
      if (S.service?.classroom_google_enabled && ['?classroom=connected','?classroom=failed'].includes(location.search)) {
        const failed = location.search === '?classroom=failed';
        window.history?.replaceState(null, '', location.pathname);
        if (failed) toast('Google Classroom 연결을 완료하지 못했어요. 계정과 읽기 권한을 확인해 주세요.', true);
      }
    } catch (error) {
      if (epoch !== authEpoch) return;
      if (S.me) {failure(error); return;}
      clearPrivate(); root.innerHTML = '<section class="login-wrap"><div class="login-card" role="alert"><h1>연결을 확인하지 못했어요</h1><p>서버 응답이 늦거나 연결이 불안정할 수 있어요. 잠시 후 다시 연결해 주세요.</p><button class="primary" data-action="retry-boot">다시 연결</button></div></section>';
    }
  }
  // 수동 수집을 요청하고 진행 중인 저장이 정리된 뒤 최신 목록을 다시 불러온다.
  async function syncTeams() {
    if (!testServer() || S.syncing) return;
    const epoch = authEpoch;
    monthEpoch++; monthRequest?.abort(); monthRequest = null;
    S.syncing = true; S.syncMessage = 'Teams 공지를 가져오고 있어요. 다른 화면도 계속 사용할 수 있어요.'; updateSyncStatus();
    let ready = false;
    try {
      const result = await api('/api/teams/sync', {method:'POST', timeout:210000});
      if (!activeView(epoch)) return;
      const report = result.ingestion;
      const incomplete = !!(report?.partial || report?.messages_failed);
      S.syncMessage = report?.state === 'busy' ? '이미 Teams 공지를 확인하고 있어요. 잠시 후 새로고침으로 결과를 확인해 주세요.' : report?.state === 'queued' ? '저장된 일정을 표시했어요. 새 공지는 백그라운드에서 확인 중이에요. 잠시 후 새로고침으로 확인해 주세요.' : incomplete ? `최근 공지 중 일부를 확인했어요 (${report.channels_scanned}/${report.channels_total}개 채널).${report.messages_failed ? ' 처리하지 못한 공지가 있어 다음 수집에서 다시 확인해요.' : ' 남은 채널은 다음 수집에서 이어서 확인해요.'}` : connectionText[result.state] || '연결 상태를 확인해 주세요.';
      S.syncUncertain = report?.state === 'busy';
      if (S.syncUncertain) {toast(S.syncMessage); return;}
      ready = true;
      let revision;
      do {
        await mutationSettled;
        if (!activeView(epoch)) return;
        revision = mutationRevision;
        await load({background:true, afterSync:true, mutationVersion:revision});
      } while (activeView(epoch) && (S.busy || revision !== mutationRevision));
      if (activeView(epoch)) toast(S.syncMessage, incomplete || result.state !== 'connected');
    } catch (error) {
      if (activeView(epoch)) {S.syncUncertain = !ready; S.syncMessage = error.message || 'Teams 공지 수집 결과를 확인하지 못했어요.'; failure(error);}
    } finally {
      if (epoch === authEpoch) {
        S.syncing = false; updateSyncStatus(); refreshPlannerStatus({force:true});
        if (S.pendingMonth) {
          if (ready) await loadMonth(S.pendingMonth, S.pendingMonthSelected);
          else {S.pendingMonth = ''; render();}
        }
      }
    }
  }
  function changesPanel() {
    reviewDialogItems = new Map(reviewItems.map(item => [String(item.id), item]));
    openDialog('변경사항 검토', `<p class="muted">원문이 바뀐 일정은 확인 후 반영해요. 변경을 반영하면 현재 내 수정 내용을 대체해요. 검토 중인 일정의 예약 알림은 잠시 멈춰요.</p>${reviewItems.length ? reviewItems.map((change,i) => `<section class="change-item"><h3>${esc(change.before.title)}</h3><p class="fine">기존 · ${esc(period(change.before))} ${esc(change.before.time || '종일')}</p><p>변경 · ${esc(period(change.after))} ${esc(change.after.time || '종일')} ${change.after.cancelled ? '· 취소 또는 원문 삭제' : ''}</p><div class="detail-notes">${esc(change.after.notes || '')}</div><div class="button-row"><button class="secondary small" data-change-id="${esc(change.id)}" data-change-decision="keep">기존 유지</button><button class="primary small" data-change-id="${esc(change.id)}" data-change-decision="accept">변경 반영</button></div></section>`).join('') : '<div class="empty">검토할 변경사항이 없어요.</div>'}`, '', reviewItems.length > 0);
  }
  async function notificationsPanel() {
    const push = await readDialog('/api/push');
    if (!push) return;
    openDialog('이 기기의 알림', `<p>알림 상태: <strong>${push.registered ? '켜짐' : '꺼짐'}</strong></p><p class="muted">화면이 닫혀 있어도 지원되는 브라우저에서 알림을 받아요. 잠금화면에는 일정 제목 대신 확인 안내만 표시해요.</p>${!push.configured ? '<p class="error-message">서버의 Web Push 키가 아직 설정되지 않았어요.</p>' : ''}${!push.background_enabled ? '<p class="error-message">알림을 받으려면 다시 로그인할 때 이 기기 연동 유지를 선택해 주세요.</p>' : ''}<form id="notification-form"><label class="toggle-row">아침 요약 알림<input name="morning_enabled" type="checkbox" ${S.prefs.morning_enabled ? 'checked' : ''}></label><label class="field"><span class="field-label">아침 알림 시각</span><input name="morning_time" type="time" required value="${esc(S.prefs.morning_time || '07:00')}"></label><button class="secondary" type="submit">아침 설정 저장</button></form><p class="fine muted">아이폰·아이패드는 홈 화면에 추가한 웹앱에서 알림을 켜야 할 수 있어요. 브라우저·기기 설정에 따라 수신이 제한될 수 있어요.</p>`, `<button class="secondary" data-action="push-disable">이 기기 알림 끄기</button><button class="secondary" data-action="push-test" ${!push.registered ? 'disabled' : ''}>테스트 알림</button><button class="primary" data-action="push-enable" ${!push.configured || !push.background_enabled ? 'disabled' : ''}>이 기기 알림 켜기</button>`);
  }
  async function enablePush() {
    const epoch = authEpoch, ownerDialog = dialogEpoch;
    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) return failure(new Error('이 브라우저에서는 Web Push를 지원하지 않아요.'), ownerDialog);
    if (!activeView(epoch)) return;
    const permission = Notification.requestPermission().catch(() => 'denied');
    const info = await api('/api/push'); if (!activeView(epoch)) return; if (!info.configured || !info.background_enabled) return failure(new Error('연동과 서버 알림 설정을 먼저 확인해 주세요.'), ownerDialog);
    if (await permission !== 'granted') return failure(new Error('기기의 알림 권한이 허용되지 않았어요.'), ownerDialog);
    if (!activeView(epoch)) return;
    const registration = await navigator.serviceWorker.register('/service-worker.js'); await navigator.serviceWorker.ready;
    if (!activeView(epoch)) return;
    let subscription = await registration.pushManager.getSubscription();
    if (!activeView(epoch)) return;
    if (subscription && !info.registered) {await subscription.unsubscribe(); subscription = null;}
    if (!activeView(epoch)) return;
    if (!subscription) {
      const value = info.public_key.replace(/-/g,'+').replace(/_/g,'/'); const raw = atob(value + '='.repeat((4-value.length%4)%4));
      subscription = await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:Uint8Array.from(raw,c=>c.charCodeAt(0))});
    }
    if (!activeView(epoch)) {if (subscription) await subscription.unsubscribe(); return;}
    await api('/api/push',{method:'POST',body:subscription.toJSON()}); if (!activeView(epoch)) return; await load(); if (activeView(epoch) && ownerDialog === dialogEpoch) return notificationsPanel();
  }
  async function detachBrowserPush() {
    try {if ('serviceWorker' in navigator) {const registration = await navigator.serviceWorker.getRegistration('/'); if (registration) {const subscription = await registration.pushManager.getSubscription(); if (subscription) await subscription.unsubscribe(); registration.active?.postMessage({type:'clear-notifications'});}}} catch {}
  }
  async function disablePush() {const epoch = authEpoch, ownerDialog = dialogEpoch; await api('/api/push',{method:'DELETE'}); await detachBrowserPush(); if (activeView(epoch) && ownerDialog === dialogEpoch) return notificationsPanel();}
  async function remindersPanel(item) {
    const original = {...item}, rules = await readDialog(`/api/reminders/${item.source}/${encodeURIComponent(item.id)}`);
    if (!rules) return; detailItem = original; reminderRules = rules;
    const custom = rules.custom_times?.[0] ? new Date(rules.custom_times[0]) : null;
    reminderCustomInitial = custom && !Number.isNaN(custom.getTime()) ? new Date(custom.getTime()-custom.getTimezoneOffset()*60000).toISOString().slice(0,16) : '';
    openDialog('이 일정의 예약 알림', `<form id="reminder-form"><p>${esc(item.title)}</p><label class="toggle-row">하루 전<input name="day" type="checkbox" ${rules.offsets.includes(-1440) ? 'checked' : ''}></label><label class="toggle-row">1시간 전<input name="hour" type="checkbox" ${rules.offsets.includes(-60) ? 'checked' : ''}></label><label class="field"><span class="field-label">직접 지정 (기기 현지 시각, 선택)</span><input name="custom" type="datetime-local" value="${esc(reminderCustomInitial)}"></label><p class="fine muted">완료·취소·숨김 또는 꺼둔 채널의 일정은 알림에서 제외돼요. 직접 지정한 시각은 새로 저장한 값으로 바뀌어요.</p><div class="dialog-foot"><button class="primary" type="submit">저장</button></div></form>`);
  }
  function sourceLink(value) {
    if (!SCHOOL || !value) return '';
    try {const url = new URL(value); if (url.protocol !== 'https:' || url.hostname !== 'teams.microsoft.com' || !url.pathname.startsWith('/l/message/')) return ''; return `<p><a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">Teams 원문 보기 →</a></p>`;} catch {return '';}
  }
  function expireIdle() {
    if (S.me && idleDeadline && Date.now() >= idleDeadline) {
      clearPrivate(); loginScreen(); return true;
    }
    return false;
  }
  function resetIdle() {
    if (!SCHOOL || document.hidden || expireIdle()) return;
    clearTimeout(idleTimer);
    const delay = (S.service?.idle_seconds || 1800) * 1000;
    idleDeadline = Date.now() + delay;
    idleTimer = setTimeout(expireIdle, delay);
    if (S.me && Date.now() - lastSessionCheckAt >= 60000) checkResumedSession();
  }
  // 화면 복귀 시 자동 조회를 잠시 멈추고 로그인 사용자와 CSRF 토큰이 같은 세션인지 재확인한다.
  async function checkResumedSession() {
    if (!S.me || resumeCheck) return;
    const check = {epoch: authEpoch, id: S.me.id, csrf: S.me.csrf_token};
    resumeCheck = check; autoReadsPaused = true;
    lastSessionCheckAt = Date.now();
    try {
      // Revalidate the session without repeating Teams reads or replacing the UI.
      const me = await api('/api/me');
      if (!activeView(check.epoch) || expireIdle()) return;
      if (me.id !== check.id || me.csrf_token !== check.csrf) {clearPrivate(); loginScreen();}
      else autoReadsPaused = false;
    } catch (error) {
      if (activeView(check.epoch)) {
        if ([401, 403].includes(error.status)) failure(error);
        else toast('로그인 연결을 확인하지 못했어요. 잠시 후 다시 시도해 주세요.', true);
      }
    } finally {
      if (resumeCheck === check) {
        resumeCheck = null; updateRefreshStatus();
        if (activeView(check.epoch) && !autoReadsPaused) {taskDrafts?.changed(); refreshPlannerStatus({force:true});}
        if (activeView(check.epoch)) {scheduleCheckingRefresh(); scheduleIngestionRefresh(); scheduleClassroomRefresh(0);}
      }
    }
  }
  if (L) listControls = L.bind(root,{getState:()=>S.listFilters,onChange:next=>{S.listFilters=next;render();},enabled:()=>!!S.me && plannerEnabled(),onCompositionEnd:()=>render()});
  if (window.BetaTaskDrafts) taskDrafts = window.BetaTaskDrafts.create({api:(path,options)=>api(path,{timeout:15000,...options}),getIdentity:()=>S.me ? {id:S.me.id,epoch:authEpoch} : null,canPersist:()=>plannerEnabled() && !!S.me && !document.hidden && !autoReadsPaused && !resumeCheck,readDraft:readCurrentDraft,hasForm:personalForm,isDialogCurrent:context=>context===dialogEpoch,onStateChange:state=>{const available=!!draftUi?.available;draftUi=state;updateDraftStatus();if (available!==!!state.available && S.me && S.today) render();},onError:(error,{context})=>{if ([401,403].includes(error.status)) failure(error);else if (context===dialogEpoch) {noteMissingTask(error,context); updateDraftStatus();}}});
  window.addEventListener?.('beforeunload',event=>{if (S.me && plannerEnabled() && taskDrafts?.hasUnsaved()) {event.preventDefault();event.returnValue='';}});
  if (SCHOOL) {
    for (const type of ['pointerdown','keydown','touchstart']) document.addEventListener(type,resetIdle,{passive:true});
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {clearTimeout(ingestionTimer); ingestionTimer = null; stopClassroomRefresh();}
      else if (!expireIdle()) {checkResumedSession(); scheduleCheckingRefresh(); scheduleIngestionRefresh();}
    });
    resetIdle();
  }
  boot();
})();

