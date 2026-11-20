const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/backups.js', 'utf8');
const template = fs.readFileSync('templates/backup_dialog.html', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve; const promise = new Promise(yes => { resolve = yes; }); return {promise, resolve}; }
class Element {
    constructor(tag, document) { this.tagName = tag.toUpperCase(); this.document = document; this.children = []; this.listeners = {}; this.dataset = {}; this._value = ''; this.textContent = ''; this.checked = false; this.disabled = false; this.hidden = false; this.open = false; this.files = []; }
    get value() { return this._value; }
    set value(value) { this._value = value; if (this.type === 'file' && !value) this.files = []; }
    set innerHTML(_) { throw new Error('Backup responses must be rendered as text'); }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; this.textContent = ''; }
    removeAttribute(name) { if (name === 'href') delete this.href; }
    focus() { if (!this.disabled) this.document.activeElement = this; }
    remove() { this.removed = true; }
    showModal() { this.open = true; }
    close() { this.open = false; return this.fire('close'); }
    click() { this.document.clicks.push(this); return this.fire('click'); }
    async fire(name) { const event = {target: this, prevented: false, preventDefault() { this.prevented = true; }}; for (const callback of this.listeners[name] || []) await callback(event); return event; }
}
const text = node => [node.textContent, ...node.children.map(text)].join('\n');
class Form { constructor() { this.items = []; } append(key, value) { this.items.push([key, value]); } get(key) { return this.items.find(item => item[0] === key)?.[1]; } }
function app(options = {}) {
    const elements = new Map(), events = {}, calls = [], writes = [], logs = [], urls = [], downloads = [], revocations = [], redirects = [], tabValues = new Map();
    const document = {activeElement: null, clicks: [], getElementById: id => elements.get(id), createElement: tag => new Element(tag, document), addEventListener: (name, callback) => {events[name] = callback;}};
    document.body = new Element('body', document); const opener = document.createElement('button'); document.activeElement = opener;
    for (const match of template.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
        const node = new Element(match[1], document), attrs = match[2];
        node.checked = /\bchecked\b/.test(attrs); node.hidden = /\bhidden\b/.test(attrs); node.disabled = /\bdisabled\b/.test(attrs);
        node.type = attrs.match(/\btype="([^"]+)"/)?.[1]; elements.set(match[3], node);
    }
    elements.get('restoreUnlockMethod').value = 'password';
    const entries = options.entries || [{id: 'encrypted.alterabak', encrypted: true, counts: {}, created_at: '2026-10-28T12:00:00Z', bytes: 1200, download_url: '/api/backups/encrypted.alterabak/download'},
        {id: 'plain.zip', encrypted: false, counts: {messages: 2}, created_at: '2026-10-28T11:00:00Z', bytes: 900, download_url: '/api/backups/plain.zip/download'}];
    const response = (data, status = 200) => ({ok: status < 400, status, json: async () => structuredClone(data)});
    const context = vm.createContext({document, AbortController, TextEncoder, Blob, Date,
        FormData: Form, AlteraAPI: {epoch: 'epoch-a'}, currentSessionId: 1,
        localStorage: {getItem: () => null, setItem: (key, value) => writes.push({store: 'local', key, value})},
        sessionStorage: {getItem: key => tabValues.has(key) ? tabValues.get(key) : null,
            setItem: (key, value) => { tabValues.set(key, value); writes.push({store: 'session', key, value}); },
            removeItem: key => { tabValues.delete(key); writes.push({store: 'session', key, removed: true}); }},
        console: {log: (...args) => logs.push(args), warn: (...args) => logs.push(args), error: (...args) => logs.push(args)},
        navigator: {clipboard: {writeText() { throw new Error('No implicit clipboard access'); }}},
        URL: {createObjectURL(blob) { downloads.push(blob); const url = 'blob:explicit-' + downloads.length; urls.push(url); return url; }, revokeObjectURL: url => revocations.push(url)},
        setTimeout(callback) { callback(); return 1; }, getQueuedMessages: async () => [{session_id: 1, message: 'A retained ordinary draft'}],
        pendingTurns: () => ({}), clearQueuedMessages: async () => {}, location: {assign: value => redirects.push(value)},
        fetch: async (url, init = {}) => {
            const payload = init.body instanceof Form ? Object.fromEntries(init.body.items) : init.body ? JSON.parse(init.body) : null;
            const call = {url, ...init, payload}; calls.push(call);
            if (options.intercept) { const value = await options.intercept(call, response); if (value !== undefined) return value; }
            if (url === '/api/backups' && !init.method) return response({backup_api_version: Object.hasOwn(options, 'apiVersion') ? options.apiVersion : 2, backups: entries, recovery_required: Boolean(options.recovery), recovery_backup: options.recovery ? entries[0].id : null});
            if (url === '/api/backups/v2') return response({id: 'new-backup', encrypted: payload.format === 'encrypted', counts: payload.format === 'zip' ? {messages: 2} : {}, ...(payload.include_recovery_key ? {recovery_key: 'one-time-recovery-secret'} : {})});
            if (url.endsWith('/preview')) return response({token: 'validated-preview', encrypted: payload.backup_id ? payload.backup_id !== 'plain.zip' : payload.file?.encrypted !== false, counts: {messages: 4, memories: 2}, notes: ['Original sources validated.']});
            if (url.endsWith('/apply')) return response({status: 'restored', recovery_backup: {id: 'recovery-archive'}, browser_state: {preferences: {}, drafts: []}});
            throw new Error('Unexpected request');
        }});
    require('./helpers/ordinary-page.cjs')(context);
    context.window = context; vm.runInContext(source, context); events.DOMContentLoaded();
    const get = id => elements.get(id);
    const rows = () => get('backupList').children;
    const select = async (index = 0) => rows()[index].children.find(node => node.tagName === 'BUTTON').fire('click');
    const unlock = async (method, value) => { get('restoreUnlockMethod').value = method; await get('restoreUnlockMethod').fire('change'); get('restoreSecret').value = value; await get('restoreSecret').fire('input'); };
    const recovery = async (value = 'recovery password 123') => { get('recoveryPassword').value = value; get('recoveryPasswordConfirm').value = value; await get('recoveryPassword').fire('input'); };
    const acknowledge = async () => { get('restoreAck').checked = true; await get('restoreAck').fire('change'); };
    return {context, get, calls, writes, logs, urls, downloads, revocations, redirects, opener, document, rows, select, unlock, recovery, acknowledge, response, tabValues};
}
const posts = w => w.calls.filter(call => call.method === 'POST');
function assertPrivate(w, values) {
    const publicData = JSON.stringify({writes: w.writes, urls: w.calls.map(call => call.url), logs: w.logs, status: w.get('backupStatus').textContent});
    for (const value of values) assert.ok(!publicData.includes(value), 'Secret escaped into storage, URL, log or status');
}

