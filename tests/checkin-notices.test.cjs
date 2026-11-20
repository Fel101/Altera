const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {create, KEY} = require('../static/js/checkin-notices.js');

const MIRA = {id: 'pi_1', avatar_id: 'mira-1a', session_id: 4, kind: 'checkin', name: 'Mira', created_at: 1000.5,
    text: 'Thinking of you. How did the interview go?'};

function fixture({saved = null, permission = 'granted', grant = 'granted', hidden = true, focused = false, discreet = false,
                  answers = [], registration = null, supported = true} = {}) {
    let stored = saved === null ? null : JSON.stringify(saved);
    const nodes = {}, listeners = {}, timers = [], calls = [], notes = [], opened = [], swListeners = {};
    for (const id of ['checkinNoticeToggle', 'checkinNoticeText', 'checkinNoticeStatus']) {
        nodes[id] = {id, checked: false, disabled: false, textContent: '',
            addEventListener(name, fn) { listeners[`${id}:${name}`] = fn; }};
    }
    class FakeNotification {
        constructor(title, options) { this.title = title; this.options = options; notes.push(this); }
        close() { this.closed = true; }
        static async requestPermission() { FakeNotification.permission = grant; return grant; }
    }
    FakeNotification.permission = permission;
    const document = {hidden, hasFocus: () => focused, getElementById: id => nodes[id],
        documentElement: {classList: {contains: name => name === 'discreet' && discreet}}};
    const request = async url => {
        calls.push(url);
        const next = answers.shift() || {status: 200, body: {now: 2000, notices: []}};
        return {ok: next.status < 400, status: next.status, json: async () => next.body};
    };
    const serviceWorker = {getRegistration: async () => registration,
        addEventListener: (name, fn) => { swListeners[name] = fn; }, removeEventListener() {}};
    const controller = create({document, storage: {getItem: () => stored, setItem: (_, value) => { stored = value; }},
        request, Notification: supported ? FakeNotification : undefined, serviceWorker,
        setTimer: fn => { timers.push(fn); return timers.length; }, clearTimer() {},
        openPerson: async data => { opened.push(data); }});
    return {controller, nodes, listeners, timers, calls, notes, opened, swListeners, FakeNotification,
        stored: () => JSON.parse(stored)};
}

test('the Settings panel has the switches, and the chat page and Home load the script', () => {
    const index = fs.readFileSync('templates/index.html', 'utf8'), home = fs.readFileSync('templates/home.html', 'utf8');
    for (const id of ['checkinNoticeToggle', 'checkinNoticeText', 'checkinNoticeStatus']) assert.match(index, new RegExp(`id="${id}"`));
    assert.match(index, /js\/checkin-notices\.js/);
    assert.match(home, /js\/checkin-notices\.js/);
    assert.match(fs.readFileSync('static/sw.js', 'utf8'), /notificationclick/);
});

test('it is off until switched on, and while off it never asks Altera or the browser anything', async () => {
    const f = fixture({permission: 'default'});
    await f.controller.start();
    assert.equal(f.nodes.checkinNoticeToggle.checked, false);
    assert.equal(f.nodes.checkinNoticeText.disabled, true);
    assert.match(f.nodes.checkinNoticeStatus.textContent, /^Off\. For this browser only/);
    assert.deepEqual(f.calls, []);
    assert.equal(f.timers.length, 0);
});

test('switching it on asks the browser, then starts from now so the backlog is not announced', async () => {
    const f = fixture({permission: 'default'});
    await f.controller.start();
    f.nodes.checkinNoticeToggle.checked = true;
    await f.listeners['checkinNoticeToggle:change']();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.FakeNotification.permission, 'granted');
    assert.deepEqual(f.calls, ['/api/presence/notices']);                         // no "after": only Altera's clock
    assert.equal(f.stored().after, 2000);
    assert.equal(f.notes.length, 0);
    assert.equal(f.timers.length, 1);                                              // and it keeps watching
});

