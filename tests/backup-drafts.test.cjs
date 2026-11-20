const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

test('backup draft reads fail explicitly when ordinary browser storage is unavailable', async () => {
    const source = fs.readFileSync('static/js/offline-queue.js', 'utf8');
    const context = vm.createContext({getDB: async () => { throw new Error('synthetic browser storage failure'); }, console: {error() {}}});
    vm.runInContext(source.slice(source.indexOf('async function getQueuedMessages('), source.indexOf('async function clearQueuedMessages(')), context);
    assert.equal((await context.getQueuedMessages(true)).length, 0);
    await assert.rejects(context.getQueuedMessages(true, {requireReadable: true}), /backup or restore was not started/);
});

test('backup draft reads wait for transaction completion and reject a late abort', async () => {
    const source = fs.readFileSync('static/js/offline-queue.js', 'utf8');
    let tx, request;
    const context = vm.createContext({STORE_NAME: 'pending-messages', getDB: async () => ({transaction() {
        request = {result: [{message: 'Synthetic retained draft', status: 'pending'}]};
        const store = {getAll: () => request, index: () => ({getAll: () => request})};
        tx = {objectStore: () => store}; return tx;
    }}), console: {error() {}}});
    vm.runInContext(source.slice(source.indexOf('async function getQueuedMessages('), source.indexOf('async function clearQueuedMessages(')), context);
    let done = false;
    const first = context.getQueuedMessages(true, {requireReadable: true}).then(value => { done = true; return value; });
    await new Promise(resolve => setImmediate(resolve)); request.onsuccess(); await Promise.resolve();
    assert.equal(done, false); tx.oncomplete(); assert.equal((await first)[0].message, 'Synthetic retained draft');
    const second = context.getQueuedMessages(true, {requireReadable: true});
    await new Promise(resolve => setImmediate(resolve)); request.onsuccess(); tx.onabort();
    await assert.rejects(second, /backup or restore was not started/);
});

test('only same-origin API mutations carry the page dataset identifier', async () => {
    const calls = [];
    const context = vm.createContext({URL, Headers, Request, __DATA_EPOCH__: 'page-epoch',
        location: {href: 'http://localhost:5001/', origin: 'http://localhost:5001'},
        fetch: async (input, init) => calls.push({input, init})});
    context.window = context;
    vm.runInContext(fs.readFileSync('static/js/api.js', 'utf8'), context);
    await context.fetch('/api/memory', {method: 'POST'});
    await context.fetch(new Request('http://localhost:5001/api/photos', {method: 'DELETE'}));
    await context.fetch('/api/status');
    await context.fetch('https://example.com/api/elsewhere', {method: 'POST'});
    assert.equal(calls[0].init.headers.get('X-Altera-Dataset'), 'page-epoch');
    assert.equal(calls[1].init.headers.get('X-Altera-Dataset'), 'page-epoch');
    assert.equal(calls[2].init.headers, undefined);
    assert.equal(calls[3].init.headers, undefined);
    assert.equal(Object.isFrozen(context.AlteraAPI), true);
});

test('restored and legacy queued drafts never auto-send into the new dataset', async () => {
    const source = fs.readFileSync('static/js/offline-queue.js', 'utf8');
    const items = [], sent = [];
    const db = {transaction() {
        const tx = {objectStore: () => ({add: row => items.push(row), put() {}})};
        queueMicrotask(() => tx.oncomplete?.());
        return tx;
    }};
    const context = vm.createContext({crypto: webcrypto, STORE_NAME: 'pending-messages', AlteraAPI: {epoch: 'restored-data'},
        getDB: async () => db, updateOfflineIndicator() {},
        getQueuedMessages: async () => items.filter(i => i.status === 'pending'),
        saveQueueItem: async () => {}, fetch: async (...args) => sent.push(args),
        showNotification() {}, console});
    vm.runInContext(source.slice(source.indexOf('async function restoreBrowserDrafts('), source.indexOf('async function reviewQueuedMessages(')), context);
    vm.runInContext(source.slice(source.indexOf('let retryTimeout ='), source.indexOf('// Schedule periodic retry')), context);
    await context.restoreBrowserDrafts([{session_id: 1, message: 'Restored draft'}]);
    items.push({session_id: 1, message: 'Older queued draft', status: 'pending', dataset_epoch: 'old-data'});
    items.push({session_id: 1, message: 'Legacy queued draft', status: 'pending'});
    await context.retryQueuedMessages();
    assert.equal(sent.length, 0);
    assert.deepEqual(items.map(i => i.status), ['needs_review', 'needs_review', 'needs_review']);
});

test('navigation never replays a cached page from before restore', async () => {
    const events = {}, fetched = [];
    const context = vm.createContext({URL, Response,
        self: {location: {origin: 'http://localhost:5001'}, addEventListener: (name, fn) => {events[name] = fn;}},
        Request: class {constructor(request, options) {Object.assign(this, request, options);}},
        fetch: async request => {fetched.push(request); return new Response('Fresh page');},
        caches: {match() {throw new Error('Personalized HTML must not come from a cache');}}});
    vm.runInContext(fs.readFileSync('static/sw.js', 'utf8'), context);
    let response;
    events.fetch({request: {method: 'GET', mode: 'navigate', url: 'http://localhost:5001/'}, respondWith: p => {response = p;}});
    assert.equal(await (await response).text(), 'Fresh page');
    assert.equal(fetched[0].cache, 'no-store');
});

test('private generated audio bypasses both browser and service worker caches', async () => {
    const events = {}, fetched = [];
    const context = vm.createContext({URL, Response,
        self: {location: {origin: 'http://localhost:5001'}, addEventListener: (name, fn) => {events[name] = fn;}},
        Request: class {constructor(request, options) {Object.assign(this, request, options);}},
        fetch: async request => {fetched.push(request); return new Response('Private audio');},
        caches: {match() {throw new Error('Speech must not be read from a cache');}, open() {throw new Error('Speech must not be cached');}}});
    vm.runInContext(fs.readFileSync('static/sw.js', 'utf8'), context);
    let response;
    events.fetch({request: {method: 'GET', mode: 'cors', url: 'http://localhost:5001/static/audio/tts/private.wav'}, respondWith: p => {response = p;}});
    assert.equal(await (await response).text(), 'Private audio');
    assert.equal(fetched[0].cache, 'no-store');
});