test('encrypted creation is default, preserves Unicode spaces, and hides encrypted counts', async () => {
    const w = app(); await w.context.BackupUI.open();
    assert.equal(w.get('backupEncrypted').checked, true); assert.equal(w.get('backupRecoveryKey').checked, false);
    assert.match(text(w.rows()[0]), /contents hidden until unlock/); assert.doesNotMatch(text(w.rows()[0]), /0 messages/);
    const secret = ' 🌿'.repeat(6); w.get('backupPassword').value = secret; w.get('backupPasswordConfirm').value = secret;
    await w.get('backupCreate').fire('click');
    const body = posts(w)[0].payload;
    assert.equal(body.format, 'encrypted'); assert.equal(body.password, secret); assert.equal(body.include_recovery_key, false);
    assert.equal(body.browser_state.drafts[0].message, 'A retained ordinary draft');
    assert.equal(w.get('backupPassword').value, ''); assertPrivate(w, [secret]);
    assert.equal(posts(w)[0].url, '/api/backups/v2');
});

test('unreadable draft storage prevents creation and restore before any mutation', async () => {
    const w = app(); await w.context.BackupUI.open();
    let reads = 0;
    w.context.getQueuedMessages = async (review, options) => {
        assert.equal(review, true); assert.equal(options.requireReadable, true); reads++;
        throw new Error('Saved drafts could not be read. Your backup or restore was not started.');
    };
    w.get('backupPassword').value = w.get('backupPasswordConfirm').value = 'synthetic backup password';
    await w.get('backupCreate').fire('click');
    assert.equal(posts(w).length, 0); assert.match(w.get('backupStatus').textContent, /could not be read/);
    await w.unlock('password', 'source password'); await w.select(); await w.recovery(); await w.acknowledge();
    await w.get('backupApply').fire('click');
    assert.equal(posts(w).filter(call => call.url.endsWith('/apply')).length, 0);
    assert.equal(reads, 2); assert.equal(w.redirects.length, 0);
});

