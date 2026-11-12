'use strict';
(() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {working:'진행 중',submitted:'제출됨',returned:'반환됨',reassigned:'재제출 필요',excused:'면제',inactive:'종료됨',unknown:'제출 상태 미확인'};
  const status = e => labels[e.submission_status] || labels.unknown;
  const overdue = (e, now) => !e.completed && e.due_at && Date.parse(e.due_at) < now;
  function filter(items, tab, now = Date.now()) {
    return items.filter(e => tab === 'all' || (tab === 'completed' ? e.completed :
      tab === 'overdue' ? overdue(e,now) : !e.completed && !overdue(e,now)));
  }
  function row(e) {
    return `<div class="task-row assignment ${e.completed ? 'done' : ''}"><span class="assignment-mark" aria-label="Teams 과제">과제</span><button class="task-main" data-detail="${esc(e.id)}" data-source="assignment"><span class="task-title">${esc(e.title)}</span><span class="task-meta">${esc(e.channel)} · ${esc(status(e))}</span></button><span class="task-time">${esc(e.time || '기한 없음')}</span></div>`;
  }
  function notice(snapshot, connect) {
    if (snapshot.state === 'permission_required') return `<div class="beta-banner" role="status"><strong>Teams 과제 연결</strong><span>처음 한 번 과제 읽기 권한을 연결해 주세요. 학교 관리자 승인이 필요하다고 나오면 관리자에게 권한 승인을 요청해 주세요.</span><a class="primary" href="${esc(connect)}">Teams 과제 연결하기</a></div>`;
    if (snapshot.state === 'reauth_required') return '<div class="beta-banner" role="alert"><span>과제 연결을 다시 확인해 주세요.</span><a href="/auth/login">다시 로그인</a></div>';
    if (snapshot.state === 'unavailable') return '<div class="beta-banner" role="alert"><span>과제를 불러오지 못했어요. 빈 목록을 수집 완료로 처리하지 않았어요.</span><button class="secondary" data-action="refresh-assignments">다시 확인</button></div>';
    return '';
  }
  function render(snapshot, tab, connect, now = Date.now(), classroom = '') {
    const items = snapshot.items || [], counts = Object.fromEntries(['upcoming','overdue','completed','all'].map(t => [t,filter(items,t,now).length]));
    const tabs = [['upcoming','예정'],['overdue','기한 경과'],['completed','완료'],['all','전체']];
    const selected = filter(items,tab,now).sort((a,b) => (a.due_at || '9999').localeCompare(b.due_at || '9999') || a.title.localeCompare(b.title));
    const groups = new Map();
    for (const item of selected) {const day = item.end_date || ''; if (!groups.has(day)) groups.set(day,[]); groups.get(day).push(item);}
    const dateLabel = d => d ? new Intl.DateTimeFormat('ko-KR',{month:'long',day:'numeric',weekday:'short',timeZone:'UTC'}).format(new Date(d+'T00:00:00Z')) : '기한 없음';
    const loading = snapshot.loading || snapshot.state === 'idle';
    const description = classroom ? 'Google Classroom과 Teams에서 가져온 과제를 확인해요.' : '내게 배정된 Teams 과제를 모아 봐요. 제출 상태는 Teams에서 확인해요.';
    const refreshLabel = classroom ? 'Teams 과제 새로고침' : '과제 새로고침';
    const intro = `<div class="intro"><div><h1>과제</h1><p class="muted">${description}</p></div><button class="secondary" data-action="refresh-assignments" ${loading ? 'disabled aria-busy="true"' : ''}>${loading ? '불러오는 중…' : refreshLabel}</button></div>`;
    const teamsHeading = classroom ? '<h2>Teams 과제</h2>' : '';
    return `${intro}${classroom}${teamsHeading}${notice(snapshot,connect)}<div class="assignment-tabs" role="group" aria-label="Teams 과제 분류">${tabs.map(([key,title]) => `<button class="secondary ${tab === key ? 'active' : ''}" data-assignment-filter="${key}" aria-pressed="${tab === key}">${title} <span class="count">${counts[key]}</span></button>`).join('')}</div><p class="fine muted" role="status">${loading ? '과제를 확인하고 있어요.' : snapshot.checked_at ? '최근 확인: '+new Intl.DateTimeFormat('ko-KR',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Seoul'}).format(new Date(snapshot.checked_at*1000)) : '아직 과제를 가져오지 않았어요.'} · 지난 60일의 마감과 앞으로의 과제를 표시해요.</p>${[...groups].map(([day,rows]) => `<section class="card assignment-group"><div class="card-head"><h2>${esc(dateLabel(day))}</h2><span class="muted fine">${rows.length}개</span></div><div class="list">${rows.map(row).join('')}</div></section>`).join('') || `<section class="card"><div class="empty" role="status">${loading ? '확인된 과제부터 표시할게요.' : snapshot.state === 'connected' ? '이 분류에 해당하는 과제가 없어요.' : '과제 연결 상태를 확인해 주세요.'}</div></section>`}`;
  }
  window.BetaAssignments = Object.freeze({filter,overdue,status,row,render});
})();

