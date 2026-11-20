const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/js/voice-activity.js', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
    return {promise, resolve, reject};
}

function eventTarget() {
    const handlers = new Map();
    return {
        addEventListener(name, fn) {if (!handlers.has(name)) handlers.set(name, new Set()); handlers.get(name).add(fn);},
        removeEventListener(name, fn) {handlers.get(name)?.delete(fn);},
        dispatch(name, event = {}) {for (const fn of handlers.get(name) || []) fn(event);},
        listeners: () => [...handlers.values()].reduce((count, items) => count + items.size, 0),
    };
}

function monitor(options = {}) {
    let now = 0, nextTimer = 0;
    const timers = new Map(), states = [], handoffs = [], requests = [], constraints = [], contexts = [], nodes = [], streams = [];
    const contextData = {sessionId: 1, datasetEpoch: 'data-one', companionId: 7};
    const document = Object.assign(eventTarget(), {hidden: false});
    const window = eventTarget();
    const makeStream = () => {
        const track = Object.assign(eventTarget(), {stops: 0, stop() {this.stops++;},
            getSettings: () => ({echoCancellation: options.echoEnabled ?? true})});
        const stream = {track, getTracks: () => [track], getAudioTracks: () => [track]};
        streams.push(stream); return stream;
    };
    const audioNode = () => Object.assign(eventTarget(), {connections: [], disconnects: 0,
        connect(node) {this.connections.push(node);}, disconnect() {this.disconnects++;}});
    class AudioContext {
        constructor() {
            this.sampleRate = options.sourceRate || 16000; this.state = 'running'; this.closes = 0;
            this.audioWorklet = {addModule: async url => {this.moduleUrl = url; if (options.worklet) await options.worklet;}};
            this.destination = {}; contexts.push(this);
        }
        createMediaStreamSource(stream) {this.stream = stream; return this.source = audioNode();}
        createGain() {this.gainNode = Object.assign(audioNode(), {gain: {value: 1}}); return this.gainNode;}
        async resume() {if (options.resumeFailure) throw new Error('Audio context could not resume');}
        async close() {this.closes++; this.state = 'closed';}
    }
    class WorkletNode {
        constructor(context, name, settings) {
            Object.assign(this, audioNode()); this.port = {onmessage: null}; this.context = context;
            this.name = name; this.settings = settings; nodes.push(this);
        }
    }
    const DateClock = class extends Date {static now() {return now;}};
    const sandbox = vm.createContext({window, document, Blob, Float32Array, Int16Array, Uint8Array, DataView,
        AudioWorkletNode: options.noWorklet ? undefined : WorkletNode,
        AbortController: options.abortThrows ? class {constructor() {this.signal = {};} abort() {throw new Error('Abort failed');}} : AbortController,
        Date: DateClock, console,
        navigator: {onLine: true, mediaDevices: {
            getSupportedConstraints: () => ({echoCancellation: options.echoSupported ?? true}),
            getUserMedia: async value => {
                constraints.push(value);
                if (options.permission) return options.permission;
                return makeStream();
            },
        }},
        setTimeout: (fn, delay) => {const id = ++nextTimer; timers.set(id, {fn, at: now + delay}); return id;},
        clearTimeout: id => timers.delete(id),
        fetch: async (url, init) => {
            requests.push({url, ...init});
            if (options.fetch) return options.fetch(requests.length, url, init);
            const speech = (options.speech || []).shift() ?? false;
            return {ok: true, json: async () => ({available: true, speech, sample_rate: 16000, samples: 5120, session_id: 1, avatar_id: 7})};
        },
    });
    window.AudioContext = AudioContext;
    vm.runInContext(source, sandbox);
    const engine = window.VoiceActivity.create({getContext: () => contextData,
        onState: state => states.push(state), onSpeech: data => {handoffs.push(data); return options.onSpeech?.(data);}});
    return {
        engine, sandbox, window, document, contextData, states, handoffs, requests, constraints, contexts, nodes, streams, makeStream, timers,
        async start() {await engine.enable(); engine.setSpeaking(true); await tick();},
        async audio(count = 5120, value = 0.25) {
            const node = nodes.at(-1);
            node?.port.onmessage?.({data: {samples: typeof value === 'number' ? new Float32Array(count).fill(value) : value,
                sampleRate: node.context.sampleRate}});
            await tick();
        },
        async advance(ms) {
            const until = now + ms;
            while (true) {
                const due = [...timers].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
                if (!due) break;
                now = due[1].at; timers.delete(due[0]); due[1].fn(); await tick();
            }
            now = until; await tick();
        },
    };
}