test('a refused permission leaves it off and says where to allow it', async () => {
    const f = fixture({permission: 'default', grant: 'denied'});
    await f.controller.start();
    f.nodes.checkinNoticeToggle.checked = true;
    await f.listeners['checkinNoticeToggle:change']();
    assert.equal(f.nodes.checkinNoticeToggle.checked, false);
    assert.match(f.nodes.checkinNoticeStatus.textContent, /blocked .* site settings/);
    assert.deepEqual(f.calls, []);
});

test('a new check-in while Altera is in the background says who, not what', async () => {
    const f = fixture({saved: {enabled: true, text: false, after: 900},
        answers: [{status: 200, body: {now: 2000, notices: [MIRA]}}]});
    await f.controller.start();
    assert.deepEqual(f.calls, ['/api/presence/notices?after=900']);               // the text is not even asked for
    assert.equal(f.notes.length, 1);
    assert.equal(f.notes[0].title, 'Altera');
    assert.equal(f.notes[0].options.body, 'Mira has a check-in for you.');
    assert.equal(f.notes[0].options.tag, 'altera-checkin-pi_1');                   // one per check-in, never twice
    f.notes[0].onclick();
    assert.deepEqual(f.opened, [{avatar_id: 'mira-1a', session_id: 4}]);
    assert.equal(f.stored().after, 2000);
});

test('the text is shown only when chosen, and a long one is cut short', async () => {
    const long = {...MIRA, text: 'x'.repeat(400)};
    const f = fixture({saved: {enabled: true, text: true, after: 900},
        answers: [{status: 200, body: {now: 2000, notices: [long]}}]});
    await f.controller.start();
    assert.deepEqual(f.calls, ['/api/presence/notices?after=900&text=1']);
    assert.equal(f.notes[0].title, 'Mira');
    assert.equal(f.notes[0].options.body.length, 180);
    assert.ok(f.notes[0].options.body.endsWith('…'));
});

test('nothing is shown in discreet mode or while Altera is in front, but the check-in still counts as seen', async () => {
    for (const setup of [{discreet: true}, {hidden: false, focused: true}]) {
        const f = fixture({...setup, saved: {enabled: true, text: false, after: 900},
            answers: [{status: 200, body: {now: 2000, notices: [MIRA]}}]});
        await f.controller.start();
        assert.equal(f.notes.length, 0);
        assert.equal(f.stored().after, 2000);                                      // not announced later instead
    }
});

test('the installed app shows it through its service worker', async () => {
    const shown = [];
    const registration = {showNotification: async (title, options) => { shown.push({title, options}); }};
    const f = fixture({registration, saved: {enabled: true, text: false, after: 900},
        answers: [{status: 200, body: {now: 2000, notices: [MIRA]}}]});
    await f.controller.start();
    assert.equal(f.notes.length, 0);
    assert.deepEqual(shown[0].options.data, {avatar_id: 'mira-1a', session_id: 4});
    await f.swListeners.message({data: {type: 'altera-checkin', avatar_id: 'mira-1a', session_id: 4}});
    assert.deepEqual(f.opened, [{type: 'altera-checkin', avatar_id: 'mira-1a', session_id: 4}]);
});

test('the kids view or a locked browser switches it off instead of asking again and again', async () => {
    for (const status of [403, 423]) {
        const f = fixture({saved: {enabled: true, text: false, after: 900}, answers: [{status, body: {error: 'no'}}]});
        await f.controller.start();
        assert.equal(f.stored().enabled, false);
        assert.equal(f.nodes.checkinNoticeToggle.checked, false);
        assert.equal(f.timers.length, 0);
    }
});

test('permission taken away in the browser turns it off on the next start', async () => {
    const f = fixture({permission: 'denied', saved: {enabled: true, text: false, after: 900}});
    await f.controller.start();
    assert.equal(f.nodes.checkinNoticeToggle.checked, false);
    assert.deepEqual(f.calls, []);
});

test('a browser without notifications says so', async () => {
    const f = fixture({supported: false});
    await f.controller.start();
    f.nodes.checkinNoticeToggle.checked = true;
    await f.listeners['checkinNoticeToggle:change']();
    assert.equal(f.nodes.checkinNoticeStatus.textContent, 'This browser cannot show notifications.');
    assert.equal(f.nodes.checkinNoticeToggle.checked, false);
    assert.equal(KEY, 'altera_checkin_notices');
});
