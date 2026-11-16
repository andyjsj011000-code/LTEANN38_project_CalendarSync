'use strict';
(() => {
  const storageKey = 'calendarsync.theme';
  const themes = new Set(['system', 'light', 'dark']);
  const theme = () => document.documentElement.dataset.theme || 'system';
  function setTheme(value, save = true) {
    if (!themes.has(value)) return;
    document.documentElement.dataset.theme = value;
    if (save) {
      try { window.localStorage.setItem(storageKey, value); } catch {}
    }
  }
  let savedTheme;
  try { savedTheme = window.localStorage.getItem(storageKey); } catch {}
  setTheme(themes.has(savedTheme) ? savedTheme : 'system', false);

  const icons = {
    monitor: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
    moon: '<path d="M20.5 14A8.5 8.5 0 0 1 10 3.5 8.5 8.5 0 1 0 20.5 14Z"/>',
    messages: '<path d="M16 4H5a2 2 0 0 0-2 2v11l4-3h9a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2Z"/><path d="M21 9v11l-4-3H9"/>',
    list: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V2h6v2m-6 5h6m-6 4h6m-6 4h4"/>',
    book: '<path d="M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1Zm0 0v15"/>',
    sparkles: '<path d="M12 2l2.5 7.5L22 12l-7.5 2.5L12 22l-2.5-7.5L2 12l7.5-2.5L12 2Zm7 1v4m-2-2h4"/>',
    laptop: '<rect x="5" y="4" width="14" height="12" rx="2"/><path d="M2 20h20"/>',
    bell: '<path d="M18 8a6 6 0 0 0-12 0c0 5-2 6-2 9h16c0-3-2-4-2-9Zm-8 12a2 2 0 0 0 4 0"/>',
    chevron: '<path d="m6 9 6 6 6-6"/>',
    external: '<path d="M8 16 17 7m-7 0h7v7"/>'
  };
  const icon = name => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name]}</svg>`;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  const button = (action, label, extra = '') => `<button type="button" class="secondary small${extra}" data-action="${action}">${label}</button>`;
  const link = (href, label, external = false) => `<a class="profile-link" href="${esc(href)}"${external ? ' target="_blank" rel="noopener noreferrer"' : ''}>${label}${external ? icon('external') : ''}</a>`;
  const status = (label, tone = 'neutral') => ({label, tone});
  const pending = status('확인 중');
  const disabled = status('사용 불가');
  const enabled = status('연결됨', 'on');
  const approval = status('승인 필요', 'attention');
  const retry = status('다시 연결 필요', 'attention');
  const unavailable = status('확인 실패', 'attention');
  const on = status('켜짐', 'on');
  const off = status('꺼짐');

  function render(state, openRows) {
    const {account, service, connection, teamsUnverified, teamsIngestion, assignments, classroom, classroomError, push, browserPermission} = state;
    const microsoftApps = link('https://myapplications.microsoft.com/', 'Microsoft 앱 권한 관리', true);
    const deviceEnabled = connection?.background_enabled ?? push?.background_enabled;
    const background = deviceEnabled === true ? '&background=1' : '';
    const reconnectAssignments = link(`/auth/login?assignments=1${background}`, 'Teams 과제 연결하기');
    const teams = (teamsUnverified || connection?.teams_verified === false) && connection?.state === 'connected'
      ? status('권한 확인 필요', 'attention') : ({
        connected: enabled, checking: pending, not_connected: status('미연결'),
        refresh_busy: pending, reauth_required: retry, source_not_configured: disabled,
        source_unavailable: unavailable, refresh_unavailable: unavailable,
        verification_failed: status('권한 확인 필요', 'attention'),
        account_not_allowed: status('학교 계정 확인 필요', 'attention')
      })[connection?.state] ?? pending;
    const assignment = !service.assignments_enabled ? disabled : ({
      connected: enabled, permission_required: approval, reauth_required: retry, unavailable
    })[assignments.state] ?? pending;
    const classroomStatus = !service.classroom_enabled ? disabled : classroom?.reauth ? retry
      : classroomError ? unavailable : !classroom ? pending : classroom.connected ? enabled : status('미연결');
    const analysis = typeof classroom?.announcement_analysis === 'boolean'
      ? (classroom.announcement_analysis ? on : off)
      : !service.classroom_enabled || !service.classroom_google_enabled ? disabled
      : classroomError ? unavailable : !classroom ? pending : classroom.reauth || !classroom.connected ? status('연결 필요') : disabled;
    const pushStatus = push === false ? unavailable : !push ? pending : !push.configured ? disabled
      : browserPermission === 'denied' ? status('브라우저에서 차단', 'attention')
      : push.registered && browserPermission === 'granted' ? on : push.registered ? status('브라우저 권한 확인', 'attention') : off;
    const connectClassroom = button(service.classroom_google_enabled ? 'classroom-google-connect' : 'classroom-connect', classroom?.reauth ? '다시 연결' : '클래스룸 연결');

    function row(id, iconName, name, description, current, detail, action = '') {
      const collection = id === 'teams' && teamsIngestion;
      const subtitle = collection ? `<span class="profile-copy">수집 상태 · <span id="profile-teams-ingestion" role="status">${esc(collection)}</span></span>` : `<span class="profile-copy">${description}</span>`;
      return `<details class="profile-permission" data-profile-section="${id}"${openRows.has(id) ? ' open' : ''}>
        <summary id="profile-summary-${id}"><span class="profile-icon">${icon(iconName)}</span><span><strong>${name}</strong>${subtitle}</span><span class="profile-status ${current.tone}">${collection ? '읽기 권한 · ' : ''}${current.label}</span><span class="profile-chevron">${icon('chevron')}</span></summary>
        <div class="profile-detail"><p>${detail}</p>${action ? `<div class="profile-actions">${action}</div>` : ''}</div>
      </details>`;
    }

    const themeOptions = [['system','monitor','시스템'], ['light','sun','라이트'], ['dark','moon','다크']];
    return `<div class="profile-page"><div class="intro profile-intro"><h1>마이페이지</h1>${button('profile-refresh','상태 새로고침')}</div>
      <div class="profile-grid"><div class="profile-stack">
        <section class="card profile-card" aria-labelledby="profile-account-title">
          <h2 id="profile-account-title">내 계정</h2><div class="profile-person"><span class="profile-avatar" aria-hidden="true">${esc(Array.from(account.name || '나')[0])}</span><div><strong>${esc(account.name)}</strong><p class="profile-copy">${esc(account.email || '학교 Microsoft 계정')}</p></div></div>
          ${link('https://myaccount.microsoft.com/', 'Microsoft 계정 관리', true)}<p class="profile-copy">학교 계정 비밀번호는 Microsoft에서 변경해요.</p>
        </section>
        <section class="card profile-card" aria-labelledby="profile-theme-title">
          <h2 id="profile-theme-title">화면 테마</h2><fieldset class="profile-theme" aria-labelledby="profile-theme-title">${themeOptions.map(([value, symbol, label]) => `<label>${icon(symbol)}<input name="profile-theme" type="radio" value="${value}"${theme() === value ? ' checked' : ''}><span>${label}</span></label>`).join('')}</fieldset>
        </section>
        <section class="card profile-card profile-signout">${button('profile-logout-all','모든 기기에서 로그아웃')}<a href="/privacy">개인정보 안내</a></section>
      </div>
      <section class="card profile-permissions" aria-labelledby="profile-connection-title"><h2 id="profile-connection-title">연결 및 권한</h2>
        ${row('teams','messages','Teams 공지 읽기','볼 수 있는 채널의 공지와 일정',teams,'학교 Teams에서 접근 권한이 있는 채널 공지를 읽어 일정으로 표시해요.',teams === retry ? link('/auth/login','Microsoft로 다시 로그인') : microsoftApps)}
        ${row('assignments','list','Teams 과제 읽기','내 과제, 마감일, 제출 상태',assignment,'과제 읽기는 Microsoft에서 승인해요. Microsoft에서 앱 권한을 철회하면 다른 Teams 기능에도 영향을 줄 수 있어요.',assignment === approval || assignment === retry ? reconnectAssignments : assignment === enabled ? microsoftApps : '')}
        ${row('classroom','book','Google 클래스룸 읽기','수업, 공지, 내 과제와 마감일',classroomStatus,'연결을 해제하면 클래스룸 항목 가져오기를 중단하고 가져온 데이터를 지워요.',service.classroom_enabled && classroom && !classroomError ? (classroom.connected && !classroom.reauth ? button('classroom-disconnect','연결 해제') : connectClassroom) : '')}
        ${row('analysis','sparkles','클래스룸 공지 AI 분석','공지 본문에서 일정 추출',analysis,'켜면 클래스룸 공지 본문과 작성일을 OpenAI에 보내 일정을 추출해요.',typeof classroom?.announcement_analysis === 'boolean' ? button('classroom-analysis',classroom.announcement_analysis ? 'AI 분석 끄기' : '동의 설정') : '')}
        ${row('device','laptop','이 기기 연동 유지','화면을 닫은 뒤에도 학교 일정 갱신',deviceEnabled === true ? on : deviceEnabled === false ? off : pending,'이 기기에서 로그아웃하면 CalendarSync의 기기 연동도 종료돼요.',deviceEnabled === true ? button('logout','이 기기에서 로그아웃') : deviceEnabled === false ? link('/auth/login?background=1','연동 유지로 다시 로그인') : '')}
        ${row('push','bell','브라우저 알림','이 브라우저에서 일정 알림 받기',pushStatus,'브라우저 알림 허용과 이 기기의 CalendarSync 구독 상태를 함께 확인해요.',push?.configured && state.hasPreferences ? button('notifications','알림 설정') : '')}
      </section></div></div>`;
  }

  window.BetaProfile = Object.freeze({render, setTheme});
})();