test('monitor starts off without acquiring the mic; opt-in acquires only while Altera speaks', async () => {
    const a = monitor();
    a.engine.setSpeaking(true); await tick();
    assert.equal(a.constraints.length, 0);
    a.engine.setSpeaking(false); await a.engine.enable();
    assert.equal(a.constraints.length, 0);
    assert.equal(a.engine.getState().phase, 'waiting');
    a.engine.setSpeaking(true); await tick();
    assert.equal(a.constraints.length, 1);
    assert.equal(a.engine.getState().phase, 'listening');
    assert.equal(a.constraints[0].audio.echoCancellation.exact, true);
    assert.equal(a.contexts[0].moduleUrl, '/static/js/voice-capture-worklet.js');
    assert.equal(a.contexts[0].gainNode.gain.value, 0);
    assert.match(a.states.at(-1).message, /local speech detector.*Headphones/);
    a.engine.disable();
});

test('a new chat waits for an accepted session before acquiring the microphone', async () => {
    const a = monitor(); a.contextData.sessionId = null;
    await a.start();
    assert.equal(a.constraints.length, 0);
    a.contextData.sessionId = 1;
    a.engine.setSpeaking(true); await tick();
    assert.equal(a.constraints.length, 1);
    a.engine.disable();
});

test('speech requires two sustained local VAD positives; a negative resets the onset', async () => {
    const a = monitor({speech: [true, false, true, true]}); await a.start();
    await a.audio(); assert.equal(a.handoffs.length, 0);
    await a.audio(); assert.equal(a.handoffs.length, 0);
    await a.audio(); assert.equal(a.handoffs.length, 0);
    await a.audio(); assert.equal(a.handoffs.length, 1);
    assert.equal(a.requests.length, 4);
    assert.equal(a.requests[0].url, '/api/voice/activity?session_id=1');
    assert.equal(a.requests[0].headers['Content-Type'], 'audio/pcm');
    assert.equal(a.requests[0].headers['X-Audio-Sample-Rate'], '16000');
    assert.equal(a.requests[0].headers['X-Altera-Dataset'], 'data-one');
    assert.equal(a.requests[0].body.byteLength, 10240);
    assert.equal(new DataView(a.requests[0].body.buffer).getInt16(0, true), 8192);
    assert.equal(a.engine.getState().phase, 'capturing');
    a.handoffs[0].capture.cancel(); a.engine.disable();
});

test('loud audio alone never counts as speech when local VAD returns negative', async () => {
    const a = monitor(); await a.start();
    for (let i = 0; i < 5; i++) await a.audio(5120, 1);
    assert.equal(a.handoffs.length, 0);
    assert.equal(a.engine.getState().phase, 'listening');
    a.engine.disable();
});

test('handoff keeps pre-roll and the same stream; continued recording becomes one valid PCM WAV', async () => {
    const a = monitor({speech: [false, false, true, true]}); await a.start();
    await a.audio(5120, 0); await a.audio(5120, 0);
    await a.audio(5120, 0.5); await a.audio(5120, 0.75);
    const handoff = a.handoffs[0];
    assert.equal(handoff.stream, a.streams[0]);
    assert.equal(a.constraints.length, 1);
    assert.equal(handoff.sampleRate, 16000);
    assert.equal(handoff.prefixPCM.length, 20480);
    assert.equal(handoff.prefixPCM[10240], 16384, 'the first spoken samples precede the detection decision');
    assert.equal(handoff.prefixPCM[15360], 24575);
    a.engine.setSpeaking(false);
    assert.equal(a.streams[0].track.stops, 0, 'silencing Altera must not close the handed recording');
    handoff.prefixPCM.fill(0);
    await a.audio(1600, -0.5);
    const blob = handoff.capture.finish();
    assert.equal(handoff.capture.finish(), blob, 'finish is idempotent');
    assert.equal(blob.type, 'audio/wav');
    const buffer = await blob.arrayBuffer(), view = new DataView(buffer);
    assert.equal(Buffer.from(buffer, 0, 4).toString(), 'RIFF');
    assert.equal(Buffer.from(buffer, 8, 4).toString(), 'WAVE');
    assert.equal(view.getUint16(20, true), 1);
    assert.equal(view.getUint16(22, true), 1);
    assert.equal(view.getUint32(24, true), 16000);
    assert.equal(view.getUint16(34, true), 16);
    assert.equal(view.getUint32(40, true), 22080 * 2);
    assert.equal(buffer.byteLength, 44 + 22080 * 2);
    assert.equal(view.getInt16(44 + 10240 * 2, true), 16384, 'caller changes cannot corrupt the recording prefix');
    assert.equal(view.getInt16(buffer.byteLength - 2, true), -16384);
    assert.equal(a.streams[0].track.stops, 1);
    assert.equal(a.contexts[0].closes, 1);
    assert.equal(a.nodes[0].port.onmessage, null);
    a.engine.disable();
});

