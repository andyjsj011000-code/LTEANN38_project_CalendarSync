'use strict';
(() => {
  const addDays = (key, n) => new Date(Date.parse(key + 'T00:00:00Z') + n * 86400000).toISOString().slice(0, 10);
  const distance = (a, b) => Math.round((Date.parse(b + 'T00:00:00Z') - Date.parse(a + 'T00:00:00Z')) / 86400000);
  function monthGrid(month) {
    const first = month + '-01';
    const start = addDays(first, -new Date(first + 'T00:00:00Z').getUTCDay());
    return {start, end: addDays(start, 41), days: Array.from({length: 42}, (_, i) => addDays(start, i))};
  }
  function appears(item, day) {
    if (day < item.start_date || day > item.end_date) return false;
    if (item.source === 'teams' || day === item.end_date || item.display_mode === 'daily') return true;
    return item.display_mode === 'selected' && item.selected_dates.includes(day);
  }
  const classroom = item => item.source === 'classroom' || item.source === 'classroom-event';
  const normalize = value => (value || '').normalize('NFKC').toLowerCase();
  const compact = value => value.replace(/[^\p{L}\p{N}]/gu, '');
  function sameSchedule(a, b, name) {
    if (!!a.cancelled !== !!b.cancelled || (a.time && b.time && a.time !== b.time)) return false;
    const sameClassroom = classroom(a) && classroom(b);
    const channel = compact(normalize(a.channel));
    const sameCourse = sameClassroom && a.course_id && b.course_id
      ? a.course_id === b.course_id : channel && channel === compact(normalize(b.channel));
    if ((sameClassroom || name.length < 6) && !sameCourse) return false;
    if (sameClassroom) {
      // Different assignments can have the same title and deadline in one course.
      if (a.source === 'classroom' && b.source === 'classroom' && a.id !== b.id) return false;
    }
    return a.kind === b.kind || [a.kind,b.kind].every(kind => kind === 'assignment' || kind === 'event');
  }
  function mergeClassroomSchedules(items) {
    if (!items.some(classroom)) return items;
    const buckets = new Map();
    const groups = [];
    const rows = items.map((item, index) => ({item, index}));
    for (const row of [...rows.filter(row => classroom(row.item)), ...rows.filter(row => !classroom(row.item))]) {
      const {item} = row;
      const title = normalize(item.title).replace(/(?:[\s:·()\-]*(?:재공지|재안내|공지|안내|reminder))+(?:[\s.!)]*)$/u, '');
      const name = compact(title);
      const key = JSON.stringify([item.start_date, item.end_date, name, title.match(/\p{N}+/gu)]);
      const eligible = name && item.start_date && item.end_date &&
        ['classroom','classroom-event','teams','assignment'].includes(item.source);
      const candidates = eligible ? buckets.get(key) || [] : [];
      const match = candidates.find(group => group.some(row => classroom(row.item)) &&
        group.every(other => sameSchedule(item, other.item, name)));
      if (match) match.push(row);
      else {
        const group = [row];
        groups.push(group);
        if (eligible) buckets.set(key, [...candidates, group]);
      }
    }
    const priority = {teams:0,classroom:1,assignment:2,'classroom-event':3};
    return groups.map(group => {
      group.sort((a,b) => a.index - b.index);
      if (group.length === 1) return group[0];
      const members = group.map(row => row.item);
      const primary = members.reduce((a,b) => priority[b.source] < priority[a.source] ? b : a);
      const teams = members.filter(item => item.source === 'teams').flatMap(item =>
        item.related_sources || [{id:item.id,url:item.source_url,channel:item.channel,channel_id:item.channel_id}]);
      return {index:group[0].index,item:{
        ...primary, time:primary.time || members.find(item => item.time)?.time,
        calendar_members:members, ...(teams.length > 1 ? {related_sources:teams} : {}),
      }};
    }).sort((a,b) => a.index - b.index).map(row => row.item);
  }
  function layoutRanges(items, month) {
    const grid = monthGrid(month);
    const ranges = items.filter(e => e.start_date !== e.end_date).sort((a, b) => a.start_date.localeCompare(b.start_date) || b.end_date.localeCompare(a.end_date) || a.id.localeCompare(b.id));
    let previous = new Map();
    return Array.from({length: 6}, (_, w) => {
      const start = addDays(grid.start, w * 7), end = addDays(start, 6), occupied = [], next = new Map();
      const candidates = ranges.filter(e => e.start_date <= end && e.end_date >= start).sort((a, b) => Number(b.start_date < start) - Number(a.start_date < start) || a.start_date.localeCompare(b.start_date) || b.end_date.localeCompare(a.end_date) || a.id.localeCompare(b.id));
      const segments = candidates.map(item => {
        const from = item.start_date < start ? start : item.start_date;
        const to = item.end_date > end ? end : item.end_date;
        const first = distance(start, from), last = distance(start, to);
        const mask = ((1 << (last - first + 1)) - 1) << first;
        let lane = previous.get(item.id);
        if (lane === undefined || ((occupied[lane] || 0) & mask)) {
          lane = 0;
          while ((occupied[lane] || 0) & mask) lane++;
        }
        occupied[lane] = (occupied[lane] || 0) | mask;
        if (item.end_date > end) next.set(item.id, lane);
        return {item, from, to, first, last, lane, before: item.start_date < start, after: item.end_date > end, endsHere: to === item.end_date};
      });
      previous = next;
      return {start, end, lanes: occupied.length, segments, days: Array.from({length: 7}, (_, i) => addDays(start, i))};
    });
  }
  window.BetaCalendar = Object.freeze({addDays, distance, monthGrid, appears, layoutRanges, mergeClassroomSchedules});
})();
