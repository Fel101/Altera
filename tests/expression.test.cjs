const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture() {
    let now = 0, next = 0;
    const timers = new Map(), layers = new Map();
    for (const id of ['livingMood', 'livingQuick']) layers.set(id, {src: '', dataset: {},
        classList: {add() {}, remove() {}}});
    const context = vm.createContext({console, Set, WeakSet, Math, Number, Date: class extends Date {static now() {return now;}},
        currentSessionId: 1, Companion: {active: {id: 'a'}}, AlteraAPI: {epoch: 'one'},
        document: {hidden: false, readyState: 'loading', addEventListener() {},
            documentElement: {dataset: {her: 'idle'}}, getElementById: id => layers.get(id)},
        setTimeout: (fn, ms) => {const id = ++next; timers.set(id, {fn, ms}); return id;},
        clearTimeout: id => timers.delete(id), cancelAnimationFrame() {}, requestAnimationFrame: () => 10});
    require('./helpers/ordinary-page.cjs')(context);
    vm.runInContext(fs.readFileSync('static/js/living.js', 'utf8'), context);
    vm.runInContext("Living.avatarId='a'; Living.frames=new Set(['happy','sad','thinking','surprised','talking']); Living.urls={happy:'happy',sad:'sad',thinking:'thinking'};", context);
    const run = code => vm.runInContext(code, context);
    const apply = (extra = {}) => context.setCompanionExpression({simulated: true, frame: 'happy', intensity: .6},
        {session_id: 1, avatar_id: 'a', dataset_epoch: 'one', ...extra});
    return {context, run, apply, timers, advance: ms => {now += ms;}};
}

test('persistent cue returns after the temporary reply expression and decays locally', () => {
    const f = fixture(); f.apply();
    assert.equal(f.run('Living.mood'), 'happy');
    f.run("holdMood('sad')");
    assert.equal(f.run('Living.mood'), 'sad');
    [...f.timers.values()].find(t => t.ms === 7000).fn();
    assert.equal(f.run('Living.mood'), 'happy');
    f.advance(18 * 3600 * 1000);
    assert.equal(f.run('expressionFrame()'), null);
});

test('late responses for a different owner, session or dataset cannot update the portrait', () => {
    for (const change of [{avatar_id: 'b'}, {session_id: 2}, {dataset_epoch: 'old'}]) {
        const f = fixture(); f.apply(change);
        assert.equal(f.run('Living.expression'), null);
        assert.equal(f.timers.size, 0);
    }
});

test('speech keeps the mouth frame and restores a cue only after speaking stops', () => {
    const f = fixture();
    f.context.document.documentElement.dataset.companion = 'speaking';
    f.apply();
    assert.equal(f.run('Living.mood'), null);
    f.run("onCompanionState('speaking')");
    assert.equal(f.run('Living.mood'), null);
    assert.equal(f.run('Living.talkRaf'), 10);
    f.context.document.documentElement.dataset.companion = 'idle';
    f.run("onCompanionState('idle')");
    assert.equal(f.run('Living.mood'), 'happy');
});

test('reset removes the previous owner cue, pending reply and timer', () => {
    const f = fixture(); f.apply(); f.run("Living.pendingMood='sad'; resetLiving()");
    assert.equal(f.run('Living.expression'), null);
    assert.equal(f.run('Living.pendingMood'), undefined);
    assert.equal(f.run('Living.mood'), null);
    assert.equal(f.timers.size, 0);
});

test('malformed or unavailable frames fall back to the resting portrait', () => {
    const f = fixture(); f.apply();
    f.context.setCompanionExpression({simulated: false, frame: 'happy', intensity: .6},
        {session_id: 1, avatar_id: 'a', dataset_epoch: 'one'});
    assert.equal(f.run('Living.mood'), null);
    f.context.setCompanionExpression({simulated: true, frame: 'flirty', intensity: .6},
        {session_id: 1, avatar_id: 'a', dataset_epoch: 'one'});
    assert.equal(f.run('Living.mood'), null);
});
