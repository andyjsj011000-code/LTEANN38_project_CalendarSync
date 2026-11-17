'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const calls = [], handlers = [];
let responseStatus = 204;
const source = path.join(__dirname, '../web_service/static/classroom-sync.gs');
const token = 'a'.repeat(43);
const fixtures = {
  CALENDARSYNC_ORIGIN: 'https://calendar.example.school',
  CALENDARSYNC_TOKEN: token
};
const lock = {tryLock: () => true, releaseLock() {}};
const context = {
  console,
  PropertiesService: {getScriptProperties: () => ({getProperty: name => fixtures[name]})},
  LockService: {getScriptLock: () => lock},
  ScriptApp: {
    getProjectTriggers: () => handlers.map(name => ({getHandlerFunction: () => name})),
    deleteTrigger(trigger) {
      handlers.splice(handlers.indexOf(trigger.getHandlerFunction()), 1);
    },
    newTrigger(name) {
      return {timeBased: () => ({everyMinutes(interval) {
        assert.equal(interval, 15);
        return {create: () => handlers.push(name)};
      }})};
    }
  },
  Classroom: {Courses: {
    list(options) {
      assert.equal(options.studentId, 'me');
      return {courses: [{id:'1',name:'컴퓨터과학'}]};
    },
    Announcements: {list: (course, options) => options.pageToken
      ? {announcements: [{text:'이전 공지',updateTime:'2026-11-10'}]}
      : {announcements: [{text:'수업 공지',updateTime:'2026-11-11'}],nextPageToken:'more'}},
    CourseWork: {list(course, options) {
      assert.equal(course, '1');
      return options.pageToken
        ? {courseWork: [{id:'11',workType:'ASSIGNMENT',title:'실험 보고서',dueDate:{year:2026,month:11,day:20},dueTime:{hours:14,minutes:59}}]}
        : {courseWork: [{workType:'SHORT_ANSWER_QUESTION',title:'질문'}],nextPageToken:'more'};
    }, StudentSubmissions: {list(course, work, options) {
      assert.equal(course, '1'); assert.equal(work, '-'); assert.equal(options.userId, 'me');
      return {studentSubmissions:[{courseWorkId:'11',state:'TURNED_IN'}]};
    }}}
  }},
  UrlFetchApp: {
    fetch(url, options) {
      assert.equal(url, fixtures.CALENDARSYNC_ORIGIN + '/api/classroom/import');
      assert.equal(options.headers.Authorization, 'Bearer ' + token);
      assert.equal(options.followRedirects, false);
      calls.push(JSON.parse(options.payload));
      return {getResponseCode: () => responseStatus};
    }
  }
};

vm.createContext(context);
vm.runInContext(fs.readFileSync(source, 'utf8'), context);
context.startClassroomSync();
context.startClassroomSync();
assert.deepEqual(handlers, ['syncClassroom']);
assert.equal(calls.length, 2);
assert.deepEqual(calls[0].items.map(item => [item.kind, item.title, item.due]), [
  ['announcement', '수업 공지', undefined],
  ['announcement', '이전 공지', undefined],
  ['assignment', '실험 보고서', '2026-11-20T14:59:00Z']
]);
assert.equal(calls[0].items[2].submission_status, 'submitted');
responseStatus = 401;
assert.throws(() => context.syncClassroom(), /HTTP 401/);
assert.deepEqual(handlers, []);