test('slow VAD retains the first words and serializes requests without building an unbounded frame queue', async () => {
    const first = deferred(), second = deferred();
    const response = speech => ({ok: true, json: async () => ({available: true, speech, sample_rate: 16000, samples: 5120, session_id: 1, avatar_id: 7})});
    const a = monitor({fetch: index => index === 1 ? first.promise : second.promise}); await a.start();
    await a.audio(5120, 0.4); await a.audio(5120, 0.5); await a.audio(5120, 0.6);
    assert.equal(a.requests.length, 1);
    first.resolve(response(true)); await tick();
    assert.equal(a.requests.length, 2);
    second.resolve(response(true)); await tick();
    assert.equal(a.handoffs.length, 1);
    assert.equal(a.handoffs[0].prefixPCM.length, 15360);
    assert.equal(a.handoffs[0].prefixPCM[0], 13107);
    assert.equal(a.handoffs[0].prefixPCM.at(-1), 19660);
    assert.equal(a.requests.length, 2);
    a.engine.disable();
});

for (const sourceRate of [44100, 48000]) {
    test(`${sourceRate} Hz microphone samples are resampled into fixed 16 kHz PCM frames`, async () => {
        const a = monitor({sourceRate}); await a.start();
        for (let i = 0; i < sourceRate; i += 1024) await a.audio(Math.min(1024, sourceRate - i), 0.5);
        assert.equal(a.requests.length, 3, 'one second provides three complete 320ms windows');
        for (const request of a.requests) {
            assert.equal(request.body.byteLength, 10240);
            assert.equal(new DataView(request.body.buffer).getInt16(0, true), 16384);
        }
        a.engine.disable();
    });
}

test('turning the option off aborts VAD and ignores late positive responses', async () => {
    const reply = deferred(); const a = monitor({fetch: () => reply.promise}); await a.start(); await a.audio();
    a.engine.disable();
    assert.equal(a.requests[0].signal.aborted, true);
    assert.equal(a.streams[0].track.stops, 1);
    reply.resolve({ok: true, json: async () => ({available: true, speech: true, sample_rate: 16000, samples: 5120, session_id: 1, avatar_id: 7})});
    await tick();
    assert.equal(a.handoffs.length, 0);
    assert.equal(a.engine.getState().enabled, false);
    assert.equal(a.timers.size, 0);
});

test('switching off while microphone permission is pending releases the eventual stream', async () => {
    const permission = deferred(); const a = monitor({permission: permission.promise});
    await a.start(); assert.equal(a.engine.getState().phase, 'starting');
    a.engine.disable(); const stream = a.makeStream(); permission.resolve(stream); await tick();
    assert.equal(stream.track.stops, 1);
    assert.equal(a.contexts.length, 0);
    assert.equal(a.engine.getState().enabled, false);
});

test('manual recording suspends and releases the monitor until a new explicit opt-in', async () => {
    const a = monitor(); await a.start(); a.engine.suspend();
    assert.equal(a.streams[0].track.stops, 1);
    a.engine.setSpeaking(false); a.engine.setSpeaking(true); await tick();
    assert.equal(a.constraints.length, 1);
    await a.engine.enable();
    assert.equal(a.constraints.length, 2);
    a.engine.disable();
});

