const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const appSource = fs.readFileSync('static/js/app.js', 'utf8').replace(/\r\n/g, '\n');
const activitySource = fs.readFileSync('static/js/voice-activity.js', 'utf8');
const companionSource = fs.readFileSync('static/js/companions.js', 'utf8').replace(/\r\n/g, '\n');
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
    let resolve;
    const promise = new Promise(yes => {resolve = yes;});
    return {promise, resolve};
}
function slice(source, from, until) {
    const start = source.indexOf(from), end = source.indexOf(until, start);
    assert.ok(start >= 0 && end > start, `Expected actual source section ${from}`);
    return source.slice(start, end);
}
function oneFunction(source, name) {
    const declaration = source.indexOf(`function ${name}(`);
    assert.ok(declaration >= 0, `Expected actual function ${name}`);
    const start = source.lastIndexOf('\n', declaration) + 1;
    const end = source.indexOf('\n}\n', declaration) + 3;
    assert.ok(end > declaration, `Expected actual function body ${name}`);
    return source.slice(start, end);
}
function target() {
    const handlers = new Map();
    return {
        addEventListener(name, fn) {if (!handlers.has(name)) handlers.set(name, []); handlers.get(name).push(fn);},
        removeEventListener(name, fn) {handlers.set(name, (handlers.get(name) || []).filter(item => item !== fn));},
        dispatch(name, event = {}) {(handlers.get(name) || []).slice().forEach(fn => fn(event));},
    };
}
function element() {
    const classes = new Set();
    return {dataset: {}, style: {}, value: '', children: [], checked: false, disabled: false,
        classList: {add: value => classes.add(value), remove: value => classes.delete(value)},
        setAttribute(name, value) {this[name] = value;},
        append(child) {this.children.push(child);}, replaceChildren(...items) {this.children = items;}, remove() {},
    };
}