test('backup includes pending, composer and recovery-fallback text saved in this tab', async () => {
    const w = app(); await w.context.BackupUI.open();
    delete w.context.pendingTurns; // The standalone recovery page does not load the chat script.
    w.tabValues.set('altera_pending_turns', JSON.stringify({one: {message: 'Pending text', session_id: 3, resolved_session_id: 4}, greeting: {greet: true}}));
    w.tabValues.set('altera_composer_draft', JSON.stringify({message: 'Composer text', session_id: 5}));
    w.tabValues.set('altera_restored_drafts', JSON.stringify([{message: 'Fallback text', session_id: 6}]));
    w.get('backupPassword').value = w.get('backupPasswordConfirm').value = 'synthetic backup password';
    await w.get('backupCreate').fire('click');
    const drafts = posts(w)[0].payload.browser_state.drafts;
    assert.deepEqual(drafts.map(item => [item.session_id, item.message]), [[1, 'A retained ordinary draft'], [4, 'Pending text'], [5, 'Composer text'], [6, 'Fallback text']]);
    assert.equal(w.tabValues.size, 3);
});

test('unreadable or malformed tab drafts stop the backup before its request', async () => {
    for (const [key, value] of [['altera_pending_turns', '{'], ['altera_pending_turns', '[]'],
        ['altera_composer_draft', '{"message":3}'], ['altera_restored_drafts', '{}']]) {
        const w = app(); await w.context.BackupUI.open(); w.tabValues.set(key, value);
        w.get('backupPassword').value = w.get('backupPasswordConfirm').value = 'synthetic backup password';
        await w.get('backupCreate').fire('click');
        assert.equal(posts(w).length, 0); assert.match(w.get('backupStatus').textContent, /Saved tab drafts could not be read/);
        assert.equal(w.tabValues.get(key), value);
    }
    const w = app(); await w.context.BackupUI.open();
    w.context.sessionStorage.getItem = () => { throw new Error('synthetic storage failure'); };
    w.get('backupPassword').value = w.get('backupPasswordConfirm').value = 'synthetic backup password';
    await w.get('backupCreate').fire('click');
    assert.equal(posts(w).length, 0); assert.match(w.get('backupStatus').textContent, /Saved tab drafts could not be read/);
});

test('tab drafts added during an asynchronous queue read enter the snapshot', async () => {
    const gate = deferred(), w = app(); await w.context.BackupUI.open();
    w.context.getQueuedMessages = async () => gate.promise;
    w.get('backupPassword').value = w.get('backupPasswordConfirm').value = 'synthetic backup password';
    const creating = w.get('backupCreate').fire('click'); await tick();
    w.tabValues.set('altera_restored_drafts', JSON.stringify([{message: 'Late tab draft', session_id: 1}]));
    gate.resolve([]); await creating;
    assert.equal(posts(w)[0].payload.browser_state.drafts[0].message, 'Late tab draft');
});

