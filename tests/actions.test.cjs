const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {dispatch, resolve} = require('../static/js/actions.js');

function element(attributes = {}, parent = null, extra = {}) {
    return {nodeType: 1, parentElement: parent, ...extra,
        getAttribute: name => (name in attributes ? attributes[name] : null), hasAttribute: name => name in attributes};
}

function event(type, target) {
    return {type, target, cancelBubble: false, stopPropagation() { this.cancelBubble = true; }};
}

test('no page carries an inline event handler, and the pages with actions load actions.js', () => {
    for (const name of fs.readdirSync('templates')) {
        const page = fs.readFileSync(`templates/${name}`, 'utf8');
        assert.doesNotMatch(page, /\son[a-z]+\s*=\s*["']/i, name);
        if (/data-(click|change|submit)=/.test(page)) assert.match(page, /js\/actions\.js/, name);
    }
    for (const name of fs.readdirSync('static/js')) {
        assert.doesNotMatch(fs.readFileSync(`static/js/${name}`, 'utf8'), /\son(click|change|input|submit)=["'\\]/, name);
    }
});

test('a click calls the named function with no arguments unless the element gives some', () => {
    const calls = [];
    const root = {togglePhotoBar: (...args) => calls.push(args), console};
    dispatch(root, event('click', element({'data-click': 'togglePhotoBar'})));
    dispatch(root, event('click', element({'data-click': 'togglePhotoBar', 'data-args': '[false]'})));
    assert.deepEqual(calls, [[], [false]]);
});

test('data-pass adds the element, its checked state, its value or the event after the arguments', () => {
    const calls = [];
    const root = {pick: (...args) => calls.push(args), console};
    const box = element({'data-change': 'pick', 'data-pass': 'checked'}, null, {checked: true});
    const list = element({'data-change': 'pick', 'data-pass': 'value'}, null, {value: 'romantic'});
    const accent = element({'data-click': 'pick', 'data-args': '["#ff6b9d"]', 'data-pass': 'element'});
    const form = element({'data-submit': 'pick', 'data-pass': 'event'});
    dispatch(root, event('change', box));
    dispatch(root, event('change', list));
    dispatch(root, event('click', accent));
    const submitted = event('submit', form);
    dispatch(root, submitted);
    assert.deepEqual(calls, [[true], ['romantic'], ['#ff6b9d', accent], [submitted]]);
});

test('a click inside an element reaches its action, and outer actions run after inner ones like inline handlers did', () => {
    const calls = [];
    const root = {outer: () => calls.push('outer'), inner: () => calls.push('inner'), console};
    const overlay = element({'data-click': 'outer'});
    const button = element({'data-click': 'inner'}, overlay);
    const label = {nodeType: 3, parentElement: button};                    // a text node inside the button
    dispatch(root, event('click', label));
    assert.deepEqual(calls, ['inner', 'outer']);
    const stopped = event('click', button);
    root.inner = () => { calls.push('inner'); stopped.stopPropagation(); };
    dispatch(root, stopped);
    assert.deepEqual(calls, ['inner', 'outer', 'inner']);                  // stopping the event stops the outer one
});

test('a dotted name calls the method on its object, and nothing else is ever evaluated', () => {
    const calls = [];
    const BackupUI = {open() { calls.push(this === BackupUI); }};
    const errors = [];
    const root = {BackupUI, console: {error: message => errors.push(String(message))}};
    dispatch(root, event('click', element({'data-click': 'BackupUI.open'})));
    assert.deepEqual(calls, [true]);
    for (const name of ['alert(1)', 'BackupUI.open()', 'constructor.constructor', '__proto__.x', 'missing']) {
        assert.equal(resolve(root, name), null, name);
        dispatch(root, event('click', element({'data-click': name})));
    }
    assert.equal(calls.length, 1);
    assert.equal(errors.length, 5);
});

test('an action that throws or rejects is logged and does not stop the page', async () => {
    const errors = [];
    const root = {bad: () => { throw new Error('boom'); }, later: async () => { throw new Error('later'); },
        console: {error: error => errors.push(error.message)}};
    dispatch(root, event('click', element({'data-click': 'bad'})));
    dispatch(root, event('click', element({'data-click': 'later'})));
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(errors, ['boom', 'later']);
});
