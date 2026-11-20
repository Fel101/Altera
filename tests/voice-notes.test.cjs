const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const source = fs.readFileSync('static/js/voice-notes.js', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const copy = value => structuredClone(value);
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject}; }

// Transactions publish their staged writes only on complete. Quota/abort tests
// therefore exercise the real module's persistence acknowledgment boundary.
function database() {
    const rows = new Map(), controls = {failWrites: false, failOpen: false, commits: 0};
    const db = {objectStoreNames: {contains: () => true}, transaction(name, mode) {
        let pending = 0, finished = false;
        const staged = new Map([...rows].map(([key, value]) => [key, copy(value)]));
        const tx = {error: null, abort() {
            if (finished) return; finished = true;
            queueMicrotask(() => tx.onabort?.());
        }};
        function request(fn) {
            const result = {}; pending++;
            queueMicrotask(() => {
                if (finished) return;
                try { result.result = fn(); result.onsuccess?.(); }
                catch (error) { result.error = error; tx.error = error; result.onerror?.(); tx.abort(); }
                pending--;
                queueMicrotask(() => {
                    if (finished || pending) return;
                    finished = true;
                    if (mode === 'readwrite') { rows.clear(); for (const [key, value] of staged) rows.set(key, copy(value)); controls.commits++; }
                    tx.oncomplete?.();
                });
            });
            return result;
        }
        const store = {
            getAll: () => request(() => [...staged.values()].map(copy)),
            put: row => request(() => {
                if (controls.failWrites) throw new Error('QuotaExceededError');
                staged.set(row.note_id, copy(row)); return row.note_id;
            }),
            delete: key => request(() => { if (controls.failWrites) throw new Error('QuotaExceededError'); staged.delete(key); }),
        };
        tx.objectStore = () => store;
        return tx;
    }};
    const indexedDB = {open(name) {
        assert.equal(name, 'altera-voice-notes');
        const request = {};
        queueMicrotask(() => {
            if (controls.failOpen) { request.error = new Error('Storage disabled'); request.onerror?.(); }
            else { request.result = db; request.onsuccess?.(); }
        });
        return request;
    }};
    return {indexedDB, rows, controls};
}

class Element {
    constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.value = ''; this.textContent = ''; this.hidden = false; this.disabled = false; this.open = false; }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    querySelectorAll(tag) { return this.children.filter(child => child.tagName === tag); }
    pause() { this.paused = true; }
    load() { this.loaded = true; }
    showModal() { this.open = true; }
    close() { this.open = false; for (const fn of this.listeners.close || []) fn(); }
}