for (const event of ['visibilitychange', 'pagehide', 'restore']) {
    test(`${event} releases the mic and foregrounding never re-acquires without opt-in`, async () => {
        const a = monitor(); await a.start();
        if (event === 'visibilitychange') {a.document.hidden = true; a.document.dispatch('visibilitychange');}
        else if (event === 'pagehide') a.window.dispatch('pagehide');
        else a.window.dispatch('pageshow', {persisted: true});
        assert.equal(a.streams[0].track.stops, 1);
        assert.equal(a.engine.getState().enabled, false);
        a.document.hidden = false; a.document.dispatch('visibilitychange');
        a.engine.setSpeaking(true); await tick();
        assert.equal(a.constraints.length, 1);
    });
}

for (const key of ['sessionId', 'datasetEpoch', 'companionId']) {
    test(`changing ${key} invalidates stale detector results and releases captured context`, async () => {
        const a = monitor(); await a.start();
        a.contextData[key] = key === 'datasetEpoch' ? 'data-two' : 2;
        await a.audio();
        assert.equal(a.requests.length, 0);
        assert.equal(a.streams[0].track.stops, 1);
        assert.equal(a.engine.getState().enabled, false);
        assert.match(a.states.at(-1).message, /conversation or data changed/);
    });
}

for (const status of [400, 404, 409, 503]) {
    test(`HTTP ${status} disables visibly without retrying the backend`, async () => {
        const a = monitor({fetch: async () => ({ok: false, status})}); await a.start(); await a.audio();
        assert.equal(a.engine.getState().phase, 'error');
        assert.equal(a.streams[0].track.stops, 1);
        await a.audio(); await a.advance(10000);
        assert.equal(a.requests.length, 1);
        assert.match(a.states.at(-1).message, new RegExp(`HTTP ${status}`));
    });
}

test('timed-out VAD closes the mic and a late server response cannot revive monitoring', async () => {
    const response = deferred(); const a = monitor({fetch: () => response.promise}); await a.start(); await a.audio();
    await a.advance(1800);
    assert.equal(a.engine.getState().phase, 'error');
    assert.equal(a.streams[0].track.stops, 1);
    assert.equal(a.requests[0].signal.aborted, true);
    assert.match(a.states.at(-1).message, /timed out/);
    response.resolve({ok: true, json: async () => ({speech: true})}); await tick();
    assert.equal(a.handoffs.length, 0);
    assert.equal(a.requests.length, 1);
});

test('malformed VAD and cancellation failures disable visibly', async () => {
    const malformed = monitor({fetch: async () => ({ok: true, json: async () => ({available: true, speech: 'yes'})})});
    await malformed.start(); await malformed.audio();
    assert.equal(malformed.engine.getState().phase, 'error');
    assert.match(malformed.states.at(-1).message, /invalid/);
    const pending = deferred(); const abort = monitor({fetch: () => pending.promise, abortThrows: true});
    await abort.start(); await abort.audio(); abort.engine.disable();
    assert.equal(abort.streams[0].track.stops, 1);
    assert.equal(abort.engine.getState().phase, 'error');
    assert.match(abort.states.at(-1).message, /cleanup could not be confirmed/);
});

test('fetch failure closes the mic and does not retry without another user opt-in', async () => {
    const a = monitor({fetch: async () => {throw new Error('Local connection lost');}});
    await a.start(); await a.audio();
    assert.equal(a.engine.getState().phase, 'error');
    assert.equal(a.streams[0].track.stops, 1);
    assert.match(a.states.at(-1).message, /Local connection lost/);
    a.engine.setSpeaking(false); a.engine.setSpeaking(true); await tick();
    assert.equal(a.requests.length, 1);
    assert.equal(a.constraints.length, 1);
});

test('a VAD reply for a changed dataset cannot trigger a recording', async () => {
    const reply = deferred(); const a = monitor({fetch: () => reply.promise});
    await a.start(); await a.audio();
    a.contextData.datasetEpoch = 'new-data';
    reply.resolve({ok: true, json: async () => ({available: true, speech: true, sample_rate: 16000, samples: 5120, session_id: 1, avatar_id: 7})});
    await tick();
    assert.equal(a.handoffs.length, 0);
    assert.equal(a.engine.getState().enabled, false);
    assert.equal(a.streams[0].track.stops, 1);
});

