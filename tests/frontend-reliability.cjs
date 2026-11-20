const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

function reader(events, options = {}) {
    const source = fs.readFileSync('static/js/app.js', 'utf8');
    const start = source.indexOf('function pendingTurns(');
    const end = source.indexOf('// Message UI Helpers', start);
    const values = new Map();
    const seen = { text: [], saved: [], payloads: [], audio: [] };
    const wire = events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('');
    const bytes = new TextEncoder().encode(wire);
    const context = vm.createContext({
        crypto: webcrypto, TextDecoder, currentSessionId: 1,
        sessionStorage: {getItem: k => values.get(k), setItem: (k, v) => values.set(k, v), removeItem: k => values.delete(k)},
        document: {getElementById: () => null},
        fetch: async (url, request) => {
            seen.payloads.push(JSON.parse(request.body));
            return { ok: true, body: new ReadableStream({start(controller) {
                // Split UTF-8 and JSON at every possible byte boundary.
                for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
                controller.close();
            }}) };
        },
        updateStreamingMessage: text => seen.text.push(text),
        finalizeStreamingMessage: text => seen.saved.push(text),
        updateTypingIndicator() {}, hideTypingIndicator() {}, loadSessions() {}, stopSpeech() {}, queueSpeech: url => seen.audio.push(url),
        ...options,
    });
    require('./helpers/ordinary-page.cjs')(context);
    vm.runInContext(source.slice(start, end), context);
    return { run: payload => context.runChatStream(payload), seen, values, context };
}

test('saved requires an explicit persistence acknowledgement and preserves fragmented UTF-8', async () => {
    const r = reader([{response: 'Hi 🌻'}, {status: 'saved', session_id: 1, message_id: 2}]);
    assert.equal(await r.run({message: 'Hello', request_id: 'stable-request-123'}), 'Hi 🌻');
    assert.deepEqual(r.seen.saved, ['Hi 🌻']);
    assert.equal(r.values.size, 0);
    assert.equal(r.seen.payloads[0].request_id, 'stable-request-123');
});

test('a server failure escapes the parser and never finalizes a draft', async () => {
    const r = reader([{response: 'Partial'}, {status: 'failed', error: 'Could not save', response: 'Partial'}]);
    await assert.rejects(r.run({message: 'Hi'}), /Could not save/);
    assert.deepEqual(r.seen.saved, []);
    assert.deepEqual(r.seen.text, ['Partial']);
});

test('a truncated connection keeps the request key available for reconnect', async () => {
    const r = reader([{response: 'Partial'}]);
    await assert.rejects(r.run({message: 'Hi'}), /Connection interrupted/);
    assert.deepEqual(r.seen.saved, []);
    assert.equal(r.values.size, 1);
});

test('unfinished requests are kept separately for each companion and cannot be overwritten', async () => {
    const r = reader([{response: 'Partial'}]);
    await assert.rejects(r.run({session_id: 1, message: 'First', request_id: 'first-request-123'}), /Connection interrupted/);
    r.context.currentSessionId = 2;
    await assert.rejects(r.run({session_id: 2, message: 'Second', request_id: 'second-request-123'}), /Connection interrupted/);
    assert.equal(Object.keys(JSON.parse(r.values.get('altera_pending_turns'))).length, 2);
    await assert.rejects(r.run({session_id: 1, message: 'New'}), /Reconnect to retrieve/);
    assert.equal(r.seen.payloads.length, 2);
});

test('reconnecting uses the original key and does not speak the same clips twice', async () => {
    const r = reader([{response: 'Partial'}, {status: 'audio', audio_url: '/clip.wav'}]);
    const payload = {session_id: 1, message: 'First', request_id: 'first-request-123'};
    await assert.rejects(r.run(payload), /Connection interrupted/);
    await assert.rejects(r.run(payload), /Connection interrupted/);
    assert.deepEqual(r.seen.audio, ['/clip.wav']);
    assert.deepEqual(r.seen.payloads.map(p => p.request_id), ['first-request-123', 'first-request-123']);
});

test('a stale-tab rejection retains the unsent request for review after reload', async () => {
    const r = reader([], {AlteraAPI: {epoch: 'old-data'}, fetch: async () => ({ok: false, status: 409,
        json: async () => ({code: 'dataset_changed', error: 'Reload and review'})})});
    await assert.rejects(r.run({message: 'Unsent thought', session_id: 1, request_id: 'stale-request-123'}), /Reload and review/);
    assert.equal(JSON.parse(r.values.get('altera_pending_turns'))['stale-request-123'].message, 'Unsent thought');
});