test('a tab draft moved during the queue read remains in the snapshot', async () => {
    const gate = deferred(), w = app(); await w.context.BackupUI.open();
    w.tabValues.set('altera_composer_draft', JSON.stringify({message: 'Moving draft', session_id: 1}));
    w.context.getQueuedMessages = async () => gate.promise;
    w.get('backupPassword').value = w.get('backupPasswordConfirm').value = 'synthetic backup password';
    const creating = w.get('backupCreate').fire('click'); await tick();
    w.tabValues.delete('altera_composer_draft'); gate.resolve([]); await creating;
    assert.equal(posts(w)[0].payload.browser_state.drafts[0].message, 'Moving draft');
});

test('a dataset change during queue collection prevents backup creation', async () => {
    const gate = deferred(), w = app(); await w.context.BackupUI.open();
    w.context.getQueuedMessages = async () => gate.promise;
    w.get('backupPassword').value = w.get('backupPasswordConfirm').value = 'synthetic backup password';
    const creating = w.get('backupCreate').fire('click'); await tick();
    w.context.AlteraAPI.epoch = 'new-epoch'; gate.resolve([]); await creating;
    assert.equal(posts(w).length, 0); assert.match(w.get('backupStatus').textContent, /data changed while collecting drafts/);
});

test('restore fallback appends to tab drafts rather than overwriting them', async () => {
    const gate = deferred(), w = app({intercept(call) { if (call.url.endsWith('/apply')) return gate.promise; }});
    await w.context.BackupUI.open(); await w.unlock('password', 'source password'); await w.select(); await w.recovery(); await w.acknowledge();
    const applying = w.get('backupApply').fire('click'); await tick();
    w.tabValues.set('altera_restored_drafts', JSON.stringify([{message: 'Arrived during restore', session_id: 1}]));
    gate.resolve(w.response({status: 'restored', recovery_backup: {id: 'recovery'},
        browser_state: {preferences: {}, drafts: [{message: 'From backup', session_id: 1}]}}));
    await applying;
    assert.deepEqual(JSON.parse(w.tabValues.get('altera_restored_drafts')).map(item => item.message), ['Arrived during restore', 'From backup']);
    assert.deepEqual(w.redirects, ['/']);
});

test('restore keeps pending turns and drafts added while the server response is pending', async () => {
    const gate = deferred(), queue = [{session_id: 1, message: 'Existing draft'}], restored = [];
    const w = app({intercept(call) { if (call.url.endsWith('/apply')) return gate.promise; }});
    w.context.getQueuedMessages = async () => [...queue];
    w.context.clearQueuedMessages = async () => { throw new Error('Restore must not clear the existing queue'); };
    w.context.restoreBrowserDrafts = async drafts => { restored.push(...drafts); };
    await w.context.BackupUI.open(); await w.unlock('password', 'source password'); await w.select();
    await w.recovery(); await w.acknowledge();
    const applying = w.get('backupApply').fire('click'); await tick();
    queue.push({session_id: 1, message: 'Arrived during restore'});
    gate.resolve(w.response({status: 'restored', recovery_backup: {id: 'recovery'},
        browser_state: {preferences: {}, drafts: [{session_id: 1, message: 'Restored draft'}]}}));
    await applying;
    assert.deepEqual(queue.map(item => item.message), ['Existing draft', 'Arrived during restore']);
    assert.equal(restored[0].message, 'Restored draft'); assert.deepEqual(w.redirects, ['/']);
    assert.ok(!w.writes.some(item => item.key === 'altera_pending_turns' && item.removed));
});

test('older or unsupported running servers leave only saved backup downloads available', async () => {
    for (const apiVersion of [undefined, null, 1, '2', 3]) {
        const w = app({apiVersion}); await w.context.BackupUI.open();
        assert.equal(w.get('backupCreate').disabled, true);
        assert.equal(w.get('backupPreview').disabled, true);
        assert.equal(w.get('backupPassword').disabled, true);
        assert.match(w.get('backupStatus').textContent, /Restart Altera/);
        assert.ok(w.rows()[0].children.find(node => node.tagName === 'A'));
        w.get('backupPassword').value = w.get('backupPasswordConfirm').value = 'must never reach legacy server';
        await w.get('backupCreate').fire('click');
        await w.get('backupPreview').fire('click');
        await w.get('backupApply').fire('click');
        assert.equal(posts(w).length, 0);
    }
});

