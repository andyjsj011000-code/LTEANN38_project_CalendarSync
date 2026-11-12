/* Filters only the authorized, already loaded items supplied by the caller. */
(() => {
  'use strict';
  const createState = () => ({query:'', unfinishedOnly:false});
  const stateOf = state => ({query:typeof state?.query === 'string' ? state.query : '', unfinishedOnly:state?.unfinishedOnly === true});
  const normalize = value => (typeof value === 'string' ? value : '').normalize('NFKC').toLocaleLowerCase('ko-KR');
  const escape = value => String(value).replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
  const active = state => {const value=stateOf(state); return !!value.query.trim() || value.unfinishedOnly;};

  function filter(items, state) {
    const value = stateOf(state), words = normalize(value.query).trim().split(/\s+/).filter(Boolean);
    return (Array.isArray(items) ? items : []).filter(item => {
      if (!item || item.hidden) return false;
      if (value.unfinishedOnly && (item.completed || item.cancelled)) return false;
      if (!words.length) return true;
      const text = (item.calendar_members || [item])
        .flatMap(member => [member.title,member.notes,member.channel]).map(normalize).join('\n');
      return words.every(word => text.includes(word));
    });
  }

  function emptyText(state, fallback, available) {
    return active(state) && available !== 0 ? '검색·보기 조건에 맞는 일정이 없어요. 조건을 지우면 다시 볼 수 있어요.' : fallback;
  }

  function render(state, {scope='today', matched, total}={}) {
    const value = stateOf(state);
    const range = scope === 'calendar' ? '지금 불러온 달력 화면의 일정만 검색해요.' : '지금 불러온 오늘·다가오는 일정만 검색해요.';
    const count = Number.isInteger(matched) && Number.isInteger(total) && matched >= 0 && total >= matched
      ? `현재 불러온 ${total}개 중 ${matched}개 표시` : '';
    return `<section class="card list-controls" aria-label="일정 검색 및 보기"><div class="dialog-body"><label class="field"><span class="field-label">일정 검색</span><input type="search" data-list-query aria-describedby="list-search-scope" autocomplete="off" placeholder="제목·메모·채널 이름" value="${escape(value.query)}"></label><div class="button-row"><label class="toggle-row"><span>미완료만 보기</span><input type="checkbox" data-list-unfinished ${value.unfinishedOnly ? 'checked' : ''}></label><button type="button" class="secondary small" data-list-clear ${active(value) ? '' : 'disabled'}>조건 지우기</button></div><p class="fine muted" id="list-search-scope">${range} 숨긴 일정은 포함하지 않아요.</p><p class="fine muted" role="status" aria-live="polite" data-list-result-count>${count}</p></div></section>`;
  }

  // State belongs to the signed-in screen, not this controller. Call reset() on
  // account clear and reset the caller's state with createState() at the same time.
  function bind(root, {getState, onChange, enabled=()=>true, onCompositionEnd=()=>{}}) {
    let composing = null;
    const attached = target => !!target && (!root.contains || root.contains(target));
    const marker = (target, name) => attached(target) && target.dataset?.[name] !== undefined;
    const query = target => marker(target,'listQuery');
    const publish = next => {
      const previous = stateOf(getState());
      if (previous.query === next.query && previous.unfinishedOnly === next.unfinishedOnly) return false;
      onChange(next); return true;
    };
    const input = event => {
      if (!enabled() || !query(event.target) || event.isComposing || composing === event.target) return;
      publish({...stateOf(getState()),query:event.target.value});
    };
    const change = event => {
      if (!enabled() || !marker(event.target,'listUnfinished')) return;
      publish({...stateOf(getState()),unfinishedOnly:!!event.target.checked});
    };
    const click = event => {
      const target = event.target?.closest?.('[data-list-clear]');
      if (!enabled() || !marker(target,'listClear') || target.disabled) return;
      event.preventDefault(); composing = null; publish(createState());
    };
    const compositionStart = event => {if (enabled() && query(event.target)) composing = event.target;};
    const compositionEnd = event => {
      if (!enabled() || !query(event.target)) {if (composing === event.target) composing = null; return;}
      composing = null;
      if (!publish({...stateOf(getState()),query:event.target.value})) onCompositionEnd();
    };
    const listeners = {input,change,click,compositionstart:compositionStart,compositionend:compositionEnd};
    for (const [name, listener] of Object.entries(listeners)) root.addEventListener(name,listener);
    return {
      isComposing() {return attached(composing);},
      reset() {composing = null;},
      captureFocus() {
        const input = root.querySelector('[data-list-query]');
        const document = root.ownerDocument || root;
        return input && document.activeElement === input ? {start:input.selectionStart,end:input.selectionEnd,direction:input.selectionDirection} : null;
      },
      restoreFocus(selection) {
        if (!selection || !enabled()) return;
        const input = root.querySelector('[data-list-query]');
        input?.focus?.({preventScroll:true});
        if (selection.start !== null && selection.start !== undefined) input?.setSelectionRange?.(selection.start,selection.end,selection.direction);
      },
      destroy() {composing = null; for (const [name, listener] of Object.entries(listeners)) root.removeEventListener(name,listener);}
    };
  }
  window.BetaListControls = Object.freeze({createState,filter,active,emptyText,render,bind});
})();