function harness(options = {}) {
    const storage = options.storage || database();
    const elements = new Map();
    for (const suffix of ['Dialog', 'Close', 'Status', 'Record', 'Stop', 'Refresh', 'List', 'Detail', 'Context', 'InputAudio',
        'Download', 'Transcript', 'Upload', 'Transcribe', 'Save', 'Review', 'Send', 'Rebind', 'Delete', 'ReplyAudio']) elements.set('voiceNotes' + suffix, new Element());
    const requests = [], server = options.server || new Map(), recorders = [], tracks = [], sends = [], timers = new Map();
    const documentEvents = {}, windowEvents = {};
    let timerId = 0, uploads = 0;
    const page = {sessionId: 1, companionId: 'mara', datasetEpoch: 'epoch-one'};
    const savedContext = {session_id: 1, avatar_id: 'mara', dataset_epoch: 'epoch-one', source_revision: 3, recall_after: 0};
    class Recorder {
        static isTypeSupported(type) { return type === (options.mime || 'audio/webm;codecs=opus'); }
        constructor(stream, init) { this.stream = stream; this.mimeType = init.mimeType; this.state = 'inactive'; recorders.push(this); }
        start() { this.state = 'recording'; }
        stop() {
            this.state = 'inactive';
            queueMicrotask(() => { this.ondataavailable({data: new Blob(['original recorded audio'], {type: this.mimeType})}); this.completion = this.onstop(); });
        }
    }
    class Form {
        constructor() { this.values = new Map(); }
        append(key, value) { this.values.set(key, value); }
        get(key) { return this.values.get(key); }
    }
    const context = vm.createContext({
        document: {hidden: false, getElementById: id => elements.get(id), createElement: tag => new Element(tag),
            addEventListener(name, fn) { (documentEvents[name] ||= []).push(fn); }},
        indexedDB: storage.indexedDB, crypto: webcrypto, Blob, FormData: Form, AbortController,
        MediaRecorder: Recorder, URL: {createObjectURL: () => 'blob:private-local-note', revokeObjectURL() {}},
        navigator: {onLine: options.offline !== true, mediaDevices: {getUserMedia: async () => {
            if (options.permission) return options.permission.promise;
            const track = {stopped: false, stop() { this.stopped = true; }}; tracks.push(track);
            return {getTracks: () => [track]};
        }}},
        console, setTimeout(fn, delay) { const id = ++timerId; timers.set(id, {fn, delay}); return id; }, clearTimeout(id) { timers.delete(id); },
        confirm: () => options.confirm !== false,
        addEventListener(name, fn) { (windowEvents[name] ||= []).push(fn); },
        fetch: async (url, init = {}) => {
            requests.push({url, ...init});
            if (!context.navigator.onLine) throw new TypeError('Offline');
            if (init.signal?.aborted) throw new Error('Aborted');
            const method = init.method || 'GET';
            const reply = (data, status = 200) => ({ok: status < 400, status, json: async () => copy(data)});
            if (url.startsWith('/api/voice-notes/context?')) return reply({context: savedContext});
            if (url.startsWith('/api/voice-notes?')) {
                if (options.listUnavailable) return reply({error: 'Voice processing is not enabled for this host.'}, 501);
                if (options.pendingList) return new Promise((resolve, reject) => {
                    init.signal.addEventListener('abort', () => { const error = new Error('Aborted'); error.name = 'AbortError'; reject(error); });
                });
                return reply({notes: [...server.values()].filter(note => note.session_id === page.sessionId),});
            }
            if (url === '/api/voice-notes' && method === 'POST') {
                uploads++;
                const form = init.body, noteId = form.get('note_id'), blob = form.get('audio');
                const hash = Buffer.from(await webcrypto.subtle.digest('SHA-256', await blob.arrayBuffer())).toString('hex');
                const note = server.get(noteId) || {note_id: noteId, session_id: Number(form.get('session_id')), avatar_id: form.get('avatar_id'),
                    dataset_epoch: form.get('dataset_epoch'), source_revision: Number(form.get('source_revision')), recall_after: Number(form.get('recall_after')),
                    status: 'upload', upload_complete: 1, audio_sha256: options.badHash ? 'wrong' : hash, audio_bytes: blob.size,
                    transcript: '', revision: 0, reviewed_revision: -1, request_id: 'voice_' + noteId, reply_audio: []};
                server.set(noteId, note);
                if (options.loseFirstUpload && uploads === 1) throw new TypeError('Upload response lost');
                return reply({note}, 201);
            }
            const match = url.match(/^\/api\/voice-notes\/(vn_[0-9a-f]{32})(?:\/(transcribe|review))?(?:\?.*)?$/);
            if (!match) return reply({error: 'Unexpected endpoint'}, 404);
            const note = server.get(match[1]);
            if (!note) return reply({error: 'Missing note'}, 404);
            const body = init.body ? JSON.parse(init.body) : {};
            if (method === 'DELETE') { assert.equal(body.confirm, 'delete-voice-note'); server.delete(note.note_id); return reply({status: 'deleted'}); }
            if (match[2] === 'transcribe') { note.status = options.slowTranscription ? 'transcribing' : 'review'; note.transcript = 'Recognized words'; note.revision++; note.reviewed_revision = -1; }
            if (method === 'PUT') {
                if (body.revision !== note.revision) return reply({error: 'Revision changed'}, 409);
                note.transcript = body.transcript; note.revision++; note.reviewed_revision = -1; note.status = 'review';
            }
            if (match[2] === 'review') {
                if (body.revision !== note.revision) return reply({error: 'Revision changed'}, 409);
                note.reviewed_revision = note.revision; note.status = 'ready';
            }
            return reply({note});
        },
    });
    context.window = context;
    require('./helpers/ordinary-page.cjs')(context);
    vm.runInContext(source, context);
    context.VoiceNotes.configure({getContext: () => page, send: async payload => {
        sends.push(copy(payload));
        if (options.sendPending) await options.sendPending.promise;
        const note = server.get(payload.voice_note_id); note.status = options.loseSend ? 'sending' : 'sent';
        if (options.loseSend) throw new Error('Reply transport lost; retrieve the existing request');
    }});
    return {context, api: context.VoiceNotes, elements, storage, server, requests, recorders, tracks, sends, timers, page, savedContext,
        fireDocument(name, event = {}) { for (const listener of documentEvents[name] || []) listener(event); },
        fireWindow(name, event = {}) { for (const listener of windowEvents[name] || []) listener(event); },
        status: () => elements.get('voiceNotesStatus').textContent,
        async record() { await this.api.record(); await this.api.stop(); return this.api.state().notes.find(note => note.note_id === this.api.state().selectedId); },
        async ready() { await this.api.open(); const note = await this.record(); await this.api.upload(); await this.api.transcribe(); await this.api.review(); return note; },
    };
}