test('device and worklet failures disable the option and release their tracks', async () => {
    for (const failure of ['track', 'processor']) {
        const a = monitor(); await a.start();
        if (failure === 'track') a.streams[0].track.dispatch('ended');
        else a.nodes[0].dispatch('processorerror');
        assert.equal(a.engine.getState().phase, 'error');
        assert.equal(a.streams[0].track.stops, 1);
        assert.equal(a.engine.getState().enabled, false);
    }
});

test('a rejected handoff disables visibly and cancels capture without leaking its stream', async () => {
    const a = monitor({speech: [true, true], onSpeech: () => false});
    await a.start(); await a.audio(); await a.audio();
    assert.equal(a.handoffs.length, 1);
    assert.equal(a.streams[0].track.stops, 1);
    assert.equal(a.engine.getState().phase, 'error');
    assert.match(a.states.at(-1).message, /Recording could not start/);
    assert.throws(() => a.handoffs[0].capture.finish(), /cancelled/);
});

test('unsupported AudioWorklet and failed echo cancellation use visible manual-only fallback', async () => {
    const unsupported = monitor({noWorklet: true}); await unsupported.start();
    assert.equal(unsupported.constraints.length, 0);
    assert.equal(unsupported.engine.getState().enabled, false);
    assert.match(unsupported.states.at(-1).message, /AudioWorklet.*microphone button/);
    const echo = monitor({echoEnabled: false}); await echo.start();
    assert.equal(echo.streams[0].track.stops, 1);
    assert.equal(echo.engine.getState().phase, 'error');
    assert.match(echo.states.at(-1).message, /echo cancellation/);
});

test('a cooldown prevents immediate repeated interruption and disabling cancels its timer', async () => {
    const a = monitor({speech: [true, true]}); await a.start(); await a.audio(); await a.audio();
    a.engine.setSpeaking(false); a.handoffs[0].capture.finish();
    a.engine.setSpeaking(true); await tick();
    assert.equal(a.engine.getState().phase, 'cooldown');
    assert.equal(a.constraints.length, 1);
    await a.advance(2499); assert.equal(a.constraints.length, 1);
    await a.advance(1); assert.equal(a.constraints.length, 2);
    a.engine.disable();
    assert.equal(a.timers.size, 0);
});

test('background cancellation ends handed recordings and destroy removes lifecycle listeners', async () => {
    const a = monitor({speech: [true, true]}); await a.start(); await a.audio(); await a.audio();
    const handoff = a.handoffs[0];
    a.document.hidden = true; a.document.dispatch('visibilitychange');
    assert.equal(a.streams[0].track.stops, 1);
    assert.throws(() => handoff.capture.finish(), /cancelled/);
    handoff.capture.cancel();
    a.engine.destroy();
    assert.equal(a.document.listeners(), 0);
    assert.equal(a.window.listeners(), 0);
    a.document.hidden = false;
    assert.equal(await a.engine.enable(), false);
    assert.equal(a.constraints.length, 1, 'destroyed monitors cannot acquire without lifecycle guards');
});

test('the local worklet batches real input samples and always outputs silence', () => {
    let Processor;
    const messages = [];
    class Base {constructor() {this.port = {postMessage: (data, transfers) => messages.push({data, transfers})};}}
    const sandbox = vm.createContext({AudioWorkletProcessor: Base, sampleRate: 48000, Float32Array,
        registerProcessor: (name, processor) => {assert.equal(name, 'altera-voice-capture'); Processor = processor;}});
    vm.runInContext(fs.readFileSync('static/js/voice-capture-worklet.js', 'utf8'), sandbox);
    const processor = new Processor();
    for (let i = 0; i < 8; i++) {
        const output = new Float32Array(128).fill(1);
        assert.equal(processor.process([[new Float32Array(128).fill(i / 8)]], [[output]]), true);
        assert.equal(output.every(value => value === 0), true);
    }
    assert.equal(messages.length, 1);
    assert.equal(messages[0].data.sampleRate, 48000);
    assert.equal(messages[0].data.samples.length, 1024);
    assert.equal(messages[0].data.samples[128], 0.125);
    assert.equal(messages[0].data.samples[1023], 0.875);
    assert.equal(messages[0].transfers[0], messages[0].data.samples.buffer);
});
