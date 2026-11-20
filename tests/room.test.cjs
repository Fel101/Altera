const test = require('node:test');
const assert = require('node:assert/strict');
const {create} = require('../static/js/room.js');

function fixture(saved = null) {
    let value = saved, date = new Date(2026, 0, 1, 10), next = 0, writes = 0;
    const timers = new Map(), listeners = new Map(), handlers = new Map();
    const select = {value: '', addEventListener: (n, fn) => handlers.set(n, fn), removeEventListener: n => handlers.delete(n)};
    const status = {textContent: ''};
    const document = {hidden: false, documentElement: {dataset: {}},
        getElementById: id => id === 'roomMode' ? select : status,
        addEventListener: (n, fn) => listeners.set(n, fn), removeEventListener: n => listeners.delete(n)};
    const storage = {getItem: () => value, setItem: (_, nextValue) => {writes++; value = nextValue;}};
    const room = create({document, storage, now: () => date, setTimer: (fn, ms) => {const id = ++next; timers.set(id, {fn, ms}); return id;},
        clearTimer: id => timers.delete(id), flush: async () => {}});
    return {room, document, select, status, timers, listeners, handlers, storage,
        date: d => {date = d;}, saved: () => value, writes: () => writes};
}

test('default and corrupt settings are off without writes or timers', () => {
    for (const initial of [null, 'unexpected']) {
        const f = fixture(initial); f.room.start();
        assert.equal(f.select.value, 'off'); assert.deepEqual(f.document.documentElement.dataset, {});
        assert.equal(f.writes(), 0); assert.equal(f.timers.size, 0);
    }
});

test('saved static room remains still and can be switched off', async () => {
    const f = fixture('day'); f.room.start();
    assert.deepEqual(f.document.documentElement.dataset, {room: 'on', roomLight: 'day', roomSeason: 'neutral'});
    assert.equal(f.timers.size, 0);
    f.select.value = 'off'; await f.handlers.get('change')();
    assert.equal(f.saved(), 'off'); assert.deepEqual(f.document.documentElement.dataset, {});
});

test('automatic room follows local time and northern seasons without generating media', () => {
    const f = fixture('auto'); f.room.start();
    assert.deepEqual(f.document.documentElement.dataset, {room: 'on', roomLight: 'day', roomSeason: 'winter'});
    assert.equal([...f.timers.values()][0].ms, 60000);
    f.date(new Date(2026, 8, 1, 21)); f.document.hidden = true; f.listeners.get('visibilitychange')();
    assert.deepEqual(f.document.documentElement.dataset, {room: 'on', roomLight: 'night', roomSeason: 'autumn'});
    assert.equal(f.timers.size, 0);
    f.document.hidden = false; f.listeners.get('visibilitychange')(); assert.equal(f.timers.size, 1);
    assert.equal(f.writes(), 0);
});

test('private close cancels timers, removes scene and refuses later refresh', () => {
    const f = fixture('auto'); f.room.start(); const callback = [...f.timers.values()][0].fn;
    assert.equal(f.room.close(), true); callback(); f.room.refresh();
    assert.deepEqual(f.document.documentElement.dataset, {}); assert.equal(f.timers.size, 0);
    assert.equal(f.handlers.size, 0); assert.equal(f.listeners.size, 0); assert.equal(f.select.value, 'off');
});

test('a blocked save gives an actionable status', async () => {
    const f = fixture(); f.room.start(); f.storage.setItem = () => {throw Error('blocked');};
    f.select.value = 'night'; await f.handlers.get('change')();
    assert.match(f.status.textContent, /could not be saved/);
    assert.deepEqual(f.document.documentElement.dataset, {});
});