test('a server rollback after capability discovery cannot reach the legacy mutation route', async () => {
    let legacyMutations = 0;
    const w = app({intercept(call, response) {
        if (call.method !== 'POST') return;
        if (call.url === '/api/backups') { legacyMutations++; return response({encrypted: false}); }
        return response({error: 'Not found.'}, 404);
    }});
    await w.context.BackupUI.open();
    w.get('backupPassword').value = w.get('backupPasswordConfirm').value = 'synthetic password only';
    await w.get('backupCreate').fire('click');
    assert.equal(posts(w).length, 1);
    assert.equal(posts(w)[0].url, '/api/backups/v2');
    assert.equal(legacyMutations, 0);
    assert.match(w.get('backupStatus').textContent, /Not found/);
});

test('password length counts Unicode characters and bounds UTF-8 bytes with matching confirmation', async () => {
    const w = app(); await w.context.BackupUI.open();
    for (const [password, confirmation] of [['🌿'.repeat(11), '🌿'.repeat(11)], ['🌿'.repeat(300), '🌿'.repeat(300)], ['valid password 123', 'different confirmation']]) {
        w.get('backupPassword').value = password; w.get('backupPasswordConfirm').value = confirmation;
        await w.get('backupCreate').fire('click'); assert.equal(posts(w).length, 0);
    }
    assert.equal(w.document.activeElement, w.get('backupPasswordConfirm'));
    w.get('backupPassword').value = '🌿'.repeat(12); w.get('backupPasswordConfirm').value = '🌿'.repeat(12);
    await w.get('backupCreate').fire('click'); assert.equal(posts(w).length, 1);
});

test('plaintext ZIP requires an explicit opt-out and sends no password or recovery key request', async () => {
    const w = app(); await w.context.BackupUI.open();
    w.get('backupPassword').value = 'must not be sent'; w.get('backupEncrypted').checked = false;
    await w.get('backupEncrypted').fire('change'); assert.equal(w.get('backupPlaintextWarning').hidden, false);
    await w.get('backupCreate').fire('click'); const body = posts(w)[0].payload;
    assert.equal(body.format, 'zip'); assert.equal(body.password, undefined); assert.equal(body.include_recovery_key, undefined);
    assert.match(w.get('backupStatus').textContent, /Anyone with the file/);
});

test('one-time recovery key is downloaded only by the explicit button and cleared on close/reopen', async () => {
    const w = app(); await w.context.BackupUI.open();
    w.get('backupPassword').value = w.get('backupPasswordConfirm').value = 'creation password 123'; w.get('backupRecoveryKey').checked = true;
    await w.get('backupCreate').fire('click');
    assert.equal(posts(w)[0].payload.include_recovery_key, true); assert.equal(w.downloads.length, 0);
    assert.equal(w.get('backupRecoveryKeyValue').value, 'one-time-recovery-secret');
    assert.equal(w.document.activeElement, w.get('backupRecoveryKeyDownload'));
    await w.get('backupRecoveryKeyDownload').fire('click'); assert.equal(w.downloads.length, 1);
    assert.match(await w.downloads[0].text(), /one-time-recovery-secret/);
    assert.match(await w.downloads[0].text(), /Backup: new-backup/); assert.equal(w.revocations.length, 1);
    assertPrivate(w, ['creation password 123', 'one-time-recovery-secret']);
    await w.get('backupClose').fire('click'); assert.equal(w.get('backupRecoveryKeyValue').value, ''); assert.equal(w.document.activeElement, w.opener);
    await w.context.BackupUI.open(); assert.equal(w.get('backupRecoveryKeyPanel').hidden, true); assert.equal(w.get('backupRecoveryKey').checked, false);
});

