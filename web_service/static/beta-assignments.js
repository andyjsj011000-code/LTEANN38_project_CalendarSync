'use strict';
(() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {working:'진행 중',submitted:'제출됨',returned:'반환됨',reassigned:'재제출 필요',excused:'면제',inactive:'종료됨',reclaimed:'제출 취소',unknown:'제출 상태 미확인'};
  const status = e => e.source === 'classroom' && e.submission_status === 'working' ? '미제출' : labels[e.submission_status] || labels.unknown;
  const overdue = (e, now) => !e.completed && e.due_at && Date.parse(e.due_at) < now;
  const tabs = [['upcoming','예정'], ['overdue','기한 경과'], ['completed','완료'], ['all','전체']];
  const dueDateFormat = new Intl.DateTimeFormat('ko-KR', {
    month:'long', day:'numeric', weekday:'long', timeZone:'UTC'
  });
  const seoulDateFormat = new Intl.DateTimeFormat('ko-KR', {
    year:'numeric', month:'2-digit', day:'2-digit', timeZone:'Asia/Seoul'
  });
  const checkedAtFormat = new Intl.DateTimeFormat('ko-KR', {
    month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit', timeZone:'Asia/Seoul'
  });
  const classroomStatusUnknown = item => item.source === 'classroom' && item.submission_status === 'unknown';

  function category(item, now) {
    if (classroomStatusUnknown(item)) return 'unknown';
    if (item.completed) return 'completed';
    return overdue(item, now) ? 'overdue' : 'upcoming';
  }
  function filter(items, tab, now = Date.now()) {
    const selected = tab === 'overdue' || tab === 'completed' ? tab : 'upcoming';
    return items.filter(item => tab === 'all' || category(item, now) === selected);
  }
  function row(e) {
    const provider = e.source === 'classroom' ? 'Google Classroom' : 'Teams';
    const time = e.time || (e.end_date ? '종일' : '기한 없음');
    return `<div class="task-row assignment ${e.completed ? 'done' : ''}"><span class="assignment-mark" aria-label="${provider} 과제">과제</span><button class="task-main" data-detail="${esc(e.id)}" data-source="${esc(e.source)}"><span class="task-title">${esc(e.title)}</span><span class="task-meta">${esc(e.channel)} · ${provider} · ${esc(status(e))}</span></button><span class="task-time">${esc(time)}</span></div>`;
  }
  function assignmentListRow(item, now) {
    const provider = item.source === 'classroom' ? 'Google Classroom' : 'Teams';
    const state = category(item, now);
    const submission = state === 'overdue' ? '기한 경과 · ' + status(item) : status(item);
    const dueTime = item.time ? `<time datetime="${esc(item.due_at)}">${esc(item.time)}</time>` :
      `<span>${item.end_date ? '종일' : '기한 없음'}</span>`;
    return `<button type="button" class="assignment-list-row" data-detail="${esc(item.id)}" data-source="${esc(item.source)}">
      <span class="assignment-list-main">
        <span class="assignment-list-title">${esc(item.title)}</span>
        <span class="assignment-list-meta">${esc(item.channel)} <span class="assignment-list-source" data-provider="${esc(item.source)}" aria-label="${provider} 과제">${provider}</span></span>
      </span>
      <span class="assignment-list-due">
        ${dueTime}
        <span class="assignment-list-status" data-state="${state}">${esc(submission)}</span>
      </span>
      <span class="assignment-list-chevron" aria-hidden="true"></span>
    </button>`;
  }
  function notice(snapshot, connect) {
    if (snapshot.state === 'permission_required') return `<div class="beta-banner" role="status"><strong>Teams 과제 연결</strong><span>수업 이름, 과제 제목·설명·마감일, 원문 링크와 내 제출 상태를 읽어 표시해요. <a href="/privacy#teams">Teams 개인정보 안내</a></span><a class="primary" href="${esc(connect)}">Teams 과제 연결하기</a></div>`;
    if (snapshot.state === 'reauth_required') return '<div class="beta-banner" role="alert"><span>과제 연결을 다시 확인해 주세요.</span><a href="/auth/login">다시 로그인</a></div>';
    if (snapshot.state === 'unavailable') return '<div class="beta-banner" role="alert"><span>과제를 불러오지 못했어요. 빈 목록을 수집 완료로 처리하지 않았어요.</span><button class="secondary" data-action="refresh-assignments">다시 확인</button></div>';
    return '';
  }
  function renderGroups(items, now) {
    const ordered = [...items].sort((a,b) =>
      (a.due_at || '9999').localeCompare(b.due_at || '9999') || a.title.localeCompare(b.title)
    );
    const groups = new Map();
    for (const item of ordered) {
      const day = item.end_date || '';
      if (!groups.has(day)) groups.set(day, []);
      groups.get(day).push(item);
    }
    const dateParts = Object.fromEntries(seoulDateFormat.formatToParts(now).map(({type, value}) => [type, value]));
    const today = `${dateParts.year}-${dateParts.month}-${dateParts.day}`;
    return [...groups].map(([day, rows]) => {
      const dateLabel = day ?
        `<time datetime="${esc(day)}">${esc(dueDateFormat.format(new Date(day + 'T00:00:00Z')))}</time>` : '기한 없음';
      return `<section class="assignment-group">
        <h2 class="assignment-group-heading">${dateLabel}${day === today ? '<small class="assignment-group-today">오늘</small>' : ''}</h2>
        ${rows.map(item => assignmentListRow(item, now)).join('')}
      </section>`;
    }).join('');
  }
  function render(snapshot, tab, connect, now = Date.now(), classroom = '') {
    const items = snapshot.items || [];
    const loading = !!connect && (snapshot.loading || snapshot.state === 'idle');
    const providers = [connect && 'Teams', classroom && 'Google Classroom'].filter(Boolean).join('와 ');
    const refreshLabel = classroom ? 'Teams 과제 새로고침' : '과제 새로고침';
    const checked = snapshot.checked_at ? 'Teams 최근 확인 ' + checkedAtFormat.format(snapshot.checked_at * 1000) : '';
    const unknown = items.some(classroomStatusUnknown) ? '제출 상태를 확인할 수 없는 Classroom 과제는 전체 탭에서 볼 수 있어요.' : '';
    const note = [loading ? 'Teams 과제를 확인하고 있어요.' : checked, unknown].filter(Boolean).join(' · ');
    const empty = loading ? '확인된 과제부터 표시할게요.' : '이 분류에 해당하는 과제가 없어요.';
    return `<section class="assignment-page" aria-labelledby="assignments-title">
      <div class="intro assignment-intro">
        <div><h1 id="assignments-title">과제</h1><p class="muted">제출 상태는 ${providers}에서 가져와요.</p></div>
        ${connect ? `<button type="button" class="secondary" data-action="refresh-assignments" ${loading ? 'disabled aria-busy="true"' : ''}>${loading ? '불러오는 중…' : refreshLabel}</button>` : ''}
      </div>
      ${classroom}
      ${connect ? notice(snapshot, connect) : ''}
      <div class="assignment-tabs" role="group" aria-label="과제 분류">
        ${tabs.map(([key, title]) => `<button type="button" data-assignment-filter="${key}" aria-pressed="${tab === key}">${title} <span class="assignment-tab-count">${filter(items, key, now).length}</span></button>`).join('')}
      </div>
      ${note ? `<p class="fine muted assignment-updated" role="status">${esc(note)}</p>` : ''}
      ${renderGroups(filter(items, tab, now), now) || `<p class="empty" role="status">${empty}</p>`}
    </section>`;
  }
  window.BetaAssignments = Object.freeze({filter,overdue,status,row,render});
})();

