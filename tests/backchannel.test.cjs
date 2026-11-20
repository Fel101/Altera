const test = require('node:test');
const assert = require('node:assert/strict');
const {create} = require('../static/js/backchannel.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const validUrl = '/static/audio/tts/synthetic_123.wav';

function fixture(initial = null) {
    let saved = initial, next = 0, clock = 100000, permitted = true, requestImpl = null;
    const timers = new Map(), handlers = new Map(), events = new Map(), clips = [], requests = [];
    const element = name => ({checked: false, disabled: false, textContent: '',
        addEventListener: (event, fn) => handlers.set(name + ':' + event, fn),
        removeEventListener: event => handlers.delete(name + ':' + event)});
    const toggle = element('toggle'), button = element('button'), preview = element('preview'), status = element('status');
    const document = {hidden: false, getElementById: id => ({backchannelToggle:toggle, backchannelPrepare:button, backchannelPreview:preview, backchannelStatus:status})[id],
        addEventListener: (event, fn) => events.set(event, fn), removeEventListener: event => events.delete(event)};
    const storage = {getItem: () => saved, setItem: (_, value) => {saved = value;}};
    const request = async (...args) => {requests.push(args); return requestImpl ? requestImpl(...args) : {ok:true,json:async()=>({audio_url:validUrl})};};
    const makeAudio = url => {const clip = {url, paused: false, removed: false, play: () => Promise.resolve(),
        pause() {this.paused = true;}, removeAttribute() {this.removed = true;}, load() {}}; clips.push(clip); return clip;};
    const control = create({document, storage, request, makeAudio, setTimer: fn => {const id = ++next; timers.set(id, fn); return id;},
        clearTimer: id => timers.delete(id), now: () => clock, flush: async()=>{}, active: () => true,
        eligible: () => permitted, canPrepare: () => permitted});
    control.start(fn => fn());
    return {control, document, toggle, button, preview, status, handlers, events, timers, clips, requests,
        save: () => saved, permit: value => {permitted = value;}, advance: n => {clock += n;}, request: fn => {requestImpl = fn;}};
}
const saved = JSON.stringify({enabled:true,url:validUrl});
function runTimer(f) {const [id, fn] = f.timers.entries().next().value; f.timers.delete(id); fn();}

test('loading is quiet and defaults off without inference', () => {
    for (const initial of [null, 'bad', JSON.stringify({enabled:true,url:'https://outside.invalid/a.mp3'})]) {
        const f = fixture(initial); f.control.schedule({});
        assert.equal(f.toggle.checked, false); assert.equal(f.timers.size, 0); assert.equal(f.requests.length, 0);
    }
});

test('explicit preparation sends only the fixed phrase and requires a safe local cache URL', async () => {
    const f = fixture(); f.handlers.get('button:click')(); await tick();
    assert.equal(f.requests.length, 1); assert.equal(JSON.parse(f.requests[0][1].body).text, 'Mm-hmm.');
    assert.deepEqual(JSON.parse(f.save()), {enabled:false,url:validUrl});
    assert.equal(f.preview.disabled, false); f.handlers.get('preview:click')(); assert.equal(f.clips.length, 1);
    f.control.stop(); assert.equal(f.clips[0].paused, true);
    f.request(async () => ({ok:true,json:async()=>({audio_url:'https://outside.invalid/a.wav'})}));
    f.handlers.get('button:click')(); await tick();
    assert.equal(JSON.parse(f.save()).url, validUrl); assert.match(f.status.textContent, /could not be prepared/);
});

test('a single quiet clip is canceled immediately and rate limited across turns', () => {
    const f = fixture(saved); const turn = {}; f.control.schedule(turn);
    assert.equal(f.timers.size, 1); runTimer(f);
    assert.equal(f.clips.length, 1); assert.equal(f.clips[0].volume, .4);
    f.control.stop(); assert.equal(f.clips[0].paused, true); assert.equal(f.clips[0].removed, true);
    f.control.schedule(turn); assert.equal(f.timers.size, 0);
    f.advance(60000); f.control.schedule(turn); assert.equal(f.timers.size, 1);
});

test('recording, hidden page, mute and closure suppress late timers and playback', async () => {
    const f = fixture(saved); f.control.schedule({}); f.permit(false); runTimer(f); assert.equal(f.clips.length, 0);
    f.permit(true); f.control.schedule({}); const late = [...f.timers.values()][0];
    f.document.hidden = true; f.events.get('visibilitychange')(); late(); assert.equal(f.clips.length, 0);
    f.document.hidden = false; f.control.schedule({}); runTimer(f); f.toggle.checked = false;
    f.handlers.get('toggle:change')(); await tick(); assert.equal(f.clips[0].paused, true);
    f.control.close(); assert.equal(f.handlers.size, 0); assert.equal(f.events.size, 0);
    f.control.schedule({}); assert.equal(f.timers.size, 0);
});

test('late preparation after private closure cannot publish its result', async () => {
    const f = fixture(); let resolve;
    f.request(() => new Promise(r => {resolve = r;})); f.handlers.get('button:click')();
    assert.equal(f.button.disabled, true); f.control.close();
    resolve({ok:true,json:async()=>({audio_url:validUrl})}); await tick();
    assert.equal(f.save(), null); assert.equal(f.requests[0][1].signal.aborted, true);
});