function integrated(options = {}) {
    const ids = ['autoInterruptToggle', 'autoInterruptStatus', 'micBtn', 'sendBtn', 'statusText', 'waveformCanvas', 'messageInput', 'messagesContainer', 'replyNotice'];
    const elements = new Map(ids.map(id => [id, element()]));
    const document = Object.assign(target(), {hidden: false,
        getElementById: id => elements.get(id) || null, createElement: () => element(), querySelectorAll: () => []});
    const streams = [], contexts = [], nodes = [], requests = [], notices = [], drafts = [], sent = [], values = new Map(), timers = new Map();
    let timerId = 0, manualRecorders = 0;
    const node = () => Object.assign(target(), {connect() {}, disconnect() {}});
    class AudioContext {
        constructor() {this.sampleRate = 16000; this.state = 'running'; this.audioWorklet = {addModule: async () => {}}; this.destination = {}; contexts.push(this);}
        createMediaStreamSource(stream) {this.stream = stream; return node();}
        createAnalyser() {return {};}
        createGain() {return Object.assign(node(), {gain: {value: 1}});}
        async resume() {}
        async close() {this.state = 'closed';}
    }
    class WorkletNode {
        constructor(context) {Object.assign(this, node()); this.context = context; this.port = {onmessage: null}; nodes.push(this);}
    }
    class MediaRecorder {constructor() {manualRecorders++; throw new Error('Automatic handoff must not create a MediaRecorder');}}
    const sandbox = vm.createContext(Object.assign(target(), {
        document, Float32Array, Int16Array, Uint8Array, DataView, Blob, FormData, AbortController, crypto: webcrypto, console,
        AudioWorkletNode: WorkletNode, AudioContext, MediaRecorder,
        currentSessionId: 1, isStreaming: false, activeChatTurn: options.turn || null,
        AlteraAPI: {epoch: 'original-data'}, Companion: {active: options.defaultOwner ? null : {id: 7}, current: {id: 7}}, speechAudio: {},
        navigator: {onLine: true, mediaDevices: {
            getSupportedConstraints: () => ({echoCancellation: true}),
            getUserMedia: async () => {
                const track = Object.assign(target(), {stops: 0, stop() {this.stops++;}, getSettings: () => ({echoCancellation: true})});
                const stream = {getTracks: () => [track], getAudioTracks: () => [track], track}; streams.push(stream); return stream;
            },
        }},
        setTimeout: (fn, delay) => {const id = ++timerId; timers.set(id, {fn, delay}); return id;}, clearTimeout: id => timers.delete(id),
        sessionStorage: {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)},
        ChatControls: {stop: async () => true, sync() {}, hydrate() {}, prepareNavigation: async () => true},
        pendingForSession: () => null,
        suppressSpeech: () => sandbox.run('voiceActivity?.setSpeaking(false)'),
        stopSpeech: () => sandbox.run('voiceActivity?.setSpeaking(false)'),
        updateSendButton() {}, drawWaveform() {}, cancelAnimationFrame() {}, connectionStatusText: () => 'Connected',
        alert: text => notices.push(text), showTypingIndicator() {}, hideTypingIndicator() {},
        appendMessage() {}, autoResize() {},
        restoreBrowserDrafts: async items => drafts.push(...items),
        showReplyNotice: text => notices.push(text),
        runChatStream: async payload => sent.push(payload),
        applyActiveAvatar() {}, loadCompanions() {}, refreshCompanionStatus() {}, updateSessionSignal() {}, scrollToBottom() {},
        retainNotesDraft: async () => {}, retainPhotoPrompt: async () => {}, resetCompanionEditors() {},
        resumePendingTurn: async () => {}, byId: id => elements.get(id),
        fetch: async (url, init = {}) => {
            requests.push({url, ...init});
            if (url.startsWith('/api/voice/activity')) return {ok: true, json: async () => ({available: true, speech: true,
                sample_rate: 16000, samples: 5120, session_id: 1,
                avatar_id: options.serverOwner === undefined ? options.defaultOwner ? null : 7 : options.serverOwner})};
            if (url === '/api/voice/stt') return options.sttResponse || {ok: true, json: async () => ({text: 'Words from this recording'})};
            if (url.startsWith('/api/messages/')) return {ok: true, json: async () => []};
            throw new Error(`Unexpected URL ${url}`);
        },
    }));
    sandbox.window = sandbox;
    require('./helpers/ordinary-page.cjs')(sandbox);
    sandbox.run = code => vm.runInContext(code, sandbox);
    vm.runInContext(activitySource, sandbox);
    vm.runInContext(slice(appSource, 'let voiceEnabled', '// Initialization'), sandbox);
    vm.runInContext(slice(appSource, 'async function toggleAutoInterruption', 'function drawWaveform'), sandbox);
    // The application listener above is registered before the engine's lifecycle listener is created.
    vm.runInContext(oneFunction(appSource, 'loadMessages'), sandbox);
    vm.runInContext(slice(appSource, 'const messageHistoryRequests', '\nfunction createMessageHTML'), sandbox);
    vm.runInContext(oneFunction(companionSource, 'enterCompanion'), sandbox);
    sandbox.run('voiceEnabled = true;');
    return {
        sandbox, streams, contexts, nodes, requests, notices, drafts, sent, values, elements, document, timers,
        manualRecorders: () => manualRecorders,
        state: code => sandbox.run(code),
        async begin() {
            await sandbox.toggleAutoInterruption(true); await tick();
            await this.audio(5120, 0.5); await this.audio(5120, 0.75);
            assert.equal(this.state('isRecording'), true, 'actual adapter accepted the engine handoff');
        },
        async audio(count = 5120, value = 0.25) {
            const worklet = nodes[0];
            worklet?.port.onmessage?.({data: {samples: new Float32Array(count).fill(value), sampleRate: 16000}});
            await tick();
        },
        async stop() {sandbox.stopRecording(); await tick();},
        hidden() {document.hidden = true; document.dispatch('visibilitychange');},
        sttRequests: () => requests.filter(item => item.url === '/api/voice/stt'),
    };
}

test('actual automatic adapter keeps the same stream and onset PCM in a local-only WAV transcription', async () => {
    const a = integrated(); await a.begin(); await a.audio(1600, -0.5);
    assert.equal(a.streams.length, 1, 'automatic recording must not reacquire permission or a second mic stream');
    assert.equal(a.contexts[1].stream, a.streams[0], 'the waveform uses the handed stream');
    assert.equal(a.manualRecorders(), 0);
    await a.stop();
    assert.equal(a.sttRequests().length, 1);
    const body = a.sttRequests()[0].body, file = body.get('file');
    assert.equal(body.get('offline_only'), 'true');
    assert.equal(file.name, 'recording.wav');
    assert.equal(file.type, 'audio/wav');
    const buffer = await file.arrayBuffer(), wav = new DataView(buffer);
    assert.equal(Buffer.from(buffer, 0, 4).toString(), 'RIFF');
    assert.equal(wav.getUint32(24, true), 16000);
    assert.equal(wav.getUint32(40, true), (5120 * 2 + 1600) * 2);
    assert.equal(wav.getInt16(44, true), 16384, 'the first detected voice frame remains in the final WAV');
    assert.equal(wav.getInt16(44 + 5120 * 2, true), 24575);
    assert.equal(wav.getInt16(buffer.byteLength - 2, true), -16384);
    assert.equal(a.sent.length, 1);
    assert.equal(a.sent[0].session_id, 1);
    assert.equal(a.sent[0].dataset_epoch, 'original-data');
});