test('damaged server recording warns and keeps the browser original downloadable', async () => {
    const a = harness();
    await a.api.open();
    const note = await a.record();
    await a.api.upload();
    a.server.get(note.note_id).audio_warning = 'The saved recording is missing or damaged.';
    await a.api.refresh();
    assert.match(a.elements.get('voiceNotesContext').textContent, /missing or damaged/);
    assert.equal(a.elements.get('voiceNotesTranscribe').disabled, true);
    assert.equal(a.elements.get('voiceNotesDownload').hidden, false);
    assert.equal(a.elements.get('voiceNotesInputAudio').src, 'blob:private-local-note');
    assert.ok(a.storage.rows.get(note.note_id).blob);
});

test('damaged server-only recording does not expose a playback URL', async () => {
    const noteId = 'vn_' + 'f'.repeat(32);
    const server = new Map([[noteId, {note_id: noteId, session_id: 1, avatar_id: 'mara', dataset_epoch: 'epoch-one',
        source_revision: 3, recall_after: 0, status: 'upload', upload_complete: 1, transcript: '', revision: 0,
        reviewed_revision: -1, request_id: 'voice_' + noteId, reply_audio: [], audio_warning: 'The saved recording is missing or damaged.'}]]);
    const a = harness({server});
    await a.api.open();
    assert.match(a.elements.get('voiceNotesContext').textContent, /missing or damaged/);
    assert.equal(a.elements.get('voiceNotesTranscribe').disabled, true);
    assert.equal(a.elements.get('voiceNotesInputAudio').src, undefined);
    assert.equal(a.elements.get('voiceNotesDownload').hidden, true);
});

test('interrupted WAV and unknown recordings download with truthful extensions', async () => {
    for (const [type, extension] of [['audio/wav', '.wav'], ['application/octet-stream', '.bin']]) {
        const a = harness();
        assert.equal(await a.api.retainRecording(new Blob(['original'], {type}),
            {sessionId: 1, companionId: 'mara', datasetEpoch: 'epoch-one'}), true);
        await a.api.open();
        assert.ok(a.elements.get('voiceNotesDownload').download.endsWith(extension));
    }
});

test('opening and starting a voice note stop any backchannel before microphone permission', async () => {
    const permission = deferred(), a = harness({permission}); let stops = 0;
    a.context.AlteraBackchannel = {stop() {stops++;}};
    await a.api.open(); assert.equal(stops, 1);
    const recording = a.api.record(); assert.equal(stops, 2);
    await tick(); assert.equal(a.recorders.length, 0);
    permission.resolve({getTracks: () => [{stop() {}}]}); await recording;
    await a.api.stop();
});

test('recording commits its blob and frozen context before Saved, survives reload, and never uploads or sends automatically', async () => {
    const a = harness(); await a.api.open(); const note = await a.record();
    assert.match(note.note_id, /^vn_[0-9a-f]{32}$/);
    assert.equal(a.storage.rows.size, 1); assert.equal(a.storage.rows.get(note.note_id).blob.size, note.blob.size);
    assert.equal(note.context.source_revision, 3); assert.equal(note.context.avatar_id, 'mara');
    assert.match(a.status(), /Recording saved in this browser/);
    assert.equal(a.requests.filter(r => r.method === 'POST').length, 0); assert.equal(a.sends.length, 0);
    assert.equal(a.tracks[0].stopped, true);
    const reload = harness({storage: a.storage, server: a.server, offline: true}); await reload.api.open();
    assert.equal(reload.api.state().notes.length, 1); assert.equal(reload.api.state().notes[0].blob.size, note.blob.size);
    assert.equal(reload.requests.length, 0); assert.equal(reload.sends.length, 0);
});

