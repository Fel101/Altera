const test = require('node:test');
const assert = require('node:assert/strict');
const {create, HINTS} = require('../static/js/welcome.js');

function node(tag = 'div') {
    const attributes = {}, classes = new Set();
    return {tag, children: [], dataset: {}, style: {display: 'none'}, textContent: '', value: '', submitted: 0,
        classList: {add: c => classes.add(c), contains: c => classes.has(c)},
        setAttribute(name, value) { attributes[name] = String(value); }, getAttribute: name => attributes[name] ?? null,
        replaceChildren(...children) { this.children = children; }, requestSubmit() { this.submitted++; }};
}

function page({answers = {}, saved = {}, sessionId = 1, hour = 9} = {}) {
    const ids = ['welcomeTitle', 'welcomeText', 'welcomeMessage', 'conversationStarters', 'starterButtons', 'featureHint',
        'hintText', 'messageInput', 'messageForm'];
    const nodes = Object.fromEntries(ids.map(id => [id, node()]));
    const values = new Map(Object.entries(saved)), timers = [], assigned = [], warnings = [];
    const state = {sessionId, busy: false, active: true};
    const welcome = create({
        document: {getElementById: id => nodes[id] || null, createElement: tag => node(tag)},
        storage: {getItem: key => (values.has(key) ? values.get(key) : null), setItem: (key, value) => values.set(key, value)},
        json: async url => {
            const answer = answers[url];
            if (answer instanceof Error) throw answer;
            return {res: {ok: answer !== undefined}, data: answer};
        },
        later: (fn, delay) => timers.push({fn, delay}), active: () => state.active,
        context: () => ({session_id: state.sessionId}),
        insert: text => { if (nodes.messageInput.value) return false; nodes.messageInput.value = text; return true; },
        busy: () => state.busy, assign: url => assigned.push(url), warn: (...args) => warnings.push(args),
        now: () => ({getHours: () => hour})});
    return {welcome, nodes, values, timers, assigned, warnings, state};
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test('a first run that is not finished goes to the setup page and nothing else loads', async () => {
    const f = page({answers: {'/api/onboarding/status': {complete: false}}});
    await f.welcome.check();
    assert.deepEqual(f.assigned, ['/setup']);
    assert.equal(f.timers.length, 0);
    f.welcome.openSetupCheck();
    assert.deepEqual(f.assigned, ['/setup', '/setup?check=1']);
});

test('once set up, the greeting uses the name and the time of day, and up to four starters appear as plain text', async () => {
    const f = page({hour: 23, answers: {'/api/onboarding/status': {complete: true}, '/api/profile': {name: 'Sam'},
        '/api/conversation-starters': {starters: ['<b>one</b>', 'two', 'three', 'four', 'five']}}});
    await f.welcome.check();
    await settle();
    assert.equal(f.nodes.welcomeTitle.textContent, 'Hey, night owl, Sam.');
    assert.equal(f.nodes.welcomeMessage.classList.contains('personalized'), true);
    const buttons = f.nodes.starterButtons.children;
    assert.deepEqual(buttons.map(b => b.textContent), ['<b>one</b>', 'two', 'three', 'four']);   // text, never markup
    assert.equal(buttons[0].getAttribute('data-click'), 'useStarter');
    assert.equal(f.nodes.conversationStarters.style.display, 'block');
});

test('with no name the page keeps its own greeting, and a broken status check still offers starters', async () => {
    const f = page({answers: {'/api/onboarding/status': new Error('offline'), '/api/conversation-starters': {starters: ['hi']}}});
    await f.welcome.check();
    await settle();
    assert.equal(f.nodes.welcomeTitle.textContent, '');
    assert.equal(f.nodes.starterButtons.children.length, 1);
    assert.equal(f.warnings.length, 1);
});

test('a starter goes into an empty composer and is sent only if the chat is unchanged and idle', () => {
    const f = page();
    f.nodes.messageInput.value = 'my own draft';
    f.welcome.useStarter({textContent: 'Tell me a story'});
    assert.equal(f.nodes.messageInput.value, 'my own draft');                               // never replaced
    assert.equal(f.timers.length, 0);
    f.nodes.messageInput.value = '';
    f.welcome.useStarter({textContent: 'Tell me a story'});
    f.state.sessionId = 2;                                                               // the chat changed
    f.timers.shift().fn();
    assert.equal(f.nodes.messageForm.submitted, 0);
    f.state.sessionId = 1;
    f.nodes.messageInput.value = '';
    f.welcome.useStarter({textContent: 'Tell me a story'});
    f.timers.shift().fn();
    assert.equal(f.nodes.messageForm.submitted, 1);
});

test('hints come one at a time, and a dismissed one never returns, the first one included', () => {
    const f = page();
    f.welcome.scheduleHint();
    assert.equal(f.timers[0].delay, 8000);
    f.timers.shift().fn();
    assert.equal(f.nodes.hintText.textContent, HINTS[0]);
    f.welcome.dismissHint();
    assert.equal(f.nodes.featureHint.style.display, 'none');
    assert.deepEqual(JSON.parse(f.values.get('altera_dismissed_hints')), [0]);
    f.welcome.scheduleHint();
    f.timers.shift().fn();
    assert.equal(f.nodes.hintText.textContent, HINTS[1]);
    const unreadable = page({saved: {altera_dismissed_hints: 'not json'}});
    unreadable.welcome.scheduleHint();
    unreadable.timers.shift().fn();
    assert.equal(unreadable.nodes.hintText.textContent, HINTS[0]);
});