test('saved encrypted password preview and Apply resend only that credential and a dedicated recovery password', async () => {
    const w = app(); await w.context.BackupUI.open(); await w.unlock('password', ' source password 123 '); await w.select();
    assert.equal(posts(w)[0].payload.password, ' source password 123 '); assert.equal(posts(w)[0].payload.recovery_key, undefined);
    assert.equal(w.get('recoveryEncrypted').disabled, true); await w.recovery(); await w.acknowledge();
    await w.get('backupApply').fire('click'); const body = posts(w).at(-1).payload;
    assert.equal(body.token, 'validated-preview'); assert.equal(body.password, ' source password 123 '); assert.equal(body.recovery_key, undefined);
    assert.equal(body.recovery_format, 'encrypted'); assert.equal(body.recovery_password, 'recovery password 123');
    assert.deepEqual(w.redirects, ['/']); assertPrivate(w, [' source password 123 ', 'recovery password 123']);
    assert.equal(w.get('restoreSecret').value, '');
});

test('multipart recovery-key preview and Apply send recovery_key, never password', async () => {
    const w = app(); await w.context.BackupUI.open(); await w.unlock('recovery_key', 'source-recovery-key');
    w.get('backupFile').files = [{name: 'encrypted.alterabak', encrypted: true}]; await w.get('backupFile').fire('change');
    await w.get('backupPreview').fire('click'); const body = posts(w)[0].payload;
    assert.equal(body.file.name, 'encrypted.alterabak'); assert.equal(body.recovery_key, 'source-recovery-key'); assert.equal(body.password, undefined);
    assert.equal(body.restore_connections, 'false'); await w.recovery(); await w.acknowledge(); await w.get('backupApply').fire('click');
    const applied = posts(w).at(-1).payload; assert.equal(applied.recovery_key, 'source-recovery-key'); assert.equal(applied.password, undefined);
    assertPrivate(w, ['source-recovery-key', 'recovery password 123']);
});

test('plain ZIP restoration needs no unlock and can explicitly choose a plaintext recovery ZIP', async () => {
    const w = app(); await w.context.BackupUI.open(); await w.select(1);
    assert.equal(posts(w)[0].payload.password, undefined); assert.equal(posts(w)[0].payload.recovery_key, undefined);
    assert.equal(w.get('recoveryEncrypted').checked, true); assert.equal(w.get('recoveryEncrypted').disabled, false);
    w.get('recoveryEncrypted').checked = false; await w.get('recoveryEncrypted').fire('change'); await w.acknowledge();
    await w.get('backupApply').fire('click'); const body = posts(w).at(-1).payload;
    assert.equal(body.recovery_format, 'zip'); assert.equal(body.recovery_password, undefined);
});

test('wrong key and Apply errors preserve drafts and open credentials for deliberate retry without leaks', async () => {
    let attempts = 0;
    const w = app({intercept(call, reply) {
        if (call.url.endsWith('/preview') && attempts++ === 0) return reply({error: 'Wrong key: source-secret'}, 400);
        if (call.url.endsWith('/apply')) return reply({error: 'Restore failed; existing data preserved'}, 409);
    }});
    await w.context.BackupUI.open(); await w.unlock('password', 'source-secret'); await w.select();
    assert.equal(w.get('restorePreview').hidden, true); assert.equal(w.get('restoreSecret').value, 'source-secret'); assertPrivate(w, ['source-secret']);
    assert.equal(w.document.activeElement, w.get('restoreSecret'));
    await w.get('backupPreview').fire('click'); await w.recovery(); await w.acknowledge(); await w.get('backupApply').fire('click');
    assert.equal(w.writes.length, 0); assert.equal(w.redirects.length, 0); assert.equal(w.get('restoreSecret').value, 'source-secret');
    assert.match(w.get('backupStatus').textContent, /existing data preserved/);
});

test('credential changes reject a late stale preview even if transport ignores abort', async () => {
    const gate = deferred(); const w = app({intercept(call) { if (call.url.endsWith('/preview')) return gate.promise; }});
    await w.context.BackupUI.open(); await w.unlock('password', 'first source key'); const pending = w.select(); await tick();
    assert.equal(w.get('restoreSecret').disabled, true);
    w.get('restoreSecret').value = 'changed source key'; await w.get('restoreSecret').fire('input');
    gate.resolve(w.response({token: 'stale-token', encrypted: true, counts: {messages: 99}, notes: []})); await pending;
    assert.equal(w.get('restorePreview').hidden, true); assert.equal(w.get('backupApply').disabled, true); assert.equal(w.get('restoreAck').checked, false);
});