test('quota failure never promises persistence, exposes original audio, blocks closing, and allows explicit deletion', async () => {
    const a = harness(); await a.api.open(); a.storage.controls.failWrites = true;
    const note = await a.record();
    assert.equal(a.storage.rows.size, 0); assert.equal(note.storageError, true);
    assert.match(a.status(), /Recording NOT SAVED/); assert.equal(a.elements.get('voiceNotesDownload').hidden, false);
    assert.equal(await a.api.close(), false); assert.equal(a.elements.get('voiceNotesDialog').open, true);
    await a.api.remove(); assert.equal(a.api.state().notes.length, 0);
    await a.api.close(); assert.equal(a.elements.get('voiceNotesDialog').open, false);
});

test('offline recording without cached context requires explicit fresh-context approval and a new note ID', async () => {
    const a = harness({offline: true}); await a.api.open(); const original = await a.record();
    assert.equal(original.context.approved, false);
    a.context.navigator.onLine = true; await a.api.upload();
    assert.equal(a.requests.filter(r => r.url === '/api/voice-notes' && r.method === 'POST').length, 0);
    await a.api.approveContext(); const approved = a.api.state().notes.find(n => n.note_id === a.api.state().selectedId);
    assert.notEqual(approved.note_id, original.note_id); assert.equal(approved.context.approved, true);
    assert.equal(a.storage.rows.size, 2); assert.equal(original.context.approved, false);
    await a.api.upload(); assert.equal(a.server.size, 1); assert.equal(a.server.has(approved.note_id), true); assert.equal(a.sends.length, 0);
});

test('lost upload acknowledgment retries the same ID and original hash without creating a second server note', async () => {
    const a = harness({loseFirstUpload: true}); await a.api.open(); const note = await a.record();
    await a.api.upload(); assert.match(a.status(), /response lost/); assert.equal(note.blob.size > 0, true);
    await a.api.upload();
    const uploads = a.requests.filter(r => r.url === '/api/voice-notes' && r.method === 'POST');
    assert.equal(uploads.length, 2); assert.equal(uploads[0].body.get('note_id'), uploads[1].body.get('note_id'));
    assert.equal(uploads[0].body.get('avatar_id'), 'mara'); assert.equal(uploads[0].body.get('source_revision'), '3');
    assert.equal(uploads[0].body.get('recall_after'), '0'); assert.equal(a.server.size, 1);
    assert.equal(a.storage.rows.get(note.note_id).blob.size, note.blob.size); assert.equal(a.sends.length, 0);
});

test('a mismatched server hash never acknowledges or discards the browser original', async () => {
    const a = harness({badHash: true}); await a.api.open(); const note = await a.record(); await a.api.upload();
    assert.match(a.status(), /acknowledgment did not match/); assert.equal(note.server, undefined);
    assert.equal(a.storage.rows.get(note.note_id).blob.size, note.blob.size); assert.equal(a.sends.length, 0);
});

test('transcript edits invalidate review until saved and explicitly reviewed; double Send cannot overlap', async () => {
    const sending = deferred(), a = harness({sendPending: sending}); const note = await a.ready();
    const editor = a.elements.get('voiceNotesTranscript'); editor.value = '<edited words>';
    await a.api.send(); assert.equal(a.sends.length, 0); assert.match(a.status(), /Save and confirm/);
    await a.api.save(); assert.equal(note.server.revision, 2); assert.equal(note.server.reviewed_revision, -1);
    await a.api.review(); assert.equal(note.server.reviewed_revision, 2);
    const first = a.api.send(); await tick(); const second = a.api.send(); await tick();
    assert.equal(a.sends.length, 1); assert.equal(a.sends[0].message, '<edited words>');
    assert.equal(a.sends[0].request_id, 'voice_' + note.note_id); assert.equal(a.sends[0].reviewed_revision, 2);
    sending.resolve(); await first; await second;
    assert.equal(note.server.status, 'sent'); assert.equal(a.elements.get('voiceNotesReplyAudio').children.length, 0);
});

