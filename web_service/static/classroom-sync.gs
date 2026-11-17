// Set CALENDARSYNC_ORIGIN and CALENDARSYNC_TOKEN in Project Settings > Script properties.
function startClassroomSync() {
  syncClassroom();
  const exists = ScriptApp.getProjectTriggers()
    .some(trigger => trigger.getHandlerFunction() === 'syncClassroom');
  if (!exists) ScriptApp.newTrigger('syncClassroom').timeBased().everyMinutes(15).create();
}

function syncClassroom() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    const settings = PropertiesService.getScriptProperties();
    const origin = (settings.getProperty('CALENDARSYNC_ORIGIN') || '').replace(/\/$/, '');
    const token = settings.getProperty('CALENDARSYNC_TOKEN') || '';
    if (!/^https:\/\/[a-z0-9.-]+(?::\d+)?$/i.test(origin) || !/^[\w-]{43}$/.test(token)) {
      throw new Error('Set the CalendarSync HTTPS address and connection key in Script properties.');
    }
    const capturedAt = Date.now();
    const announcements = [];
    const assignments = [];
    let pageToken;
    do {
      const result = Classroom.Courses.list({
        studentId: 'me', courseStates: ['ACTIVE'], pageSize: 100, pageToken,
        fields: 'courses(id,name),nextPageToken'
      });
      for (const course of result.courses || []) {
        collectClassroomItems(course, announcements, assignments);
      }
      pageToken = result.nextPageToken;
    } while (pageToken);

    const mostRecent = items => items.sort((left, right) => right.updated.localeCompare(left.updated))
      .slice(0, 12).map(({updated, ...item}) => item);
    const recentAssignments = mostRecent(assignments);
    collectClassroomSubmissionStates(recentAssignments);
    const payload = JSON.stringify({
      captured_at: capturedAt,
      items: [...mostRecent(announcements), ...recentAssignments]
    });
    const result = UrlFetchApp.fetch(origin + '/api/classroom/import', {
      method: 'post', contentType: 'application/json', payload,
      headers: {Authorization: 'Bearer ' + token}, followRedirects: false,
      muteHttpExceptions: true
    });
    const status = result.getResponseCode();
    if (status === 401) {
      for (const trigger of ScriptApp.getProjectTriggers()) {
        if (trigger.getHandlerFunction() === 'syncClassroom') ScriptApp.deleteTrigger(trigger);
      }
    }
    if (status !== 204) throw new Error('CalendarSync sync failed (HTTP ' + status + ').');
    console.log('CalendarSync sync completed.');
  } finally {
    lock.releaseLock();
  }
}

function collectClassroomItems(course, announcements, assignments) {
  const options = {pageSize: 12, orderBy: 'updateTime desc'};
  const courseName = shortText(course.name, 120) || 'Classroom';
  const notices = [];
  let pageToken;
  do {
    const result = Classroom.Courses.Announcements.list(course.id, {
      ...options, pageToken, fields: 'announcements(text,alternateLink,updateTime),nextPageToken'
    });
    notices.push(...(result.announcements || []));
    pageToken = result.nextPageToken;
  } while (pageToken && notices.length < options.pageSize);
  for (const notice of notices) {
    announcements.push({
      kind: 'announcement', course: courseName, course_id: String(course.id),
      title: shortText(notice.text, 240) || '수업 공지', url: classroomLink(notice.alternateLink),
      updated: notice.updateTime || ''
    });
  }
  const work = [];
  pageToken = undefined;
  do {
    const result = Classroom.Courses.CourseWork.list(course.id, {
      ...options, pageToken,
      fields: 'courseWork(id,title,description,alternateLink,updateTime,dueDate,dueTime,workType),nextPageToken'
    });
    work.push(...(result.courseWork || []).filter(item => item.workType === 'ASSIGNMENT'));
    pageToken = result.nextPageToken;
  } while (pageToken && work.length < options.pageSize);
  for (const item of work) {
    assignments.push({
      kind: 'assignment', course: courseName, course_id: String(course.id), title: shortText(item.title, 240) || '과제',
      assignment_id: item.id,
      description: shortText(item.description, 160), url: classroomLink(item.alternateLink),
      updated: item.updateTime || '', due: classroomDue(item.dueDate, item.dueTime)
    });
  }
}

function collectClassroomSubmissionStates(assignments) {
  const states = {NEW:'working', CREATED:'working', TURNED_IN:'submitted', RETURNED:'returned', RECLAIMED_BY_STUDENT:'reclaimed'};
  const byCourse = new Map();
  for (const assignment of assignments) {
    if (!assignment.assignment_id) continue;
    if (!byCourse.has(assignment.course_id)) byCourse.set(assignment.course_id, new Map());
    byCourse.get(assignment.course_id).set(assignment.assignment_id, assignment);
  }
  for (const [courseId, pending] of byCourse) {
    try {
      const seen = new Set();
      let pageToken;
      do {
        const result = Classroom.Courses.CourseWork.StudentSubmissions.list(courseId, '-', {
          userId: 'me', pageSize: 100, pageToken, fields: 'studentSubmissions(courseWorkId,state),nextPageToken'
        });
        for (const submission of result.studentSubmissions || []) {
          const item = pending.get(submission.courseWorkId);
          if (!item) continue;
          item.submission_status = Object.hasOwn(states, submission.state) ? states[submission.state] : null;
          pending.delete(submission.courseWorkId);
        }
        pageToken = result.nextPageToken;
        if (seen.has(pageToken)) break;
        seen.add(pageToken);
      } while (pageToken && pending.size);
    } catch (_) {
      console.log('Classroom submission status is unavailable for one course.');
    }
  }
}

function shortText(value, length) {
  return Array.from(String(value || '').replace(/[\u0000-\u001f\u007f]+/g, ' ').trim())
    .slice(0, length).join('');
}

function classroomLink(value) {
  return value && value.startsWith('https://classroom.google.com/c/') && value.length <= 320
    ? value : '';
}

function classroomDue(date, time) {
  if (!date) return null;
  const pad = value => String(value || 0).padStart(2, '0');
  const day = date.year + '-' + pad(date.month) + '-' + pad(date.day);
  return time ? day + 'T' + pad(time.hours) + ':' + pad(time.minutes) + ':' +
    pad(time.seconds) + 'Z' : day;
}