test('background listener finishes the WAV before engine cleanup and keeps its transcript for review', async () => {
    const a = integrated(); await a.begin(); a.hidden(); await tick();
    assert.equal(a.sttRequests().length, 1, 'the root listener must finish PCM before the engine cancels it');
    assert.equal(a.sttRequests()[0].body.get('offline_only'), 'true');
    assert.equal(a.state('isRecording'), false);
    assert.equal(a.state('voiceActivity.getState().enabled'), false);
    assert.equal(a.sent.length, 0);
    assert.equal(a.drafts.length, 1);
    assert.equal(a.drafts[0].session_id, 1);
});

test('backgrounding after automatic recording stops still marks its pending STT transcript for review', async () => {
    const stt = deferred(); const a = integrated({sttResponse: stt.promise});
    await a.begin(); await a.stop();
    assert.equal(a.state('isRecording'), false);
    assert.equal(a.state('voiceTranscribing'), true);
    a.hidden();
    stt.resolve({ok: true, json: async () => ({text: 'Still the original foreground recording'})}); await tick();
    assert.equal(a.sent.length, 0);
    assert.equal(a.drafts.length, 1);
    assert.equal(a.drafts[0].message, 'Still the original foreground recording');
});

test('review-only is rechecked after the previous reply finishes while the page is hidden', async () => {
    const stopped = deferred();
    const turn = {payload: {}, terminal: null, settled: false, finished: stopped.promise};
    const a = integrated({turn}); await a.begin(); await a.stop();
    assert.equal(a.state('voiceTranscribing'), true);
    a.hidden(); turn.terminal = 'interrupted'; turn.settled = true;
    stopped.resolve({terminal: 'interrupted'}); await tick();
    assert.equal(a.sent.length, 0);
    assert.equal(a.drafts.length, 1);
});

test('companion navigation finalizes the original automatic capture before assigning the new session', async () => {
    const a = integrated(); await a.begin();
    const activityCapture = a.state('voiceCapture.activityCapture');
    const originalFinish = activityCapture.finish, sessionsAtFinish = [];
    activityCapture.finish = () => {sessionsAtFinish.push(a.sandbox.currentSessionId); return originalFinish();};
    await a.sandbox.enterCompanion({id: 8, session_id: 2, messages: 1}); await tick();
    assert.deepEqual(sessionsAtFinish, [1]);
    assert.equal(a.sandbox.currentSessionId, 2);
    assert.equal(a.state('isRecording'), false);
    assert.equal(a.state('voiceActivity.getState().enabled'), false);
    assert.equal(a.sent.length, 0);
    assert.equal(a.drafts.length, 1);
    assert.equal(a.drafts[0].session_id, 1);
});

test('loading a different session ends automatic capture for review before replacing displayed messages', async () => {
    const a = integrated(); await a.begin();
    await a.sandbox.loadMessages(2); await tick();
    assert.equal(a.state('isRecording'), false);
    assert.equal(a.state('voiceActivity.getState().enabled'), false);
    assert.equal(a.sent.length, 0);
    assert.equal(a.drafts.length, 1);
    assert.equal(a.drafts[0].session_id, 1);
});

test('turning automatic interruption off before Stop handles an already-cancelled capture safely', async () => {
    const a = integrated(); await a.begin();
    await a.sandbox.toggleAutoInterruption(false); await a.stop();
    assert.equal(a.state('isRecording'), false);
    assert.equal(a.state('voiceTranscribing'), false);
    assert.equal(a.sttRequests().length, 0);
    assert.equal(a.sent.length, 0);
    assert.equal(a.streams[0].track.stops > 0, true);
});

test('the default conversation accepts the server default owner without inventing an avatar', async () => {
    const a = integrated({defaultOwner: true}); await a.begin();
    await a.stop();
    assert.equal(a.sent.length, 1);
    assert.equal(a.state('voiceActivity.getState().enabled'), true);
});

test('a server avatar cannot be mistaken for the default conversation during detector response validation', async () => {
    const a = integrated({defaultOwner: true, serverOwner: 7});
    await a.sandbox.toggleAutoInterruption(true); await tick(); await a.audio();
    assert.equal(a.state('isRecording'), false);
    assert.equal(a.state('voiceActivity.getState().enabled'), false);
    assert.equal(a.sttRequests().length, 0);
    assert.match(a.elements.get('autoInterruptStatus').textContent, /invalid or changed conversation/);
});