for (const changed of ['session', 'owner', 'epoch', 'revision', 'forget']) {
    test(`a changed ${changed} blocks upload using the captured context`, async () => {
        const a = harness(); await a.api.open(); const original = await a.record();
        if (changed === 'session') a.page.sessionId = 2;
        if (changed === 'owner') { a.page.companionId = 'ana'; a.savedContext.avatar_id = 'ana'; }
        if (changed === 'epoch') { a.page.datasetEpoch = 'restored'; a.savedContext.dataset_epoch = 'restored'; }
        if (changed === 'revision') a.savedContext.source_revision++;
        if (changed === 'forget') a.savedContext.recall_after++;
        await a.api.upload();
        assert.equal(a.server.size, 0); assert.equal(a.sends.length, 0);
        assert.equal(a.storage.rows.get(original.note_id).context.avatar_id, 'mara');
        assert.equal(a.storage.rows.get(original.note_id).context.source_revision, 3);
        assert.equal(a.storage.rows.get(original.note_id).context.recall_after, 0);
    });
}

test('closing while microphone permission is pending stops late tracks and never starts a recording', async () => {
    const permission = deferred(), a = harness({permission}); await a.api.open();
    const recording = a.api.record(); await tick(); await a.api.close();
    let stopped = false; permission.resolve({getTracks: () => [{stop() {stopped = true;}}]}); await recording;
    assert.equal(stopped, true); assert.equal(a.recorders.length, 0); assert.equal(a.api.state().recording, false);
    assert.equal(a.storage.rows.size, 0);
});

test('browser storage bounds reject an eleventh note before requesting microphone permission', async () => {
    const storage = database();
    for (let index = 0; index < 10; index++) storage.rows.set('vn_' + index.toString(16).padStart(32, '0'), {
        note_id: 'vn_' + index.toString(16).padStart(32, '0'), context: {session_id: 1, avatar_id: 'mara', dataset_epoch: 'epoch-one'},
        blob: new Blob(['saved']), localOrigin: true, localSaved: true,
    });
    const a = harness({storage}); await a.api.open(); await a.api.record();
    assert.equal(a.recorders.length, 0); assert.equal(a.tracks.length, 0); assert.match(a.status(), /storage is full/);
});

test('polling is limited to active jobs and all timers stop when the dialog closes', async () => {
    const a = harness({slowTranscription: true}); await a.api.open(); await a.record(); await a.api.upload(); await a.api.transcribe();
    assert.equal(a.timers.size, 1);
    const timer = [...a.timers.values()][0]; assert.equal(timer.delay, 500);
    await a.api.close(); assert.equal(a.timers.size, 0); assert.equal(a.sends.length, 0);
});

test('an explicitly retrieved failed transport uses the original reviewed revision and request ID', async () => {
    const a = harness({loseSend: true}); const note = await a.ready(); await a.api.send();
    assert.equal(a.sends.length, 1); assert.match(a.status(), /transport lost/);
    await a.api.refresh(); await a.api.send();
    assert.equal(a.sends.length, 2); assert.equal(a.sends[0].request_id, a.sends[1].request_id);
    assert.equal(a.sends[0].voice_note_id, note.note_id); assert.equal(a.sends[0].reviewed_revision, a.sends[1].reviewed_revision);
});

