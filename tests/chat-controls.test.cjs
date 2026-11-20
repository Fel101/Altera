const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.isConnected = true; }
    setAttribute() {}
    addEventListener(name, fn) { this.listeners[name] = fn; }
    append(...items) { this.children.push(...items); }
    querySelector(selector) {
        if (selector === '.message-time') return {after: tools => {this.tools = tools;}};
        if (selector === '.message-content') return {textContent: 'Draft text'};
        return null;
    }
}

function controls() {
    const copied = [], requests = [];
    const context = vm.createContext({window: {addEventListener() {}}, isStreaming: false, currentSessionId: 1, AbortController,
        document: {getElementById: () => null, querySelectorAll: () => [], createElement: tag => new Element(tag)},
        navigator: {clipboard: {writeText: async text => copied.push(text)}},
        setTimeout() {}, stopSpeech() {}, showReplyNotice() {},
        fetch: async (url, options) => {requests.push({url, options}); return {ok: true, json: async () => ({status: 'stopping'})};},
    });
    require('./helpers/ordinary-page.cjs')(context);
    vm.runInContext(fs.readFileSync('static/js/chat-controls.js', 'utf8'), context);
    return {api: context.window.ChatControls, copied, requests};
}

test('Copy writes reply text without internal picture markers', async () => {
    const c = controls();
    const element = new Element();
    c.api.addTools(element, {id: 7, role: 'assistant', content: 'A reply\n[[photo:p-0123456789]]'});
    await element.tools.children.find(button => button.textContent === 'Copy').listeners.click();
    assert.deepEqual(c.copied, ['A reply']);
});

test('Stop targets the active request and repeated clicks do not send duplicate stop calls', async () => {
    const c = controls();
    c.api.begin('owned-request-123');
    c.api.accepted();
    await c.api.stop();
    await c.api.stop();
    assert.equal(c.requests.length, 1);
    assert.equal(c.requests[0].url, '/api/chat/requests/owned-request-123/stop');
    assert.equal(c.requests[0].options.method, 'POST');
});
