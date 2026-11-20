const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const source = fs.readFileSync('static/js/app.js', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return {promise, resolve, reject};
}

class Element {
    constructor(tag = 'div') {
        this.tag = tag; this.children = []; this.dataset = {}; this.listeners = {};
        this.value = ''; this.textContent = ''; this.style = {}; this.id = '';
        const classes = new Set();
        this.classList = {add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name)};
    }
    setAttribute(name, value) { this[name] = value; }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    append(...children) { children.forEach(child => {child.parentElement = this; this.children.push(child);}); }
    appendChild(child) { this.append(child); }
    after(child) { this.parentElement?.append(child); }
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); this.removed = true; }
    querySelector(selector) {
        if (selector === '.message-content') return this.content || null;
        if (selector === '.message-time') return this.time || null;
        return this.children.find(child => selector === `.${child.className}`) || null;
    }
    querySelectorAll() { return []; }
    focus() {}
}

function wire() {
    const chunks = [], waiting = [];
    let ended = false;
    return {
        push(event) {
            const item = {done: false, value: new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`)};
            if (waiting.length) waiting.shift()(item); else chunks.push(item);
        },
        end() { ended = true; while (waiting.length) waiting.shift()({done: true}); },
        read() { return chunks.length ? Promise.resolve(chunks.shift()) : ended ? Promise.resolve({done: true}) : new Promise(resolve => waiting.push(resolve)); },
    };
}

function app(options = {}) {
    const elements = new Map(['messageInput', 'sendBtn', 'stopReplyBtn', 'micBtn', 'statusText', 'waveformCanvas', 'messagesContainer']
        .map(id => {const el = new Element(); el.id = id; return [id, el];}));
    const values = new Map(), requests = [], streams = [], recordings = [], sounds = [], notices = [], drafts = [], alerts = [];
    const track = {stopped: false, stop() {this.stopped = true;}};
    const mediaStream = {getTracks: () => [track]};
    const container = elements.get('messagesContainer');
    const document = {
        getElementById: id => elements.get(id) || container.children.find(el => el.id === id && !el.removed) || null,
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: tag => new Element(tag),
    };
    class Recorder {
        constructor(stream) {this.stream = stream; recordings.push(this);}
        start() {this.started = true;}
        stop() {this.ondataavailable({data: new Blob(['recorded speech'])}); this.completion = this.onstop();}
    }
    if (options.unsupportedRecording) Recorder.isTypeSupported = () => false;
    class AudioContext {
        createMediaStreamSource() {return {connect() {}};}
        createAnalyser() {return {};}
        async close() {this.closed = true;}
    }
    class Audio {
        constructor() {sounds.push(this); this.plays = 0; this.pauses = 0;}
        play() {this.plays++; return Promise.resolve();}
        pause() {this.pauses++;}
    }
    const context = vm.createContext({
        window: {AudioContext, addEventListener() {}, confirm: () => Boolean(options.keepFailedRecording)}, document, currentSessionId: 1, isStreaming: false, AlteraAPI: {epoch: 'dataset-one'},
        Companion: {active: {id: 10}}, crypto: webcrypto, TextDecoder, Blob, FormData, AbortController, MediaRecorder: Recorder, Audio,
        navigator: {mediaDevices: {getUserMedia: () => options.permission || Promise.resolve(mediaStream)}},
        sessionStorage: {getItem: key => values.get(key), setItem: (key, value) => {
            if (options.sessionStorageFailure) throw new Error('Browser storage unavailable');
            values.set(key, value);
        }, removeItem: key => values.delete(key)},
        console: {warn() {}, error() {}}, alert: message => alerts.push(message), cancelAnimationFrame() {},
        autoResize() {}, drawWaveform() {}, updateCompanionState() {}, showTypingIndicator() {}, hideTypingIndicator() {},
        updateTypingIndicator() {}, loadSessions() {}, setTimeout() {}, clearTimeout() {},
        restoreBrowserDrafts: async items => { if (options.draftFailure) throw new Error('Draft storage unavailable'); drafts.push(...items); },
        fetch: async (url, init = {}) => {
            const request = {url, ...init};
            if (typeof init.body === 'string') request.payload = JSON.parse(init.body);
            requests.push(request);
            if (url === '/api/chat/stream') {
                const stream = wire(); streams.push(stream);
                if (options.autoVoiceReply && streams.length > 1) {
                    stream.push({status: 'accepted', session_id: 1});
                    stream.push({response: 'A new reply'});
                    stream.push({status: 'saved', session_id: 1, message_id: 4, user_message_id: 3});
                    stream.end();
                }
                return {ok: true, body: {getReader: () => stream}};
            }
            if (url.endsWith('/stop')) return options.stopResponse || {ok: true, json: async () => ({status: 'stopping'})};
            if (url === '/api/voice/stt') return options.sttResponse || {ok: true, json: async () => ({text: 'My new thought'})};
            if (url === '/api/voice/tts') return options.ttsResponse || {ok: true, json: async () => ({})};
            throw new Error(`Unexpected request ${url}`);
        },
        appendMessage: (role, text) => {
            const el = new Element(); el.role = role; el.content = new Element(); el.content.textContent = text;
            el.time = new Element(); el.append(el.content, el.time); container.append(el); return el;
        },
        updateStreamingMessage: text => {
            let el = document.getElementById('streamingMsg');
            if (!el) {el = context.appendMessage('companion', ''); el.id = 'streamingMsg';}
            el.content.textContent = text;
        },
    });
    const slice = (from, to) => source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from)));
    require('./helpers/ordinary-page.cjs')(context);
    vm.runInContext(slice('let voiceEnabled', '// Initialization'), context);
    vm.runInContext(slice('function connectionStatusText', 'function showConnectionState'), context);
    vm.runInContext(slice('async function toggleRecording', 'function drawWaveform'), context);
    vm.runInContext(slice('let speechQueue', '// Appearance: theme'), context);
    vm.runInContext(slice('async function sendMessage', 'function pendingTurns'), context);
    vm.runInContext(slice('function pendingTurns', '// Message UI Helpers'), context);
    vm.runInContext(slice('function finalizeStreamingMessage', '// Workspace Tool Drawer'), context);
    vm.runInContext(fs.readFileSync('static/js/chat-controls.js', 'utf8'), context);
    context.ChatControls = context.window.ChatControls;
    context.showReplyNotice = (text, review) => {
        notices.push({text, review});
        document.getElementById('replyNotice')?.remove();
        const el = new Element(); el.id = 'replyNotice'; container.append(el);
    };
    vm.runInContext('voiceEnabled = true;', context);
    return {
        context, elements, document, requests, streams, recordings, sounds, notices, drafts, values, track, alerts,
        state: expression => vm.runInContext(expression, context),
        async begin() {elements.get('messageInput').value = 'First question'; const done = context.sendMessage({preventDefault() {}}); await tick(); return {done, stream: streams[0]};},
        async record() {await context.startRecording(); return recordings.at(-1);},
        stopCalls: () => requests.filter(item => item.url.endsWith('/stop')),
        chatCalls: () => requests.filter(item => item.url === '/api/chat/stream'),
    };
}

test('microphone setup failures release capture and describe the actual problem', async () => {
    const permission = deferred(), a = app({permission: permission.promise});
    const starting = a.context.startRecording();
    const capture = a.state('voiceCapture');
    const denied = new Error('synthetic device missing'); denied.name = 'NotFoundError';
    permission.reject(denied);
    await starting;
    let released = false; capture.done.then(() => { released = true; }); await tick();
    assert.equal(released, true);
    assert.match(a.alerts[0], /no microphone/i);
    assert.doesNotMatch(a.alerts[0], /allow mic permissions/i);
    assert.equal(a.state('voiceCapture'), null);

    const unsupported = app({unsupportedRecording: true});
    await unsupported.context.startRecording();
    assert.equal(unsupported.track.stopped, true);
    assert.match(unsupported.alerts[0], /recording format/i);
    assert.doesNotMatch(unsupported.alerts[0], /allow mic permissions/i);
});

test('microphone permission denial is explained', async () => {
    const blocked = new Error('synthetic denial'); blocked.name = 'NotAllowedError';
    const denied = app({permission: Promise.reject(blocked)});
    await denied.context.startRecording();
    assert.match(denied.alerts[0], /microphone permission/i);
});

test('chat start schedules the backchannel and both reply text and microphone stop it', async () => {
    const permission = deferred(), a = app({permission: permission.promise}), events = [];
    a.context.AlteraBackchannel = {schedule: turn => events.push(['schedule', turn.id]), stop: () => events.push(['stop'])};
    const turn = await a.begin();
    assert.equal(events.filter(event => event[0] === 'schedule').length, 1);
    const beforeText = events.length; turn.stream.push({response: 'A partial reply'}); await tick();
    assert.ok(events.slice(beforeText).some(event => event[0] === 'stop'));
    const beforeMic = events.length, recording = a.context.startRecording();
    assert.ok(events.slice(beforeMic).some(event => event[0] === 'stop'));
    permission.resolve({getTracks: () => [{stop() {}}]}); await recording;
    turn.stream.push({status: 'interrupted', session_id: 1}); turn.stream.end();
    await turn.done;
});

test('mic click silences current and queued speech before microphone permission resolves; late clips stay silent', async () => {
    const permission = deferred();
    const a = app({permission: permission.promise});
    const turn = await a.begin();
    turn.stream.push({status: 'accepted', session_id: 1});
    turn.stream.push({response: 'A partial reply'});
    turn.stream.push({status: 'audio', audio_url: '/first.wav'});
    turn.stream.push({status: 'audio', audio_url: '/queued.wav'});
    await tick();
    assert.equal(a.sounds[0].plays, 1);
    const starting = a.context.startRecording();
    assert.equal(a.sounds[0].pauses, 1);
    assert.equal(a.state('speechQueue.length'), 0);
    assert.equal(a.recordings.length, 0);
    turn.stream.push({status: 'audio', audio_url: '/late.wav'});
    await tick();
    assert.equal(a.sounds.reduce((sum, audio) => sum + audio.plays, 0), 1);
    permission.resolve({getTracks: () => []});
    await starting;
    turn.stream.push({status: 'interrupted', session_id: 1, error: 'Stopped', user_message_id: 1});
    turn.stream.end(); await turn.done;
    assert.equal(a.state('isRecording'), true);
    assert.equal(a.elements.get('statusText').textContent, 'Listening...');
});

test('mic before accepted queues one idempotent stop and begins recording immediately', async () => {
    const a = app(); const turn = await a.begin();
    assert.equal(a.elements.get('stopReplyBtn').disabled, false, 'Stop is usable before accepted');
    const recorder = await a.record();
    const first = a.context.ChatControls.stop();
    const again = a.context.ChatControls.stop();
    assert.equal(first, again);
    assert.equal(a.stopCalls().length, 0);
    assert.equal(a.recordings[0].started, true);
    assert.equal(a.elements.get('stopReplyBtn').disabled, true);
    turn.stream.push({status: 'accepted', session_id: 1});
    await tick(); await first;
    assert.equal(a.stopCalls().length, 1);
    assert.equal(a.stopCalls()[0].url, `/api/chat/requests/${a.chatCalls()[0].payload.request_id}/stop`);
    turn.stream.push({status: 'interrupted', session_id: 1, error: 'Stopped'});
    turn.stream.end(); await turn.done;
});

test('transcription waits for terminal settlement and then sends once in its captured conversation', async () => {
    const a = app({autoVoiceReply: true}); const turn = await a.begin();
    turn.stream.push({status: 'accepted', session_id: 1});
    turn.stream.push({response: 'Keep this partial answer'});
    await tick(); const partial = a.document.getElementById('streamingMsg');
    const recorder = await a.record(); a.context.stopRecording(); await tick();
    assert.equal(a.chatCalls().length, 1);
    assert.equal(a.state('isStreaming'), true);
    assert.equal(JSON.parse(a.values.get('altera_restored_drafts'))[0].message, 'My new thought', 'a waiting transcript survives reload');
    turn.stream.push({status: 'interrupted', session_id: 1, error: 'Stopped', request_id: a.chatCalls()[0].payload.request_id, user_message_id: 1});
    await tick();
    assert.equal(a.chatCalls().length, 1, 'terminal event alone must not overlap the old reader');
    turn.stream.end(); await turn.done; await recorder.completion;
    assert.equal(a.chatCalls().length, 2);
    assert.equal(a.chatCalls()[1].payload.session_id, 1);
    assert.equal(a.chatCalls()[1].payload.dataset_epoch, 'dataset-one');
    assert.equal(a.chatCalls()[1].payload.message, 'My new thought');
    assert.equal(partial.content.textContent, 'Keep this partial answer');
    assert.equal(partial.dataset.replyState, 'interrupted');
    assert.equal(partial.removed, undefined);
    assert.equal(a.track.stopped, true);
    assert.equal(a.values.has('altera_restored_drafts'), false, 'the durable next request replaces its waiting transcript');
});

test('failed transcription explains how to retry without sending and releases the microphone', async () => {
    const a = app({sttResponse: {ok: false, status: 503, json: async () => ({error: 'synthetic private server detail'})}});
    const recorder = await a.record();
    a.context.stopRecording(); await recorder.completion;
    assert.equal(a.chatCalls().length, 0);
    assert.equal(a.track.stopped, true);
    assert.equal(a.elements.get('micBtn').disabled, false);
    assert.match(a.notices.at(-1).text, /speech model/i);
    assert.match(a.notices.at(-1).text, /record.*again/i);
    assert.doesNotMatch(a.notices.at(-1).text, /synthetic private server detail|STT failed: 503/);
});

test('a failed chat recording is retained only after an explicit choice, with its original context', async () => {
    const response = {ok: false, status: 503, json: async () => ({})};
    const a = app({sttResponse: response, keepFailedRecording: true}), retained = [];
    a.context.VoiceNotes = {retainRecording: async (blob, context) => { retained.push({blob, context}); return true; }};
    const recorder = await a.record();
    a.context.currentSessionId = 2;
    a.context.stopRecording(); await recorder.completion;
    assert.equal(retained.length, 1);
    assert.equal(await retained[0].blob.text(), 'recorded speech');
    assert.equal(retained[0].context.sessionId, 1);
    assert.equal(retained[0].context.datasetEpoch, 'dataset-one');
    assert.equal(retained[0].context.companionId, 10);
    assert.equal(a.chatCalls().length, 0);
    assert.match(a.notices.at(-1).text, /saved.*voice notes/i);

    const declined = app({sttResponse: response}), unwanted = [];
    declined.context.VoiceNotes = {retainRecording: async (...args) => { unwanted.push(args); return true; }};
    const declinedRecorder = await declined.record();
    declined.context.stopRecording(); await declinedRecorder.completion;
    assert.equal(unwanted.length, 0);
    assert.equal(declined.chatCalls().length, 0);
});

test('a recording that could not be saved remains available for review in voice notes', async () => {
    const a = app({sttResponse: {ok: false, status: 500}, keepFailedRecording: true});
    a.context.VoiceNotes = {retainRecording: async () => false};
    const recorder = await a.record(); a.context.stopRecording(); await recorder.completion;
    assert.match(a.notices.at(-1).text, /not saved/i);
    assert.match(a.notices.at(-1).text, /voice notes/i);
    assert.equal(a.chatCalls().length, 0);
});

test('a successfully transcribed recording is not offered as failed audio when its context changes', async () => {
    const a = app({keepFailedRecording: true}), retained = [];
    a.context.VoiceNotes = {retainRecording: async (...args) => { retained.push(args); return true; }};
    const recorder = await a.record();
    a.context.currentSessionId = 2;
    a.context.stopRecording(); await recorder.completion;
    assert.equal(retained.length, 0);
    assert.equal(a.drafts[0].message, 'My new thought');
    assert.equal(a.chatCalls().length, 0);
});

test('an empty transcription can be explicitly saved for review', async () => {
    const a = app({sttResponse: {ok: true, json: async () => ({text: ''})}, keepFailedRecording: true}), retained = [];
    a.context.VoiceNotes = {retainRecording: async blob => { retained.push(blob); return true; }};
    const recorder = await a.record(); a.context.stopRecording(); await recorder.completion;
    assert.equal(retained.length, 1);
    assert.equal(await retained[0].text(), 'recorded speech');
    assert.equal(a.chatCalls().length, 0);
});

for (const [status, code, guidance] of [[413, '', /shorter message/i], [409, 'dataset_changed', /reload Altera/i]]) {
    test(`transcription HTTP ${status} gives relevant recovery guidance`, async () => {
        const a = app({sttResponse: {ok: false, status, json: async () => ({code})}});
        const recorder = await a.record();
        a.context.stopRecording(); await recorder.completion;
        assert.equal(a.chatCalls().length, 0);
        assert.equal(a.elements.get('micBtn').disabled, false);
        assert.match(a.notices.at(-1).text, guidance);
    });
}

test('a busy Altera is not misreported as a missing speech model', async () => {
    const a = app({sttResponse: {ok: false, status: 503, json: async () => ({code: 'state_busy', error: 'private detail'})}});
    const recorder = await a.record();
    a.context.stopRecording(); await recorder.completion;
    assert.match(a.notices.at(-1).text, /Altera is busy/i);
    assert.doesNotMatch(a.notices.at(-1).text, /speech model|private detail/i);
    assert.equal(a.chatCalls().length, 0);
    assert.equal(a.elements.get('micBtn').disabled, false);
});

test('saved text is preserved while mic silences audio still draining after saved', async () => {
    const a = app(); const turn = await a.begin();
    turn.stream.push({status: 'accepted', session_id: 1});
    turn.stream.push({response: 'The complete answer'});
    turn.stream.push({status: 'audio', audio_url: '/first.wav'});
    turn.stream.push({status: 'saved', session_id: 1, message_id: 2, user_message_id: 1});
    await tick();
    const saved = a.state('speechMsgEl');
    const recorder = await a.record();
    turn.stream.push({status: 'audio', audio_url: '/late-after-saved.wav'});
    await tick(); turn.stream.end(); await turn.done;
    assert.equal(saved.dataset.replyState, 'saved');
    assert.equal(saved.content.textContent, 'The complete answer');
    assert.equal(a.sounds.reduce((sum, audio) => sum + audio.plays, 0), 1);
    assert.equal(a.sounds[0].pauses, 1);
    assert.equal(a.stopCalls().length, 1, 'Stop cancels the saved reply\'s remaining server audio drain');
    const replay = saved.querySelector('.audio-play-btn');
    replay.onclick();
    assert.equal(a.sounds.reduce((sum, audio) => sum + audio.plays, 0), 1, 'Replay waits until recording stops');
    a.elements.get('messageInput').value = 'Keep this typed draft';
    a.context.stopRecording(); await recorder.completion;
    replay.onclick();
    assert.equal(a.sounds.at(-2).plays, 1, 'an explicit Replay click still starts the saved clips');
});

test('a rejected cancellation keeps the transcript for review without sending another request', async () => {
    const a = app({stopResponse: {ok: false, json: async () => ({error: 'Stop unavailable'})}});
    const turn = await a.begin();
    turn.stream.push({status: 'accepted', session_id: 1}); await tick();
    const recorder = await a.record(); a.context.stopRecording(); await recorder.completion;
    assert.equal(a.chatCalls().length, 1);
    assert.equal(a.drafts.length, 1);
    assert.equal(a.drafts[0].message, 'My new thought');
    assert.equal(a.state('isStreaming'), true, 'STT completion must not clear the older stream busy state');
    turn.stream.push({status: 'interrupted', session_id: 1, error: 'Stopped'}); turn.stream.end(); await turn.done;
});

test('a lost stream retains its durable request and the new transcript separately', async () => {
    const a = app(); const turn = await a.begin();
    turn.stream.push({status: 'accepted', session_id: 1}); turn.stream.push({response: 'Partial'}); await tick();
    const recorder = await a.record(); a.context.stopRecording(); await tick();
    turn.stream.end(); await turn.done; await recorder.completion;
    assert.equal(a.chatCalls().length, 1);
    assert.equal(a.drafts[0].message, 'My new thought');
    const pending = JSON.parse(a.values.get('altera_pending_turns'));
    assert.equal(pending[a.chatCalls()[0].payload.request_id].message, 'First question');
});

for (const change of ['conversation', 'dataset', 'companion']) {
    test(`${change} changes during transcription keep the original transcript unsent`, async () => {
        const transcription = deferred();
        const a = app({sttResponse: transcription.promise});
        const recorder = await a.record(); a.context.stopRecording();
        if (change === 'conversation') a.context.currentSessionId = 2;
        if (change === 'dataset') a.context.AlteraAPI = {epoch: 'dataset-two'};
        if (change === 'companion') a.context.Companion.active = {id: 11};
        transcription.resolve({ok: true, json: async () => ({text: 'Keep my original context'})});
        await recorder.completion;
        assert.equal(a.chatCalls().length, 0);
        assert.equal(a.drafts[0].session_id, 1);
        assert.equal(a.drafts[0].message, 'Keep my original context');
        assert.equal(a.elements.get('messageInput').value, '');
    });
}

test('transcription never replaces text already typed in the composer', async () => {
    const a = app(); const recorder = await a.record();
    a.elements.get('messageInput').value = 'A typed draft';
    a.context.stopRecording(); await recorder.completion;
    assert.equal(a.chatCalls().length, 0);
    assert.equal(a.elements.get('messageInput').value, 'A typed draft');
    assert.equal(a.drafts[0].message, 'My new thought');
});

test('review queue failure leaves a reload-safe and visible transcript fallback', async () => {
    const a = app({draftFailure: true}); const recorder = await a.record();
    a.context.currentSessionId = 2;
    a.context.stopRecording(); await recorder.completion;
    assert.equal(a.chatCalls().length, 0);
    assert.equal(JSON.parse(a.values.get('altera_restored_drafts'))[0].session_id, 1);
    assert.equal(a.document.getElementById('replyNotice').children[0].value, 'My new thought');
});

test('a delayed fallback TTS response cannot restart speech after a mic click', async () => {
    const fallback = deferred(); const a = app({ttsResponse: fallback.promise}); const turn = await a.begin();
    turn.stream.push({status: 'accepted', session_id: 1}); turn.stream.push({response: 'A complete answer'});
    turn.stream.push({status: 'saved', session_id: 1, message_id: 2}); await tick();
    await a.record();
    fallback.resolve({ok: true, json: async () => ({audio_url: '/fallback.wav'})});
    await tick(); turn.stream.end(); await turn.done;
    assert.equal(a.sounds.length, 0);
});

test('a check-in without a request ID stays silent when its TTS finishes after recording', async () => {
    const fallback = deferred(); const a = app({ttsResponse: fallback.promise});
    const pending = a.context.fallbackTTS('A proactive check-in');
    const recorder = await a.record();
    a.context.currentSessionId = 2;
    a.context.stopRecording(); await recorder.completion;
    fallback.resolve({ok: true, json: async () => ({audio_url: '/check-in.wav'})});
    await pending;
    assert.equal(a.sounds.length, 0);
});

for (const status of ['interrupted', 'failed']) {
    test(`voice input survives ${status} before its user message was saved`, async () => {
        const a = app(); const recorder = await a.record();
        a.context.stopRecording(); await tick();
        assert.equal(a.chatCalls().length, 1);
        a.streams[0].push({status: 'accepted', session_id: 1});
        a.streams[0].push({status, session_id: 1, user_message_id: null, error: 'Not saved yet'});
        a.streams[0].end(); await recorder.completion;
        const pending = JSON.parse(a.values.get('altera_pending_turns') || '{}');
        assert.equal(Object.values(pending)[0].message, 'My new thought');
        assert.equal(a.chatCalls().length, 1, 'the unsaved input is retained for review, never automatically resent');
    });
}

test('failed acceptance releases a queued early stop and preserves the voice transcript', async () => {
    const a = app(); const turn = await a.begin();
    const recorder = await a.record(); a.context.stopRecording(); await tick();
    turn.stream.end(); await turn.done; await recorder.completion;
    assert.equal(a.stopCalls().length, 0);
    assert.equal(a.chatCalls().length, 1);
    assert.equal(a.drafts[0].message, 'My new thought');
});

test('Stop can be retried by durable key after a previously stopped reader disconnects', async () => {
    const a = app(); const turn = await a.begin();
    turn.stream.push({status: 'accepted', session_id: 1}); await tick();
    await a.context.ChatControls.stop();
    turn.stream.end(); await turn.done;
    await a.context.ChatControls.stop();
    assert.equal(a.stopCalls().length, 2);
    assert.equal(a.stopCalls()[0].url, a.stopCalls()[1].url);
});

test('a new chat accepted after recording starts uses its resolved original session', async () => {
    const a = app({autoVoiceReply: true});
    a.context.currentSessionId = null;
    const turn = await a.begin(); const recorder = await a.record();
    a.context.stopRecording(); await tick();
    turn.stream.push({status: 'accepted', session_id: 1});
    turn.stream.push({status: 'interrupted', session_id: 1, error: 'Stopped', user_message_id: 1});
    turn.stream.end(); await turn.done; await recorder.completion;
    assert.equal(a.chatCalls().length, 2);
    assert.equal(a.chatCalls()[1].payload.session_id, 1);
});

test('the reply to a voice message can be interrupted by the next recording', async () => {
    const a = app(); const firstRecording = await a.record();
    a.context.stopRecording(); await tick();
    assert.equal(a.chatCalls().length, 1);
    assert.equal(a.elements.get('micBtn').disabled, false);
    assert.equal(a.elements.get('statusText').textContent, 'Thinking...');
    const stream = a.streams[0];
    stream.push({status: 'accepted', session_id: 1});
    stream.push({response: 'The first voice reply'});
    stream.push({status: 'audio', audio_url: '/voice-answer.wav'});
    await tick();
    const secondRecording = await a.record();
    assert.equal(secondRecording.started, true);
    assert.equal(a.sounds[0].pauses, 1);
    stream.push({status: 'interrupted', session_id: 1, error: 'Stopped'});
    stream.end(); await firstRecording.completion;
    assert.equal(a.state('isRecording'), true);
    assert.equal(a.state('voiceCapture.recorder') === secondRecording, true, 'the older STT callback cannot erase the new capture');
    assert.equal(a.elements.get('statusText').textContent, 'Listening...');
});

test('when all browser storage fails the transcript stays visible with an explicit copy-before-closing notice', async () => {
    const a = app({draftFailure: true, sessionStorageFailure: true});
    const recorder = await a.record(); a.context.currentSessionId = 2;
    a.context.stopRecording(); await recorder.completion;
    assert.equal(a.chatCalls().length, 0);
    assert.equal(a.values.has('altera_restored_drafts'), false);
    assert.match(a.notices.at(-1).text, /could not be saved.*Copy the text below before closing/);
    assert.equal(a.document.getElementById('replyNotice').children[0].value, 'My new thought');
});