test('a supported alternate recording MIME is selected and replay audio never autoplays', async () => {
    const a = harness({mime: 'audio/mp4'}); const note = await a.ready();
    assert.equal(a.recorders[0].mimeType, 'audio/mp4'); assert.match(a.elements.get('voiceNotesDownload').download, /\.m4a$/);
    a.server.get(note.note_id).reply_audio = [{filename: 'reply-' + 'a'.repeat(64) + '.wav'}];
    await a.api.refresh(); const audio = a.elements.get('voiceNotesReplyAudio').children[0];
    assert.equal(audio.controls, true); assert.equal(audio.preload, 'none'); assert.equal(audio.autoplay, undefined);
    assert.match(audio.src, /^\/api\/voice-notes\/vn_[0-9a-f]{32}\/replies\//);
});

test('offline edits to a server-only note are committed locally and require a new server save and review', async () => {
    const origin = harness(); const saved = await origin.ready();
    const a = harness({server: origin.server}); await a.api.open();
    assert.equal(a.api.state().notes[0].localOrigin, false);
    a.context.navigator.onLine = false;
    a.elements.get('voiceNotesTranscript').value = 'An offline correction'; await a.api.save();
    assert.match(a.status(), /saved in this browser/); assert.equal(a.storage.rows.size, 1);
    assert.equal(a.server.get(saved.note_id).transcript, 'Recognized words');
    const reloaded = harness({storage: a.storage, server: a.server, offline: true}); await reloaded.api.open();
    assert.equal(reloaded.elements.get('voiceNotesTranscript').value, 'An offline correction');
    assert.equal(reloaded.elements.get('voiceNotesSend').disabled, true);
    reloaded.context.navigator.onLine = true; await reloaded.api.save();
    assert.equal(a.server.get(saved.note_id).transcript, 'An offline correction');
    assert.equal(a.server.get(saved.note_id).reviewed_revision, -1); assert.equal(reloaded.sends.length, 0);
});

function unloadWarning(app) {
    let prevented = false;
    const event = {preventDefault() { prevented = true; }};
    app.fireWindow('beforeunload', event);
    return {prevented, returnValue: event.returnValue};
}

test('hiding the page immediately stops active microphone tracks and commits the recording locally without uploading', async () => {
    const a = harness(); await a.api.open(); await a.api.record();
    assert.equal(a.api.state().recording, true); assert.equal(unloadWarning(a).prevented, true);
    a.context.document.hidden = true; a.fireDocument('visibilitychange');
    assert.equal(a.tracks[0].stopped, true); assert.equal(a.recorders[0].state, 'inactive');
    assert.doesNotMatch(a.status(), /Recording saved/);
    await a.api.stop();
    assert.equal(a.storage.rows.size, 1); assert.equal(a.api.state().recording, false);
    assert.match(a.status(), /Recording saved in this browser/); assert.equal(unloadWarning(a).prevented, false);
    assert.equal(a.requests.filter(request => request.method === 'POST').length, 0);
    assert.equal(a.sends.length, 0); assert.equal(a.timers.size, 0);
});

test('hiding during pending microphone permission invalidates the capture even after the page becomes visible again', async () => {
    const permission = deferred(), a = harness({permission}); await a.api.open();
    const recording = a.api.record(); await tick();
    assert.equal(a.api.state().busy, true); assert.equal(unloadWarning(a).prevented, false);
    a.context.document.hidden = true; a.fireDocument('visibilitychange');
    a.context.document.hidden = false; a.fireDocument('visibilitychange');
    let stopped = false; permission.resolve({getTracks: () => [{stop() { stopped = true; }}]}); await recording;
    assert.equal(stopped, true); assert.equal(a.recorders.length, 0); assert.equal(a.storage.rows.size, 0);
    assert.equal(a.requests.filter(request => request.method === 'POST').length, 0); assert.equal(a.sends.length, 0);
});

test('pagehide stops recording resources without claiming that unloading guarantees a completed save', async () => {
    const a = harness(); await a.api.open(); await a.api.record();
    assert.equal(a.timers.size, 1);
    a.fireWindow('pagehide');
    assert.equal(a.tracks[0].stopped, true); assert.equal(a.timers.size, 0);
    assert.match(a.status(), /may close before the recording finishes saving/);
    assert.equal(a.storage.rows.size, 0, 'a requested stop is not a persistence acknowledgment');
    await a.api.stop(); assert.equal(a.storage.rows.size, 1);
    assert.equal(a.requests.filter(request => request.method === 'POST').length, 0); assert.equal(a.sends.length, 0);
});

test('pagehide aborts pending fetch controllers and removes their request timeout timers', async () => {
    const a = harness({pendingList: true}); const opening = a.api.open(); await tick();
    const request = a.requests[0]; assert.equal(request.signal.aborted, false); assert.equal(a.timers.size, 1);
    a.fireWindow('pagehide');
    assert.equal(request.signal.aborted, true); assert.equal(a.timers.size, 0);
    await opening; assert.equal(a.timers.size, 0); assert.equal(a.sends.length, 0);
});

test('only an unsaved original keeps the unload warning, and explicit Delete removes it', async () => {
    const a = harness(); await a.api.open(); assert.equal(unloadWarning(a).prevented, false);
    a.storage.controls.failWrites = true; await a.record();
    assert.equal(unloadWarning(a).prevented, true); assert.equal(unloadWarning(a).returnValue, '');
    await a.api.close(); assert.equal(a.elements.get('voiceNotesDialog').open, true);
    await a.api.remove(); assert.equal(unloadWarning(a).prevented, false);
    await a.api.close(); assert.equal(a.elements.get('voiceNotesDialog').open, false);
});

test('an interrupted immediate recording remains local and unapproved when its save fails',async()=>{
    const a=harness();a.storage.controls.failWrites=true;
    const blob=new Blob(['original interrupted bytes'],{type:'audio/webm'});
    assert.equal(await a.api.retainRecording(blob,{sessionId:1,datasetEpoch:'epoch-one',companionId:''}),false);
    assert.equal(a.api.state().notes.length,1);assert.equal(a.api.state().notes[0].blob,blob);
    assert.equal(a.api.state().notes[0].context.approved,false);assert.equal(a.storage.rows.size,0);
    assert.equal(a.requests.length,0);assert.equal(a.sends.length,0);
});

test('chat failure recovery uses the voice-note store and cannot send from a changed conversation', async () => {
    const a = harness(), notices = [], chat = fs.readFileSync('static/js/app.js', 'utf8');
    const start = chat.indexOf('async function offerFailedVoiceRecording(');
    const end = chat.indexOf('async function startRecording(', start);
    assert.ok(start >= 0 && end > start);
    a.context.showReplyNotice = message => notices.push(message);
    vm.runInContext(chat.slice(start, end), a.context);
    const blob = new Blob(['synthetic failed chat recording'], {type: 'audio/webm'});
    a.page.sessionId = 2;
    await a.context.offerFailedVoiceRecording({sessionId: 1, companionId: 'mara', datasetEpoch: 'epoch-one'}, blob);
    assert.equal(a.storage.rows.size, 1);
    const note = [...a.storage.rows.values()][0];
    assert.equal(await note.blob.text(), 'synthetic failed chat recording');
    assert.equal(note.context.session_id, 1);
    assert.equal(note.context.approved, false);
    assert.match(notices.at(-1), /saved in this browser/i);
    assert.equal(a.requests.length, 0);
    await a.api.open(); await a.api.upload();
    assert.match(a.status(), /different or unapproved context/i);
    assert.equal(a.requests.filter(request => request.method === 'POST').length, 0);
    assert.equal(a.sends.length, 0);
});

test('chat failure recovery leaves a failed browser write visible and downloadable', async () => {
    const a = harness(), notices = [], chat = fs.readFileSync('static/js/app.js', 'utf8');
    const start = chat.indexOf('async function offerFailedVoiceRecording(');
    const end = chat.indexOf('async function startRecording(', start);
    assert.ok(start >= 0 && end > start);
    a.context.showReplyNotice = message => notices.push(message);
    vm.runInContext(chat.slice(start, end), a.context);
    a.storage.controls.failWrites = true;
    const blob = new Blob(['unsaved synthetic recording'], {type: 'audio/webm'});
    await a.context.offerFailedVoiceRecording({sessionId: 1, companionId: 'mara', datasetEpoch: 'epoch-one'}, blob);
    assert.equal(a.storage.rows.size, 0);
    assert.equal(a.api.state().notes[0].blob, blob);
    assert.match(notices.at(-1), /NOT SAVED/);
    const event = {preventDefault() { this.prevented = true; }};
    a.fireWindow('beforeunload', event);
    assert.equal(event.prevented, true);
    await a.api.open();
    assert.equal(a.elements.get('voiceNotesDownload').hidden, false);
    assert.equal(a.requests.filter(request => request.method === 'POST').length, 0);
});

test('a recording captured before a conversation exists keeps its original for explicit context review',async()=>{
    const a=harness();const blob=new Blob(['orphan original'],{type:'audio/webm'});
    assert.equal(await a.api.retainRecording(blob,{}),true);
    const note=[...a.storage.rows.values()][0];assert.equal(note.context.session_id,null);assert.equal(note.context.approved,false);
    assert.equal(await note.blob.text(),'orphan original');assert.equal(a.requests.length,0);
});

test('saved local originals render even when the server gates the voice list',async()=>{
    const a=harness({listUnavailable:true}),noteId='vn_'+'4'.repeat(32);
    a.storage.rows.set(noteId,{note_id:noteId,localOrigin:true,localSaved:true,blob:new Blob(['Synthetic recovered audio'],{type:'audio/webm'}),
        context:{session_id:1,avatar_id:'mara',dataset_epoch:'epoch-one',source_revision:3,recall_after:0,approved:true}});
    await a.api.open();assert.equal(a.api.state().selectedId,noteId);
    assert.equal(a.elements.get('voiceNotesList').children.length,1);
    assert.match(a.elements.get('voiceNotesStatus').textContent,/not enabled/);
    assert.equal(a.sends.length,0);
});