test('file, method and connection changes invalidate preview; recovery password only resets acknowledgment', async () => {
    const w = app(); await w.context.BackupUI.open(); await w.unlock('password', 'source key 123'); await w.select();
    await w.acknowledge(); await w.recovery(); assert.equal(w.get('restorePreview').hidden, false); assert.equal(w.get('restoreAck').checked, false);
    w.get('restoreConnections').checked = true; await w.get('restoreConnections').fire('change'); assert.equal(w.get('restorePreview').hidden, true);
    await w.get('backupPreview').fire('click'); w.get('backupFile').files = [{name: 'different'}]; await w.get('backupFile').fire('change'); assert.equal(w.get('backupApply').disabled, true);
    await w.unlock('recovery_key', 'different-key'); assert.equal(w.get('restorePreview').hidden, true);
});

test('readonly preview close aborts, clears secrets and ignores late result after reopening', async () => {
    const gate = deferred(); const w = app({intercept(call) { if (call.url.endsWith('/preview')) return gate.promise; }});
    await w.context.BackupUI.open(); await w.unlock('password', 'temporary source key'); const pending = w.select(); await tick();
    await w.get('backupClose').fire('click'); assert.equal(w.get('restoreSecret').value, ''); assert.equal(w.get('backupDialog').open, false);
    assert.equal(posts(w)[0].signal.aborted, true); await w.context.BackupUI.open();
    gate.resolve(w.response({token: 'late-token', encrypted: true, counts: {messages: 999}, notes: []})); await pending;
    assert.equal(w.get('restorePreview').hidden, true); assert.equal(w.get('backupApply').disabled, true);
});

test('double create and Apply submit once and mutation close/Escape cannot suggest cancellation', async () => {
    const createGate = deferred(), applyGate = deferred();
    const w = app({intercept(call) { if (call.url === '/api/backups/v2' && call.method === 'POST') return createGate.promise; if (call.url.endsWith('/apply')) return applyGate.promise; }});
    await w.context.BackupUI.open(); w.get('backupPassword').value = w.get('backupPasswordConfirm').value = 'creation password 123';
    const creating = w.get('backupCreate').fire('click'); await tick(); await w.get('backupCreate').fire('click');
    const beforeReopen = w.calls.length;
    await w.context.BackupUI.open();
    assert.equal(w.calls.length, beforeReopen); assert.equal(w.get('backupPassword').value, 'creation password 123');
    assert.equal(posts(w).length, 1); assert.equal(w.get('backupClose').disabled, true); assert.equal((await w.get('backupDialog').fire('cancel')).prevented, true);
    await w.get('backupClose').fire('click'); assert.equal(w.get('backupDialog').open, true);
    createGate.resolve(w.response({encrypted: true, counts: {}})); await creating;
    await w.unlock('password', 'source password'); await w.select(); await w.recovery(); await w.acknowledge();
    const applying = w.get('backupApply').fire('click'); await tick(); await w.get('backupApply').fire('click');
    const beforeApplyReopen = w.calls.length;
    await w.context.BackupUI.open();
    assert.equal(w.calls.length, beforeApplyReopen); assert.equal(w.get('restoreSecret').value, 'source password');
    assert.equal(posts(w).filter(call => call.url.endsWith('/apply')).length, 1); assert.equal(w.get('backupClose').disabled, true);
    applyGate.resolve(w.response({recovery_backup: {id: 'recovery'}, browser_state: {preferences: {}, drafts: []}})); await applying;
});

