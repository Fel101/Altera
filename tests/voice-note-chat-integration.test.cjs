const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/app.js', 'utf8');
const start = source.indexOf('async function sendReviewedVoiceNote(');
const end = source.indexOf('function endAutomaticRecordingForContextChange(', start);

function chat(response) {
    const sent = [], appended = [], loaded = [], notices = [];
    const context = vm.createContext({currentSessionId: 3, AlteraAPI: {epoch: 'current'}, isStreaming: false,
        isRecording: false, recordingStarting: false, voiceTranscribing: false, pendingForSession: () => null,
        fetch: async () => response, appendMessage: (...args) => appended.push(args),
        runChatStream: async payload => {sent.push(payload); return 'Reply';},
        loadMessages: async sid => loaded.push(sid), updateSendButton() {}, showTypingIndicator() {}, hideTypingIndicator() {},
        showReplyNotice: text => notices.push(text)});
    require('./helpers/ordinary-page.cjs')(context);vm.runInContext(source.slice(start, end), context);
    const payload = {voice_note_id: 'vn_test', request_id: 'voice_vn_stable', session_id: 3, dataset_epoch: 'current', message: 'Reviewed text'};
    return {run: () => context.sendReviewedVoiceNote(payload), context, sent, appended, loaded, notices};
}

test('retrieving a saved voice reply reloads persisted messages without duplicate user bubbles or inference', async () => {
    const c = chat({ok: true, json: async () => ({session_id: 3, status: 'saved'})});
    await c.run();
    assert.deepEqual(c.loaded, [3]); assert.equal(c.appended.length, 0); assert.equal(c.sent.length, 0);
    assert.equal(c.context.isStreaming, false);
});

test('a new voice note appends once and submits its original durable request', async () => {
    const c = chat({ok: false, status: 404});
    await c.run();
    assert.deepEqual(c.appended, [['user', 'Reviewed text']]);
    assert.equal(c.sent[0].request_id, 'voice_vn_stable'); assert.equal(c.sent.length, 1);
});

test('reconnecting an active voice reply reuses its stream without appending the user again', async () => {
    const c = chat({ok: true, json: async () => ({session_id: 3, status: 'streaming'})});
    await c.run();
    assert.deepEqual(c.loaded, [3]); assert.equal(c.appended.length, 0);
    assert.equal(c.sent[0].request_id, 'voice_vn_stable');
});

test('an existing failed voice request remains inspectable without automatically submitting it again', async () => {
    const c = chat({ok: true, json: async () => ({session_id: 3, status: 'failed', error: 'Saved partial reply'})});
    await c.run();
    assert.equal(c.sent.length, 0); assert.equal(c.appended.length, 0); assert.deepEqual(c.notices, ['Saved partial reply']);
});

test('unavailable or foreign request state never creates another voice turn', async () => {
    for (const response of [{ok: false, status: 503}, {ok: true, json: async () => ({session_id: 99, status: 'saved'})}]) {
        const c = chat(response);
        await assert.rejects(c.run());
        assert.equal(c.sent.length, 0); assert.equal(c.appended.length, 0); assert.equal(c.context.isStreaming, false);
    }
});
