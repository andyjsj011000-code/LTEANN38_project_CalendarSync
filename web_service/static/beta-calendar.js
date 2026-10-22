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
  window.BetaCalendar = Object.freeze({addDays, distance, monthGrid, appears, layoutRanges});
})();