test('interrupted-restore recovery resends source credential without requiring another recovery password', async () => {
    const w = app({recovery: true}); await w.context.BackupUI.open();
    assert.equal(w.get('backupCreate').disabled, true); assert.equal(w.get('recoverySettings').hidden, true);
    assert.match(w.get('restoreAckText').textContent, /No additional recovery copy/);
    await w.unlock('password', 'recovery source password'); await w.select(); await w.acknowledge(); await w.get('backupApply').fire('click');
    const body = posts(w).at(-1).payload; assert.equal(body.password, 'recovery source password'); assert.equal(body.recovery_password, undefined); assert.equal(body.recovery_format, undefined);
});

test('recovery confirmation is required before mutation and encrypted sources cannot opt out', async () => {
    const w = app(); await w.context.BackupUI.open(); await w.unlock('password', 'source password'); await w.select();
    w.get('recoveryEncrypted').checked = false; await w.get('recoveryEncrypted').fire('change');
    assert.equal(w.get('recoveryEncrypted').checked, true);
    await w.recovery('new recovery password'); w.get('recoveryPasswordConfirm').value = 'different'; await w.acknowledge();
    await w.get('backupApply').fire('click'); assert.equal(posts(w).filter(call => call.url.endsWith('/apply')).length, 0);
    assert.equal(w.document.activeElement, w.get('recoveryPasswordConfirm'));
    assert.equal(posts(w)[0].payload.recovery_password, undefined);
});

test('multipart password and saved recovery-key methods preserve exact unlock field selection', async () => {
    const uploaded = app(); await uploaded.context.BackupUI.open(); await uploaded.unlock('password', ' uploaded password ');
    uploaded.get('backupFile').files = [{name: 'encrypted.backup', encrypted: true}]; await uploaded.get('backupFile').fire('change');
    await uploaded.get('backupPreview').fire('click');
    assert.equal(posts(uploaded)[0].payload.password, ' uploaded password '); assert.equal(posts(uploaded)[0].payload.recovery_key, undefined);
    const saved = app(); await saved.context.BackupUI.open(); await saved.unlock('recovery_key', 'saved-recovery-key'); await saved.select();
    assert.equal(posts(saved)[0].payload.recovery_key, 'saved-recovery-key'); assert.equal(posts(saved)[0].payload.password, undefined);
});

test('new backup clears an older one-time key and Escape clears every credential field', async () => {
    const w = app(); await w.context.BackupUI.open();
    w.get('backupPassword').value = w.get('backupPasswordConfirm').value = 'creation password 123'; w.get('backupRecoveryKey').checked = true;
    await w.get('backupCreate').fire('click'); assert.notEqual(w.get('backupRecoveryKeyValue').value, '');
    w.get('backupPassword').value = w.get('backupPasswordConfirm').value = 'new creation password'; w.get('backupRecoveryKey').checked = false;
    await w.get('backupCreate').fire('click'); assert.equal(w.get('backupRecoveryKeyValue').value, ''); assert.equal(w.get('backupRecoveryKeyPanel').hidden, true);
    for (const id of ['backupPassword', 'backupPasswordConfirm', 'restoreSecret', 'recoveryPassword', 'recoveryPasswordConfirm', 'backupRecoveryKeyValue']) w.get(id).value = 'ephemeral-secret';
    const canceled = await w.get('backupDialog').fire('cancel'); assert.equal(canceled.prevented, false);
    for (const id of ['backupPassword', 'backupPasswordConfirm', 'restoreSecret', 'recoveryPassword', 'recoveryPasswordConfirm', 'backupRecoveryKeyValue']) assert.equal(w.get(id).value, '');
    assertPrivate(w, ['ephemeral-secret']);
});

test('malformed preview metadata cannot enable Apply or claim encryption', async () => {
    const w = app({intercept(call, reply) { if (call.url.endsWith('/preview')) return reply({token: 'missing-format', counts: {messages: 2}}); }});
    await w.context.BackupUI.open(); await w.unlock('password', 'source password'); await w.select();
    assert.equal(w.get('restorePreview').hidden, true); assert.equal(w.get('backupApply').disabled, true);
    assert.match(w.get('backupStatus').textContent, /incomplete restore preview/);
});
