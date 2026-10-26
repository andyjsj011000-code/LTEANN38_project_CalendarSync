'use strict';
(() => {
  const $ = s => document.querySelector(s);
  const SCHOOL = document.documentElement?.dataset.mode === 'school';
  let idleTimer;
  const connectionText = {connected:'Teams 권한과 일정 연결을 확인했어요.', checking:'Teams 접근 권한을 확인하고 있어요.', not_connected:'Teams 연결을 확인해 주세요.', source_not_configured:'학교 일정 저장소가 아직 연결되지 않았어요. 개인 할 일은 사용할 수 있어요.', source_unavailable:'학교 일정을 가져오지 못해 Teams 표시를 중단했어요. 개인 할 일은 사용할 수 있어요.', verification_failed:'Teams 접근 권한을 확인하지 못해 표시를 중단했어요.', reauth_required:'Microsoft 계정으로 다시 로그인해 주세요.', account_not_allowed:'학교 소속 계정인지 확인해 주세요.'};
  const root = $('#app'), modal = $('#modal'), topbar = $('#topbar');
  const C = window.BetaCalendar;
  const S = {me: null, view: 'today', day: '', month: '', selected: '', today: null, items: [], upcoming: [], channels: [], prefs: null, busy: false, epoch: 0};
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const kindName = {assignment: '과제', application: '신청', event: '행사·일정'};
  const pretty = d => new Intl.DateTimeFormat('ko-KR', {month: 'long', day: 'numeric', weekday: 'short', timeZone: 'UTC'}).format(new Date(d + 'T00:00:00Z'));
  const period = e => e.start_date === e.end_date ? pretty(e.start_date) : `${pretty(e.start_date)} ~ ${pretty(e.end_date)}`;
  let toastTimer;
  function toast(message, error = false) {
    const el = $('#toast'); el.textContent = message; el.className = 'toast' + (error ? ' error' : ''); el.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => {el.hidden = true;}, 6000);
  }
  async function api(path, options = {}) {
    const {body, ...rest} = options;
    const response = await fetch(path, {...rest, credentials: 'same-origin', headers: {'Content-Type': 'application/json', ...(S.me ? {'x-csrf-token': S.me.csrf_token} : {})}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    if (response.status === 204) return null;
    const value = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = response.status === 409 ? '다른 화면에서 변경되었어요. 새로고침한 뒤 다시 시도해 주세요.' : typeof value.detail === 'string' ? value.detail : '입력 내용과 날짜를 확인해 주세요.';
      const error = new Error(message); error.status = response.status; throw error;
    }
    return value;
  }
  function failure(error) {
    if (error.status === 401) {clearPrivate(); loginScreen(); toast('다시 로그인해 주세요.', true);}
    else toast(error.message || '연결하지 못했어요. 실행 창이 열려 있는지 확인해 주세요.', true);
  }
  function clearPrivate() {
    clearTimeout(idleTimer);
    S.epoch++; S.me = null; S.items = []; S.upcoming = []; S.today = null; S.channels = []; S.prefs = null; S.busy = false;
    detailItem = null; hiddenItems = []; selectedDates.clear();
    if (modal.open) modal.close(); modal.replaceChildren(); topbar.hidden = true; $('#account-name').textContent = '';
  }
  function loginScreen() {
    if (SCHOOL) {root.innerHTML = `<section class="login-wrap"><div class="login-card"><div class="login-logo">C</div><p class="eyebrow">CALENDARSYNC</p><h1>학교 일정과 할 일,<br>한눈에.</h1><p>학교 Microsoft 계정으로 로그인하세요.</p><a class="primary school-signin" href="/auth/login">Microsoft 학교 계정으로 로그인 →</a><p class="login-note">Microsoft 로그인 화면에서 인증해요. 이 서비스는 계정 비밀번호를 받지 않아요. 본인이 접근할 수 있는 Teams 일정만 표시하며, 외부 캘린더 등록과 알림은 아직 제공하지 않아요.</p><p><a href="/privacy">개인정보 안내</a></p><p class="error-message" id="login-error" role="alert">${location.search?.includes('login=failed') ? '로그인을 완료하지 못했어요. 학교 계정과 앱 권한을 확인하고 다시 시도해 주세요.' : ''}</p></div></section>`; return;}
    root.innerHTML = `<section class="login-wrap"><div class="login-card"><div class="login-logo">C</div><p class="eyebrow">CALENDARSYNC · BETA</p><h1>오늘 해야 할 일,<br>한눈에.</h1><p class="muted">학교 로그인 없이 초대 코드로 먼저 써보세요.</p><form id="login-form"><label class="field"><span class="field-label">테스트 계정 초대 코드</span><input name="code" type="password" required minlength="16" maxlength="128" autocomplete="off" spellcheck="false" placeholder="실행 창에 표시된 코드를 입력하세요"></label><p class="error-message" id="login-error" role="alert"></p><button class="primary" type="submit">베타 시작하기 →</button></form><p class="login-note">Teams 공지는 예시 데이터입니다. 개인 할 일은 계정별로 서버에 저장됩니다. 같은 코드로 로그인하면 같은 계정이 열려요.</p></div></section>`;
  }
  async function load() {
    const epoch = ++S.epoch;
    const today = await api('/api/today');
    const month = S.month || today.date.slice(0, 7), grid = C.monthGrid(month);
    const [calendar, upcoming, channels, settings] = await Promise.all([
      api(`/api/calendar?start=${grid.start}&end=${grid.end}`),
      api(`/api/calendar?start=${today.date}&end=${C.addDays(today.date, 7)}`),
      api('/api/channels'), api('/api/settings')
    ]);
    if (epoch !== S.epoch || !S.me) return;
    Object.assign(S, {today, day: today.date, month, selected: S.selected || today.date, items: calendar.items, upcoming: upcoming.items, channels, prefs: settings.preferences, connection: settings.connection});
    render();
  }
  function row(e) {
    return `<div class="task-row ${e.source === 'personal' ? 'personal' : ''} ${e.completed ? 'done' : ''} ${e.cancelled ? 'cancelled' : ''}"><input aria-label="${esc(e.title)} 완료" type="checkbox" data-complete="${esc(e.id)}" data-source="${e.source}" ${e.completed ? 'checked' : ''} ${e.cancelled ? 'disabled' : ''}><button class="task-main" data-detail="${esc(e.id)}" data-source="${e.source}"><span class="task-title">${esc(e.title)} ${e.cancelled ? '<span class="badge">취소</span>' : ''}</span><span class="task-meta">${esc(e.channel || '개인 할 일')} · ${esc(e.start_date !== e.end_date ? period(e) : kindName[e.kind] || e.kind)}</span></button><span class="task-time">${esc(e.time || '종일')}</span></div>`;
  }
  function list(items, empty = '표시할 할 일이 없어요.') {return items.length ? items.map(row).join('') : `<div class="empty">${empty}</div>`;}
  function intro(title, subtitle) {
    if (SCHOOL) return `<div class="intro"><div><p class="eyebrow">나의 학교생활 플래너</p><h1>${esc(title)}</h1><p class="muted">${esc(subtitle)}</p></div><div class="actions"><button class="secondary" data-action="refresh">새로고침</button><button class="secondary" data-action="filters">표시 설정</button><button class="primary" data-action="new">＋ 할 일 추가</button></div></div><div class="beta-banner" role="status"><strong>학교 계정 연결</strong><span>${esc(connectionText[S.connection?.state] || connectionText.not_connected)}</span><a href="/auth/login">다시 로그인</a><button data-action="security">연결 및 보안</button></div>`;
    return `<div class="intro"><div><p class="eyebrow">나의 학교생활 플래너</p><h1>${esc(title)}</h1><p class="muted">${esc(subtitle)}</p></div><div class="actions"><button class="secondary" data-action="refresh">새로고침</button><button class="secondary" data-action="filters">표시 설정</button><button class="primary" data-action="new">＋ 할 일 추가</button></div></div><div class="beta-banner"><strong>학교 로그인 전 베타</strong><span>Teams 일정은 예시예요. 직접 추가한 할 일은 저장돼요. 알림 발송과 실제 Teams 연결은 준비 중이에요.</span></div>`;
  }
  function render() {
    topbar.hidden = false; $('#account-name').textContent = S.me.name;
    $('#nav-today').classList.toggle('active', S.view === 'today'); $('#nav-calendar').classList.toggle('active', S.view === 'calendar');
    if (S.view === 'calendar') return renderCalendar();
    const all = [...S.today.teams, ...S.today.personal].filter(e => !e.cancelled), done = all.filter(e => e.completed).length;
    const upcoming = S.upcoming.filter(e => e.end_date > S.day && !e.completed && !e.cancelled).sort((a, b) => a.end_date.localeCompare(b.end_date) || (a.time || '').localeCompare(b.time || '')).slice(0, 5);
    root.innerHTML = intro('오늘 할 일', pretty(S.day)) + `<div class="today-layout"><div class="stack"><section class="card"><div class="card-head"><h2><span class="dot"></span>Teams 일정 <span class="count">${S.today.teams.length}</span></h2><span class="muted fine">${SCHOOL ? '학교 공지' : '예시 공지'}</span></div><div class="list">${list(S.today.teams, '오늘의 Teams 일정이 없어요.')}</div></section><section class="card"><div class="card-head"><h2><span class="dot personal"></span>개인 할 일 <span class="count">${S.today.personal.length}</span></h2></div><div class="list">${list(S.today.personal, '오늘 할 일을 가볍게 적어보세요.')}</div><form class="quick-form" id="quick-form"><input name="title" aria-label="오늘 할 일" maxlength="500" required placeholder="오늘 할 일 빠르게 추가"><button class="primary" type="submit">추가</button></form></section></div><aside class="stack"><section class="card progress-card"><p class="eyebrow">오늘의 진행</p><h2>${done}<span class="muted"> / ${all.length} 완료</span></h2><div class="progress-track"><div class="progress-fill" style="width:${all.length ? Math.round(done / all.length * 100) : 0}%"></div></div><p class="muted fine">완료한 항목은 원래 위치에 남아요.</p></section><section class="card"><div class="card-head"><h2>다가오는 일정</h2><button class="text-button" data-action="calendar">전체 보기 →</button></div><div class="upcoming-list">${upcoming.length ? upcoming.map(e => `<button class="upcoming-item" data-detail="${esc(e.id)}" data-source="${e.source}"><span class="date-stamp">${esc(e.end_date.slice(5).replace('-', '/'))}</span><span class="upcoming-text"><strong>${esc(e.title)}</strong><small>${esc(e.time || '종일')} · ${esc(e.source === 'teams' ? (SCHOOL ? 'Teams' : 'Teams 예시') : '개인')}</small></span></button>`).join('') : '<div class="empty">일주일 안에 다가오는 마감이 없어요.</div>'}</div></section><section class="card help-card"><h2>${SCHOOL ? '내 일정 관리' : '함께 시험해 보세요'}</h2><p class="muted">기간을 정해 할 일을 추가하고, 캘린더에서 이어지는 막대를 확인해 보세요.</p><div class="button-row"><button class="secondary small" data-action="hidden">숨긴 일정</button>${SCHOOL ? '<button class="secondary small" data-action="security">연결 및 보안</button>' : '<button class="secondary small" data-action="feedback">의견 남기기</button>'}</div></section></aside></div>`;
  }
  function renderCalendar() {
    const weeks = C.layoutRanges(S.items, S.month);
    const gridHtml = weeks.map(week => {
      const rows = week.lanes + 2;
      return `<div class="calweek" style="grid-template-rows:30px ${week.lanes ? `repeat(${week.lanes},30px) ` : ''}minmax(55px,auto)">${week.days.map((day, i) => `<button aria-label="${esc(pretty(day))}" class="cal-day ${day === S.selected ? 'selected' : ''}" data-day="${day}" style="grid-column:${i + 1};grid-row:1 / span ${rows}"></button><span class="cal-date ${day.slice(0, 7) !== S.month ? 'outside' : ''} ${day === S.day ? 'today' : ''}" style="grid-column:${i + 1};grid-row:1">${Number(day.slice(8))}</span>`).join('')}${week.segments.map(s => `<button title="${esc(s.item.title + ' · ' + period(s.item))}" aria-label="${esc(s.item.title + ' · ' + period(s.item))}" data-detail="${esc(s.item.id)}" data-source="${s.item.source}" class="range-event ${s.item.source === 'personal' ? 'personal' : ''} ${s.before ? 'before' : ''} ${s.after ? 'after' : ''} ${s.item.completed ? 'done' : ''} ${s.item.cancelled ? 'cancelled' : ''}" style="grid-column:${s.first + 1} / ${s.last + 2};grid-row:${s.lane + 2}"><span class="range-title">${s.before ? '‹ ' : ''}${esc(s.item.title)}${s.item.cancelled ? ' · 취소' : ''}${s.after ? ' ›' : ''}</span>${s.endsHere ? '<span class="deadline-tag">마감</span>' : ''}</button>`).join('')}${week.days.map((day, i) => `<div class="single-day" style="grid-column:${i + 1};grid-row:${rows}">${S.items.filter(e => e.start_date === day && e.end_date === day).map(e => `<button data-detail="${esc(e.id)}" data-source="${e.source}" title="${esc(e.title)}" class="single-event ${e.source === 'personal' ? 'personal' : ''} ${e.completed ? 'done' : ''} ${e.cancelled ? 'cancelled' : ''}"><span class="single-title">${esc(e.title)}${e.cancelled ? ' · 취소' : ''}</span><span class="single-time">${esc(e.time || '')}</span></button>`).join('')}</div>`).join('')}</div>`;
    }).join('');
    const selected = S.items.filter(e => e.start_date <= S.selected && e.end_date >= S.selected);
    const monthLabel = `${S.month.slice(0, 4)}년 ${Number(S.month.slice(5))}월`;
    root.innerHTML = intro('캘린더', '기간이 있는 일정은 시작일부터 마감일까지 이어져요.') + `<section class="card calendar-card"><div class="calendar-toolbar"><h2>${monthLabel}</h2><div class="month-actions"><button class="icon-button" aria-label="이전 달" data-action="prev-month">‹</button><button class="secondary small" data-action="this-month">이번 달</button><button class="icon-button" aria-label="다음 달" data-action="next-month">›</button></div></div><div class="calendar-legend"><span><i class="dot"></i>${SCHOOL ? 'Teams' : 'Teams 예시'}</span><span><i class="dot personal"></i>개인 할 일</span><span class="muted">날짜를 누르면 아래에 상세 목록이 보여요.</span></div><div class="calendar-grid"><div class="weekdays">${['일', '월', '화', '수', '목', '금', '토'].map(d => `<div class="weekday">${d}</div>`).join('')}</div>${gridHtml}</div></section><section class="card" style="margin-top:20px"><div class="card-head selected-heading"><h2>${esc(pretty(S.selected))}</h2><span class="muted fine">기간 전체 기준 · 오늘 표시는 선택한 날짜를 따릅니다</span></div><div class="date-detail">${list(selected, '이 날짜에는 표시할 일정이 없어요.')}</div></section>`;
  }
  function openDialog(title, content, footer = '') {
    if (modal.open) modal.close();
    modal.innerHTML = `<div class="dialog-head"><h2 id="dialog-title">${esc(title)}</h2><button class="dialog-close" aria-label="닫기" data-action="close">×</button></div><div class="dialog-body">${content}</div>${footer ? `<div class="dialog-foot">${footer}</div>` : ''}`;
    modal.showModal();
  }
  function find(id, source) {return [...(S.today?.teams || []), ...(S.today?.personal || []), ...S.items, ...S.upcoming].find(e => e.id === id && e.source === source);}
  let detailItem = null;
  function detail(e) {
    detailItem = e;
    openDialog(e.title, `<p class="detail-meta">${esc(e.source === 'personal' ? '직접 작성한 할 일' : e.channel)} · ${esc(e.source === 'personal' ? '개인' : kindName[e.kind] || e.kind)}</p><p>${esc(period(e))} ${esc(e.time || '종일')}</p>${e.cancelled ? '<p class="badge">취소된 일정</p>' : ''}${e.source === 'personal' ? `<p class="muted">오늘 표시: ${esc({daily: '기간 중 매일', selected: '선택한 날짜와 마지막 날', final: '마지막 날만'}[e.display_mode])}</p>${e.display_mode === 'selected' ? `<p class="fine muted">${e.selected_dates.map(esc).join(', ')}</p>` : ''}` : (SCHOOL ? '<p class="muted fine">학교 공지에서 추출한 일정이에요. 정확한 내용은 원문에서 확인해 주세요.</p>' + sourceLink(e.source_url) : '<p class="muted fine">실제 학교 공지가 아닌 베타용 예시입니다.</p>')}<div class="detail-notes">${esc(e.notes || '메모가 없어요.')}</div>`, `<button class="secondary" data-action="hide-detail">숨기기</button>${e.source === 'personal' ? '<button class="danger" data-action="delete-detail">삭제</button><button class="primary" data-action="edit-detail">수정</button>' : ''}`);
  }
  function taskForm(e = null) {
    detailItem = e;
    const day = S.view === 'calendar' ? S.selected : S.day;
    const val = e || {title: '', notes: '', start_date: day, end_date: day, time: '', display_mode: 'daily', selected_dates: []};
    openDialog(e ? '개인 할 일 수정' : '개인 할 일 추가', `<form id="task-form"><label class="field"><span class="field-label">무엇을 할까요?</span><input name="title" required maxlength="500" placeholder="예: 1주치 숙제하기, 개발 완료하기" value="${esc(val.title)}"></label><div class="form-two"><label class="field"><span class="field-label">시작 날짜</span><input name="start_date" type="date" required value="${val.start_date}"></label><label class="field"><span class="field-label">마지막 날짜</span><input name="end_date" type="date" required value="${val.end_date}"></label></div><label class="field"><span class="field-label">마감 시간 (선택)</span><input name="time" type="time" value="${esc(val.time || '')}"></label><label class="field"><span class="field-label">오늘 할 일에 표시할 날짜</span><select name="display_mode"><option value="daily" ${val.display_mode === 'daily' ? 'selected' : ''}>기간 중 매일</option><option value="selected" ${val.display_mode === 'selected' ? 'selected' : ''}>선택한 날짜 + 마지막 날</option><option value="final" ${val.display_mode === 'final' ? 'selected' : ''}>마지막 날만</option></select></label><div id="selected-date-field" ${val.display_mode === 'selected' ? '' : 'hidden'}><div class="date-picker-row"><input type="date" id="pick-date" aria-label="표시할 날짜"><button type="button" class="secondary small" data-action="pick-date">날짜 추가</button></div><div class="date-chips" id="date-chips"></div><p class="fine muted">마지막 날에는 항상 표시됩니다.</p></div><label class="field"><span class="field-label">메모 (선택)</span><textarea name="notes" rows="4" maxlength="10000">${esc(val.notes)}</textarea></label><p class="fine muted">캘린더에는 기간 전체가 이어져 표시돼요. 지난 미완료 항목은 다음 날로 옮겨지지 않아요.</p><p class="error-message" id="form-error" role="alert"></p><div class="dialog-foot"><button type="button" class="secondary" data-action="close">닫기</button><button class="primary" type="submit">저장</button></div></form>`);
    selectedDates = new Set(val.selected_dates); syncDates();
  }
  let selectedDates = new Set();
  function syncDates() {
    const f = $('#task-form'); if (!f) return;
    const a = f.elements.start_date.value, b = f.elements.end_date.value;
    f.elements.end_date.min = a;
    selectedDates = new Set([...selectedDates].filter(d => d >= a && d <= b));
    $('#selected-date-field').hidden = f.elements.display_mode.value !== 'selected';
    $('#pick-date').min = a; $('#pick-date').max = b; $('#pick-date').value = a;
    $('#date-chips').innerHTML = [...new Set([...selectedDates, b])].filter(Boolean).sort().map(d => `<button type="button" class="secondary small" data-remove-date="${d}" ${d === b ? 'disabled' : ''}>${esc(d)} ${d === b ? '· 마지막 날' : '×'}</button>`).join('');
  }
  async function mutate(operation, message) {
    if (S.busy) return;
    S.busy = true;
    const epoch = S.epoch;
    document.querySelectorAll('button,input,select,textarea').forEach(el => {if (!el.disabled) {el.disabled = true; el.dataset.locked = '1';}});
    let saved = false;
    try {
      await operation(); saved = true;
      if (epoch !== S.epoch) return;
      if (modal.open) modal.close(); modal.replaceChildren();
      await load(); toast(message);
    } catch (error) {
      if (saved) toast('저장됐지만 화면을 다시 불러오지 못했어요. 새로고침해 주세요.', true);
      else failure(error);
    } finally {
      S.busy = false;
      document.querySelectorAll('[data-locked]').forEach(el => {el.disabled = false; delete el.dataset.locked;});
    }
  }
  function setState(e, values) {
    return api(`/api/${e.source === 'personal' ? 'tasks' : 'events'}/${encodeURIComponent(e.id)}/state`, {method: 'PATCH', body: {...values, ...(e.source === 'personal' ? {version: e.version} : {})}});
  }
  function filters() {
    openDialog('표시 설정', `<form id="filters-form"><h3>Teams 채널</h3>${S.channels.map(ch => `<label class="toggle-row"><span>${esc(ch.name)}</span><input type="checkbox" name="channel" value="${esc(ch.id)}" ${ch.enabled ? 'checked' : ''}></label>`).join('')}<h3>일정 종류</h3>${Object.entries(kindName).map(([key, label]) => `<label class="toggle-row"><span>${label}</span><input type="checkbox" name="kind" value="${key}" ${S.prefs.disabled_kinds.includes(key) ? '' : 'checked'}></label>`).join('')}<p class="fine muted">설정은 내 계정에만 적용돼요.</p><div class="dialog-foot"><button type="button" class="secondary" data-action="close">닫기</button><button class="primary" type="submit">적용</button></div></form>`);
  }
  let hiddenItems = [];
  async function hidden() {
    const epoch = S.epoch, result = await api(SCHOOL ? `/api/hidden?start=${S.day.slice(0,4)}-01-01&end=${S.day.slice(0,4)}-12-31` : '/api/beta/hidden');
    if (epoch !== S.epoch) return;
    hiddenItems = result.items;
    openDialog('숨긴 일정', `<p class="muted">개별적으로 숨긴 일정이에요. 복원해도 꺼둔 채널·종류 설정은 유지돼요.</p><div class="hidden-list">${hiddenItems.length ? hiddenItems.map((e, i) => `<div class="hidden-item"><div><strong>${esc(e.title)}</strong><p class="fine muted">${esc(period(e))} · ${esc(e.source === 'teams' ? (SCHOOL ? 'Teams' : 'Teams 예시') : '개인')}</p></div><button class="secondary small" data-restore="${i}">복원</button></div>`).join('') : '<div class="empty">숨긴 일정이 없어요.</div>'}</div>`);
  }
  function feedback() {
    openDialog('베타 의견 남기기', `<form id="feedback-form"><p class="muted">불편한 점이나 원하는 기능을 적어주세요. 이 베타 서버에 저장되며, 운영자가 나중에 확인할 수 있어요.</p><label class="field"><span class="field-label">의견</span><textarea name="message" rows="6" required maxlength="4000" placeholder="어느 화면에서 무엇이 불편했나요?"></textarea></label><div class="dialog-foot"><button type="button" class="secondary" data-action="close">닫기</button><button class="primary" type="submit">저장</button></div></form>`);
  }
  document.addEventListener('click', async event => {
    const button = event.target.closest('button'); if (!button || S.busy) return;
    try {
      if (button.dataset.detail) {const e = find(button.dataset.detail, button.dataset.source); if (e) detail(e); return;}
      if (button.dataset.day) {S.selected = button.dataset.day; render(); return;}
      if (button.dataset.removeDate) {selectedDates.delete(button.dataset.removeDate); syncDates(); return;}
      if (button.dataset.restore !== undefined) {const e = hiddenItems[Number(button.dataset.restore)]; return mutate(() => setState(e, {hidden: false}), '일정을 복원했어요.');}
      const action = button.dataset.action;
      if (action === 'close') {modal.close(); return;}
      if (action === 'today' || action === 'calendar') {S.view = action; if (S.today) render(); return;}
      if (action === 'refresh') return await load();
      if (action === 'new') return taskForm();
      if (action === 'filters') return filters();
      if (action === 'hidden') return await hidden();
      if (action === 'feedback') return feedback();
      if (action === 'security') return openDialog('연결 및 보안', `<p>${esc(connectionText[S.connection?.state] || connectionText.not_connected)}</p><p>Teams 권한은 최대 60초 동안만 재사용해요. 활동이 없으면 15분 뒤 로그아웃되며, 토큰이 만료되면 다시 로그인해야 해요.</p><p>외부 캘린더 등록과 알림 전송은 현재 제공하지 않아요.</p><p><a href="/privacy">개인정보 안내</a></p>`, '<button class="danger" data-action="logout-all">모든 기기 로그아웃</button><button class="secondary" data-action="close">닫기</button>');
      if (action === 'logout-all') {await api('/auth/logout-all', {method:'POST'}); clearPrivate(); loginScreen(); return;}
      if (action === 'edit-detail') return taskForm(detailItem);
      if (action === 'hide-detail') return mutate(() => setState(detailItem, {hidden: true}), '숨겼어요. 숨긴 일정에서 복원할 수 있어요.');
      if (action === 'delete-detail') {
        const e = detailItem;
        openDialog('할 일을 삭제할까요?', `<p>${esc(e.title)}</p><p class="muted">삭제한 항목은 복원할 수 없어요.</p>`, '<button class="secondary" data-action="close">닫기</button><button class="danger" data-action="confirm-delete">삭제</button>');
        return;
      }
      if (action === 'confirm-delete') return mutate(() => api(`/api/tasks/${encodeURIComponent(detailItem.id)}?version=${detailItem.version}`, {method: 'DELETE'}), '삭제했어요.');
      if (action === 'pick-date') {const d = $('#pick-date'); if (d.value && d.checkValidity()) {selectedDates.add(d.value); syncDates();} else d.reportValidity(); return;}
      if (['prev-month', 'next-month', 'this-month'].includes(action)) {
        if (action === 'this-month') {S.month = S.day.slice(0, 7); S.selected = S.day;}
        else {const date = new Date(S.month + '-01T00:00:00Z'); date.setUTCMonth(date.getUTCMonth() + (action === 'next-month' ? 1 : -1)); S.month = date.toISOString().slice(0, 7); S.selected = S.month + '-01';}
        return await load();
      }
      if (action === 'logout') {await api('/auth/logout', {method: 'POST'}); clearPrivate(); loginScreen();}
    } catch (error) {failure(error);}
  });
  document.addEventListener('change', event => {
    const el = event.target;
    if (el.dataset.complete) {
      const e = find(el.dataset.complete, el.dataset.source); const checked = el.checked; el.checked = e.completed;
      return mutate(() => setState(e, {completed: checked}), checked ? '완료했어요.' : '완료 표시를 해제했어요.');
    }
    if (el.closest('#task-form') && ['start_date', 'end_date', 'display_mode'].includes(el.name)) syncDates();
  });
  document.addEventListener('submit', async event => {
    event.preventDefault(); const form = event.target;
    if (S.busy) return;
    if (form.id === 'login-form') {
      S.busy = true; const button = form.querySelector('button'); button.disabled = true; $('#login-error').textContent = '';
      try {
        await api('/auth/beta', {method: 'POST', body: {code: form.elements.code.value.trim()}});
        form.elements.code.value = ''; S.me = await api('/api/me'); S.view = 'today'; S.month = ''; S.selected = '';
        await load();
      } catch (error) {if (form.isConnected) $('#login-error').textContent = error.message; else failure(error);}
      finally {S.busy = false; button.disabled = false;}
      return;
    }
    if (form.id === 'quick-form') {
      const title = form.elements.title.value.trim(); if (!title) return;
      return mutate(() => api('/api/tasks', {method: 'POST', body: {title, start_date: S.day, end_date: S.day, display_mode: 'daily'}}), '오늘 할 일에 추가했어요.');
    }
    if (form.id === 'task-form') {
      const data = Object.fromEntries(new FormData(form));
      data.time = data.time || null; data.selected_dates = data.display_mode === 'selected' ? [...selectedDates].filter(d => d >= data.start_date && d <= data.end_date) : [];
      const e = detailItem;
      if (!data.title.trim() || data.end_date < data.start_date) {$('#form-error').textContent = '제목과 시작·마지막 날짜를 확인해 주세요.'; return;}
      return mutate(() => api(e ? `/api/tasks/${encodeURIComponent(e.id)}` : '/api/tasks', {method: e ? 'PUT' : 'POST', body: {...data, ...(e ? {version: e.version} : {})}}), '할 일을 저장했어요.');
    }
    if (form.id === 'filters-form') {
      const channels = Object.fromEntries([...form.querySelectorAll('[name=channel]')].map(el => [el.value, el.checked]));
      const disabled_kinds = [...form.querySelectorAll('[name=kind]')].filter(el => !el.checked).map(el => el.value);
      if (SCHOOL) return mutate(async () => {for (const [id, enabled] of Object.entries(channels)) await api(`/api/channels/${encodeURIComponent(id)}/preference`, {method:'PUT',body:{enabled}}); await api('/api/settings', {method:'PUT',body:{...S.prefs,disabled_kinds}});}, '표시 설정을 저장했어요.');
      return mutate(() => api('/api/beta/display-settings', {method: 'PUT', body: {channels, disabled_kinds}}), '표시 설정을 저장했어요.');
    }
    if (form.id === 'feedback-form') {
      const message = form.elements.message.value.trim(); if (!message) return;
      return mutate(() => api('/api/beta/feedback', {method: 'POST', body: {message}}), '의견을 이 베타 서버에 저장했어요.');
    }
  });
  async function boot() {
    if (location.protocol === 'file:') {root.innerHTML = '<section class="login-wrap"><div class="login-card"><h1>실행 파일로 시작해 주세요</h1><p>압축을 푼 뒤 start_beta.cmd를 실행하면 브라우저가 열려요. HTML 파일만 열면 데이터를 저장할 서버가 실행되지 않아요.</p></div></section>'; return;}
    try {
      await api(SCHOOL ? '/api/service' : '/api/beta/info');
      try {S.me = await api('/api/me');} catch (error) {if (error.status === 401) {loginScreen(); return;} throw error;}
      await load();
    } catch (error) {
      clearPrivate(); root.innerHTML = `<section class="login-wrap"><div class="login-card"><h1>베타를 연결하지 못했어요</h1><p>${esc(error.message)}</p><p class="muted">실행 창을 확인한 뒤 브라우저를 새로고침해 주세요.</p></div></section>`;
    }
  }
  function sourceLink(value) {
    if (!SCHOOL || !value) return '';
    try {const url = new URL(value); if (url.protocol !== 'https:' || url.hostname !== 'teams.microsoft.com' || !url.pathname.startsWith('/l/message/')) return ''; return `<p><a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">Teams 원문 보기 →</a></p>`;} catch {return '';}
  }
  function resetIdle() {
    if (!SCHOOL) return;
    clearTimeout(idleTimer);
    idleTimer = setTimeout(async () => {if (S.me) {try {await api('/auth/logout',{method:'POST'});} catch {} clearPrivate(); loginScreen();}}, 15 * 60 * 1000);
  }
  if (SCHOOL) {
    for (const type of ['pointerdown','keydown','touchstart']) document.addEventListener(type,resetIdle,{passive:true});
    document.addEventListener('visibilitychange', () => {if (document.hidden) {clearPrivate(); root.innerHTML = '<div class="loading">화면을 다시 열면 계정을 확인해요.</div>';} else {boot(); resetIdle();}});
    resetIdle();
  }
  boot();
})();
