const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {createHash} = require('node:crypto');

const source = fs.readFileSync('static/js/knowledge.js', 'utf8');
const template = fs.readFileSync('templates/memory.html', 'utf8');
const copy = value => structuredClone(value);
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; }

class Element {
    constructor(tag, owner) { this.tag = tag; this.owner = owner; this.children = []; this.parent = null; this.listeners = {}; this.attributes = {}; this.value = ''; this._text = ''; this.className = ''; this.open = false; this.checked = false; this.hidden = false; this.disabled = false; }
    get isConnected() { return this === this.owner.body || Boolean(this.parent?.isConnected); }
    set innerHTML(_) { throw new Error('Memory content must stay text'); }
    set textContent(value) { for (const child of this.children) child.parent = null; this.children = []; this._text = value == null ? '' : String(value); }
    get textContent() { return this._text; }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
    prepend(...nodes) { for (const node of nodes) node.parent = this; this.children.unshift(...nodes); }
    replaceChildren(...nodes) { for (const node of this.children) node.parent = null; this.children = []; this._text = ''; this.append(...nodes); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    focus() { if (this.isConnected) this.owner.activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; for (const callback of this.listeners.close || []) callback(); }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); this.parent = null; }
    querySelectorAll(selector) { return descendants(this).slice(1).filter(node => selector === 'button:not([data-export]), select' ? ((node.tag === 'button' && !('data-export' in node.attributes)) || node.tag === 'select') : selector === '[data-export]' ? 'data-export' in node.attributes : selector === '[data-report-export]' ? 'data-report-export' in node.attributes : false); }
    async fire(name) { let prevented = false; const event = {preventDefault() { prevented = true; }, target: this}; for (const callback of this.listeners[name] || []) await callback(event); if (name === 'cancel' && !prevented) this.close(); return prevented; }
    async click() { if (this.disabled) return; this.focus(); await this.fire('click'); }
}
const descendants = node => [node, ...node.children.flatMap(descendants)];
const contents = node => [node.textContent, ...node.children.map(contents)].join('\n');
const find = (node, tag, label) => descendants(node).find(child => child.tag === tag && child.textContent === label);

function memory(id, content, changes = {}) {
    return {id, content, content_sha256: createHash('sha256').update(content).digest('hex'), review_sha256: 'f'.repeat(64), metadata: {type: 'fact', confidence: .42}, source_kind: 'added by you', needs_review: false, sources: [],
        priority: {salience: 3, pinned: false, decay_factor: .5, age_days: 30, recall_count: 2, revision: 7}, ...changes};
}

function app(options = {}) {
    const memories = new Map([['mara', [memory('mara-memory', 'Mara’s tea preference')]], ['ana', [memory('ana-memory', 'Ana’s painting preference')]]]);
    const epochs = new Map([['mara', 'epoch-one'], ['ana', 'epoch-one']]);
    const calls = [], elements = new Map(), documentEvents = {}, timers = new Map(); let readCount = 0, nextId = 0;
    const document = {body: null, activeElement: null, getElementById: id => elements.get(id), createElement: tag => new Element(tag, document),
        addEventListener(name, callback) { (documentEvents[name] ||= []).push(callback); },
        querySelector(selector) { if (selector === '.knowledge-dialog[open]') return descendants(document.body).find(node => node.tag === 'dialog' && node.open && node.className.split(' ').includes('knowledge-dialog')) || null; return null; }};
    document.body = new Element('body', document);
    for (const match of template.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"[^>]*>/g)) { const node = new Element(match[1], document); node.id = match[2]; elements.set(node.id, node); }
    const page = elements.get('knowledgePage'); document.body.append(page); for (const element of elements.values()) if (element !== page) page.append(element);
    const context = vm.createContext({document, URL, console, AbortController, crypto: {randomUUID: () => (++nextId).toString(16).padStart(32, '0')}, location: {href: options.url || 'http://localhost/memory?scope=mara'}, __DATA_EPOCH__: 'epoch-one',
        AlteraAPI: {epoch: 'epoch-one'},
        Audio: class { constructor() { throw new Error('Priority controls must not play audio'); } },
        setTimeout(callback, delay) { const id = timers.size + 1; timers.set(id, {callback, delay}); return id; }, clearTimeout(id) { timers.delete(id); },
        fetch: async (url, init = {}) => {
            const payload = init.body ? JSON.parse(init.body) : undefined, call = {url, ...init, payload}; calls.push(call);
            const method = init.method || 'GET', reply = (data, status = 200) => ({ok: status < 400, status, json: async () => copy(data)});
            if (options.intercept) { const result = await options.intercept({url, method, payload, calls, memories, epochs, reply}); if (result !== undefined) return result; }
            if (url === '/api/knowledge/scopes') return reply({scopes: [{id: 'mara', name: options.name || 'Mara'}, {id: 'ana', name: 'Ana'}], active: 'mara'});
            if (url === '/api/knowledge/messages/77') return reply({scope: 'mara', name: 'Mara', dataset_epoch: 'epoch-one',
                review_through: 12, recall_after: 0, source_sha256: 'a'.repeat(64), message: {id: 77, session_id: 9, content: 'Synthetic source'}});
            const inventory = url.match(/^\/api\/knowledge\/scopes\/([^/]+)\/memories$/);
            if (inventory && method === 'GET') {
                readCount++;
                if (options.inventoryGate && readCount === options.gatedRead) return options.inventoryGate.promise;
                return reply({dataset_epoch: epochs.get(inventory[1]), memories: memories.get(inventory[1]), review_through: 12, recall_after: 0, repair_pending: false, repair_available: false, notes: null});
            }
            if (inventory && method === 'POST') {
                memories.get(inventory[1]).push(memory('saved-memory', payload.content));
                return reply({status: 'ok', id: 'saved-memory'}, 201);
            }
            const priority = url.match(/^\/api\/knowledge\/scopes\/([^/]+)\/memories\/([^/]+)\/priority$/);
            if (priority && method === 'PATCH') {
                if (options.failWrite) return reply({error: options.error || 'Priority write could not be saved'}, 503);
                if (options.writeGate) return options.writeGate.promise;
                const item = memories.get(priority[1]).find(row => row.id === priority[2]);
                assert.equal(payload.dataset_epoch, epochs.get(priority[1])); assert.equal(payload.content_sha256, item.content_sha256); assert.equal(payload.revision, item.priority.revision);
                item.priority = {...item.priority, pinned: payload.pinned, salience: payload.salience, revision: item.priority.revision + 1};
                return reply({id: item.id, content_sha256: item.content_sha256, priority: item.priority});
            }
            const forget = url.match(/^\/api\/knowledge\/scopes\/([^/]+)\/memories\/([^/]+)$/);
            if (forget && method === 'PUT') {
                const item = memories.get(forget[1]).find(row => row.id === forget[2]);
                if (!item) return reply({error: 'Memory not found'}, 404);
                item.content = payload.content; item.content_sha256 = createHash('sha256').update(payload.content).digest('hex');
                item.review_sha256 = 'e'.repeat(64);
                return reply({status: 'ok', notice: 'Synthetic memory corrected.'});
            }
            if (forget && method === 'DELETE') {
                const item = memories.get(forget[1]).find(row => row.id === forget[2]);
                if (!item) return reply({error: 'Memory not found'}, 404);
                assert.equal(payload.confirm, 'forget');
                memories.set(forget[1], memories.get(forget[1]).filter(row => row.id !== forget[2]));
                return reply({status: 'ok', notice: 'Synthetic memory forgotten.'});
            }
            return reply({error: 'Unexpected test endpoint'}, 404);
        },
    });
    require('./helpers/ordinary-page.cjs')(context);
    context.window = context; vm.runInContext(source, context);
    const dialog = () => document.querySelector('.knowledge-dialog[open]');
    return {context, document, elements, calls, memories, epochs, timers,
        async init() { for (const callback of documentEvents.DOMContentLoaded || []) await callback(); },
        async open() { const button = find(elements.get('knowledgeCards'), 'button', 'Retrieval priority'); assert.ok(button, 'inventory must expose a Retrieval priority control: ' + elements.get('knowledgeStatus').textContent); await button.click(); return dialog(); },
        async choose(owner) { elements.get('knowledgeScope').value = owner; await elements.get('knowledgeScope').fire('change'); },
        dialog, writes: () => calls.filter(call => call.method && call.method !== 'GET'),
        reads: owner => calls.filter(call => call.url === `/api/knowledge/scopes/${owner}/memories` && !call.method),
        status: () => elements.get('knowledgeStatus').textContent,
    };
}

function controls(dialog) {
    const all = descendants(dialog);
    return {form: all.find(node => node.tag === 'form'), importance: all.find(node => node.tag === 'select'), pin: all.find(node => node.tag === 'input' && node.type === 'checkbox'),
        save: find(dialog, 'button', 'Save priority'), cancel: find(dialog, 'button', 'Cancel'), feedback: all.find(node => node.tag === 'p' && node.attributes.role === 'status')};
}

async function forgetDialog(a) {
    const button = find(a.elements.get('knowledgeCards'), 'button', 'Forget');
    assert.ok(button); await button.click();
    const box = a.dialog();
    return {box, form: descendants(box).find(node => node.tag === 'form'),
        feedback: descendants(box).find(node => node.tag === 'p' && node.attributes.role === 'status')};
}

async function correctDialog(a) {
    const button = find(a.elements.get('knowledgeCards'), 'button', 'Correct');
    assert.ok(button); await button.click();
    const box = a.dialog();
    return {box, form: descendants(box).find(node => node.tag === 'form'),
        input: descendants(box).find(node => node.tag === 'textarea'),
        feedback: descendants(box).find(node => node.tag === 'p' && node.attributes.role === 'status')};
}

async function submitRemember(a) {
    const submitter = {disabled: false};
    await a.elements.get('rememberForm').listeners.submit[0]({preventDefault() {}, submitter});
}

test('a processed Remember retry leaves its draft visible for review', async () => {
    const a = app({intercept({method, reply}) { if (method === 'POST') return reply({status: 'already_processed', id: 'saved-memory'}); }});
    await a.init(); a.elements.get('knowledgeType').value = 'fact'; a.elements.get('knowledgeContent').value = 'Review this fact';
    await a.elements.get('knowledgeContent').fire('input'); await submitRemember(a);
    assert.equal(a.elements.get('knowledgeContent').value, 'Review this fact'); assert.match(a.status(), /already processed.*review/i);
});

test('Remember suppresses a second submission while the first is pending', async () => {
    const gate = deferred();
    const a = app({intercept({method}) { if (method === 'POST') return gate.promise; }});
    await a.init(); a.elements.get('knowledgeType').value = 'fact'; a.elements.get('knowledgeContent').value = 'One fact';
    await a.elements.get('knowledgeContent').fire('input');
    const first = submitRemember(a); await tick(); await submitRemember(a);
    assert.equal(a.writes().length, 1);
    gate.resolve({ok: true, status: 201, json: async () => ({status: 'saved', id: 'one'})});
    await first;
});

test('ordinary correction keeps its existing payload', async () => {
    const a = app(); await a.init(); const ui = await correctDialog(a);
    ui.input.value = 'Corrected ordinary text'; await ui.form.fire('submit');
    assert.deepEqual(a.writes()[0].payload, {content: 'Corrected ordinary text'});
});

test('Forget leaves the reviewed dialog open after a conflict without retrying', async () => {
    const a = app({intercept({method, reply}) {
        if (method === 'DELETE') return reply({error: 'This memory changed. Reload before forgetting.'}, 409);
    }});
    await a.init(); const ui = await forgetDialog(a); await ui.form.fire('submit');
    assert.equal(a.writes().length, 1); assert.equal(a.dialog(), ui.box);
    assert.match(ui.feedback.textContent, /memory changed/i); assert.equal(a.memories.get('mara').length, 1);
});

test('ordinary Forget retains its existing confirmation payload', async () => {
    const a = app(); await a.init(); const ui = await forgetDialog(a); await ui.form.fire('submit');
    assert.deepEqual(a.writes()[0].payload, {confirm: 'forget'});
});

test('priority opens through the card action without writing and exposes separate confidence/decay meaning', async () => {
    const a = app(); await a.init(); const box = await a.open(), ui = controls(box);
    assert.equal(a.writes().length, 0); assert.equal(a.calls.length, 2); assert.equal(a.timers.size, 0);
    assert.equal(box.attributes['aria-labelledby'], 'memoryPriorityTitle'); assert.equal(ui.importance.value, '3'); assert.equal(ui.importance.children.length, 5); assert.equal(ui.pin.checked, false);
    assert.equal(a.document.activeElement, ui.importance); assert.match(contents(box), /prevents age decay.*does not guarantee prompt selection or confirm a fact/);
    assert.match(contents(a.elements.get('knowledgeCards')), /42% confidence/); assert.match(contents(a.elements.get('knowledgeCards')), /recency factor 50%/);
});

test('submission freezes inventory owner, epoch, revision and content SHA without changing saved fact/confidence', async () => {
    const a = app(); await a.init(); const before = copy(a.memories.get('mara')[0]), box = await a.open(), ui = controls(box);
    ui.importance.value = '5'; ui.pin.checked = true; assert.equal(a.writes().length, 0); await ui.form.fire('submit');
    assert.equal(a.writes().length, 1); assert.equal(a.writes()[0].url, '/api/knowledge/scopes/mara/memories/mara-memory/priority');
    assert.deepEqual(a.writes()[0].payload, {dataset_epoch: 'epoch-one', revision: 7, content_sha256: before.content_sha256, pinned: true, salience: 5});
    assert.equal(a.memories.get('mara')[0].content, before.content); assert.deepEqual(a.memories.get('mara')[0].metadata, before.metadata); assert.equal(a.memories.get('ana')[0].priority.pinned, false);
    assert.equal(a.dialog(), null); assert.equal(a.reads('mara').length, 2); assert.match(a.status(), /priority saved.*Confidence and content are unchanged/);
});

for (const cancellation of ['button', 'escape']) {
    test(`priority ${cancellation} cancellation writes nothing and restores focus to its opener`, async () => {
        const a = app(); await a.init(); const opener = find(a.elements.get('knowledgeCards'), 'button', 'Retrieval priority'); const box = await a.open();
        controls(box).importance.value = '5'; controls(box).pin.checked = true;
        if (cancellation === 'button') await controls(box).cancel.click(); else await box.fire('cancel');
        assert.equal(a.dialog(), null); assert.equal(a.writes().length, 0); assert.equal(a.document.activeElement, opener); assert.equal(a.memories.get('mara')[0].priority.salience, 3);
    });
}

test('failed server writes preserve an inspectable dialog and error with unchanged saved priority', async () => {
    const a = app({failWrite: true, error: '<img src=x onerror=boom> write rejected'}); await a.init(); const box = await a.open(), ui = controls(box);
    ui.pin.checked = true; ui.importance.value = '4'; await ui.form.fire('submit');
    assert.equal(a.dialog(), box); assert.equal(box.open, true); assert.equal(ui.save.disabled, false); assert.equal(ui.cancel.disabled, false); assert.equal(ui.importance.value, '4'); assert.equal(ui.pin.checked, true);
    assert.equal(ui.feedback.textContent, '<img src=x onerror=boom> write rejected'); assert.equal(a.memories.get('mara')[0].priority.pinned, false); assert.equal(a.reads('mara').length, 1); assert.doesNotMatch(a.status(), /priority saved/);
});

test('double submit dispatches one PATCH and prevents cancellation while its result is pending', async () => {
    const gate = deferred(), a = app({writeGate: gate}); await a.init(); const box = await a.open(), ui = controls(box);
    const first = ui.form.fire('submit'), duplicate = ui.form.fire('submit'); await tick();
    assert.equal(a.writes().length, 1); assert.equal(ui.save.disabled, true); assert.equal(ui.cancel.disabled, true); assert.equal(await box.fire('cancel'), true); assert.equal(a.dialog(), box);
    gate.resolve({ok: true, status: 200, json: async () => ({id: 'mara-memory'})}); await first; await duplicate;
    assert.equal(a.writes().length, 1); assert.equal(a.dialog(), null);
});

for (const change of ['owner', 'epoch', 'inventory revision']) {
    test(`a ${change} change before submit requires reviewing the memory again and sends no write`, async () => {
        const a = app(); await a.init(); const opener = find(a.elements.get('knowledgeCards'), 'button', 'Retrieval priority'), box = await a.open(), ui = controls(box);
        if (change === 'owner') await a.choose('ana');
        else if (change === 'epoch') a.context.AlteraAPI.epoch = 'epoch-two';
        else await a.choose('mara');
        await ui.form.fire('submit'); assert.equal(a.writes().length, 0); assert.equal(a.dialog(), box); assert.match(ui.feedback.textContent, /displayed data changed.*review this memory/);
        await ui.cancel.click(); assert.equal(a.document.activeElement === (change === 'epoch' ? opener : a.elements.get('knowledgeScope')), true);
    });
}

test('a late acknowledgment from the prior owner cannot reload or announce success for the newly selected owner', async () => {
    const gate = deferred(), a = app({writeGate: gate}); await a.init(); const ui = controls(await a.open());
    const saving = ui.form.fire('submit'); await tick(); await a.choose('ana'); const before = a.calls.length;
    gate.resolve({ok: true, status: 200, json: async () => ({id: 'mara-memory'})}); await saving;
    assert.equal(a.calls.length, before); assert.equal(a.reads('mara').length, 1); assert.equal(a.reads('ana').length, 1); assert.match(a.status(), /Ana/); assert.doesNotMatch(a.status(), /priority saved/);
    assert.match(contents(a.elements.get('knowledgeCards')), /Ana’s painting/); assert.doesNotMatch(contents(a.elements.get('knowledgeCards')), /Mara’s tea/);
});

test('a late acknowledgment from the prior epoch cannot reload a new dataset or announce success there', async () => {
    const gate = deferred(), a = app({writeGate: gate}); await a.init(); const ui = controls(await a.open());
    const saving = ui.form.fire('submit'); await tick(); a.context.AlteraAPI.epoch = 'epoch-two'; a.epochs.set('mara', 'epoch-two'); a.memories.set('mara', [memory('new-dataset-memory', 'New dataset private content')]);
    const before = a.calls.length; gate.resolve({ok: true, status: 200, json: async () => ({id: 'mara-memory'})}); await saving;
    assert.equal(a.calls.length, before); assert.equal(a.reads('mara').length, 1); assert.doesNotMatch(a.status(), /priority saved/); assert.doesNotMatch(contents(a.elements.get('knowledgeCards')), /New dataset private content/);
});

test('inventory from a stale dataset epoch is rejected before it can expose priority controls or private text', async () => {
    const a = app(); a.epochs.set('mara', 'stale-epoch'); a.memories.set('mara', [memory('old', 'STALE PRIVATE MEMORY')]); await a.init();
    assert.match(a.status(), /data changed.*Reload/); assert.doesNotMatch(contents(a.elements.get('knowledgeCards')), /STALE PRIVATE MEMORY/); assert.equal(find(a.elements.get('knowledgeCards'), 'button', 'Retrieval priority'), undefined); assert.equal(a.writes().length, 0);
});

test('scope names, memory content and server errors stay text throughout priority review', async () => {
    const a = app({name: '<svg onload=boom>', failWrite: true, error: '<script>error()</script>'}); a.memories.set('mara', [memory('markup', '<img src=x onerror=boom>')]); await a.init();
    assert.match(contents(a.elements.get('knowledgeCards')), /<img src=x onerror=boom>/); const box = await a.open(); assert.match(contents(box), /Retrieval priority for <svg onload=boom>/);
    await controls(box).form.fire('submit'); assert.match(controls(box).feedback.textContent, /<script>error\(\)<\/script>/); assert.equal(a.writes().length, 1);
});

test('a server-side content/revision conflict cannot replace the dialog’s frozen review identity', async () => {
    const a = app({intercept({url, reply}) { if (url.endsWith('/priority')) return reply({error: 'This memory changed. Reload before changing priority.'}, 409); }});
    await a.init(); const reviewed = copy(a.memories.get('mara')[0]), box = await a.open(), ui = controls(box);
    a.memories.get('mara')[0] = memory('mara-memory', 'A later correction', {priority: {...reviewed.priority, revision: 8}});
    ui.importance.value = '4'; await ui.form.fire('submit');
    assert.equal(a.writes()[0].payload.revision, reviewed.priority.revision); assert.equal(a.writes()[0].payload.content_sha256, reviewed.content_sha256);
    assert.equal(a.dialog() === box, true); assert.match(ui.feedback.textContent, /memory changed/); assert.equal(a.reads('mara').length, 1); assert.equal(a.memories.get('mara')[0].priority.revision, 8);
});

test('a lost transport acknowledgment retains the priority dialog for review without an automatic retry', async () => {
    const a = app({intercept({url}) { if (url.endsWith('/priority')) throw new TypeError('Connection lost before acknowledgment'); }});
    await a.init(); const box = await a.open(), ui = controls(box); ui.pin.checked = true; await ui.form.fire('submit');
    assert.equal(a.dialog() === box, true); assert.match(ui.feedback.textContent, /Connection lost/); assert.equal(ui.pin.checked, true); assert.equal(ui.save.disabled, false); assert.equal(a.writes().length, 1); assert.equal(a.reads('mara').length, 1); assert.equal(a.timers.size, 0);
});

test('a late failed inventory from the prior owner cannot clear the current owner’s review controls', async () => {
    const gate = deferred(), a = app({inventoryGate: gate, gatedRead: 1}); const initializing = a.init(); await tick(); await a.choose('ana');
    assert.match(contents(a.elements.get('knowledgeCards')), /Ana’s painting/);
    gate.resolve({ok: false, status: 503, json: async () => ({error: 'Old companion inventory failure'})}); await initializing;
    assert.match(contents(a.elements.get('knowledgeCards')), /Ana’s painting/); assert.match(a.status(), /Ana/); assert.doesNotMatch(a.status(), /Old companion inventory failure/);
    assert.ok(find(a.elements.get('knowledgeCards'), 'button', 'Retrieval priority')); assert.equal(a.writes().length, 0);
});

test('a confirmed post-save inventory refresh restores focus to the connected companion selector', async () => {
    const a = app(); await a.init(); const opener = find(a.elements.get('knowledgeCards'), 'button', 'Retrieval priority'), ui = controls(await a.open());
    ui.pin.checked = true; await ui.form.fire('submit');
    assert.equal(opener.isConnected, false, 'successful reload replaces the card that opened the dialog');
    assert.equal(a.document.activeElement === a.elements.get('knowledgeScope'), true); assert.equal(a.document.activeElement.isConnected, true);
    assert.equal(a.elements.get('knowledgeScope').disabled, false); assert.match(a.status(), /Retrieval priority saved/); assert.equal(a.writes().length, 1); assert.equal(a.reads('mara').length, 2);
});

test('failed post-save inventory refresh keeps its error instead of claiming refreshed success', async () => {
    const a = app({intercept({url, method, calls, reply}) {
        if (method === 'GET' && url === '/api/knowledge/scopes/mara/memories' && calls.filter(call => call.url === url).length === 2) return reply({error: 'Saved priority, but inventory is currently unavailable'}, 503);
    }}); await a.init(); const ui = controls(await a.open()); ui.pin.checked = true; await ui.form.fire('submit');
    assert.equal(a.memories.get('mara')[0].priority.pinned, true, 'the acknowledged priority write remains saved'); assert.equal(a.dialog(), null);
    assert.equal(a.status(), 'Saved priority, but inventory is currently unavailable'); assert.doesNotMatch(a.status(), /Retrieval priority saved|Confidence and content are unchanged/);
    assert.equal(find(a.elements.get('knowledgeCards'), 'button', 'Retrieval priority'), undefined); assert.equal(a.writes().length, 1); assert.equal(a.reads('mara').length, 2);
});

test('owner navigation during post-save refresh preserves the new owner’s status, content and focus', async () => {
    const gate = deferred(), a = app({inventoryGate: gate, gatedRead: 2}); await a.init(); const ui = controls(await a.open());
    const saving = ui.form.fire('submit'); await tick(); assert.equal(a.writes().length, 1); assert.equal(a.reads('mara').length, 2); assert.equal(a.dialog(), null);
    await a.choose('ana'); const focus = a.elements.get('knowledgeSearch'); focus.focus(); const currentStatus = a.status();
    gate.resolve({ok: true, status: 200, json: async () => ({dataset_epoch: 'epoch-one', memories: copy(a.memories.get('mara')), notes: null})}); await saving;
    assert.equal(a.status(), currentStatus); assert.match(a.status(), /Ana/); assert.doesNotMatch(a.status(), /priority saved/);
    assert.match(contents(a.elements.get('knowledgeCards')), /Ana’s painting/); assert.doesNotMatch(contents(a.elements.get('knowledgeCards')), /Mara’s tea/);
    assert.equal(a.document.activeElement === focus, true); assert.equal(a.reads('ana').length, 1); assert.equal(a.writes().length, 1);
});

test('an epoch transition during post-save refresh never announces old-dataset priority success or takes focus', async () => {
    const gate = deferred(), a = app({inventoryGate: gate, gatedRead: 2}); await a.init(); const ui = controls(await a.open());
    const saving = ui.form.fire('submit'); await tick(); assert.equal(a.reads('mara').length, 2); a.context.AlteraAPI.epoch = 'epoch-two'; a.epochs.set('mara', 'epoch-two');
    const focus = a.elements.get('knowledgeSearch'); focus.focus(); const newMemory = memory('replacement-memory', 'Current dataset memory');
    gate.resolve({ok: true, status: 200, json: async () => ({dataset_epoch: 'epoch-two', memories: [newMemory], notes: null})}); await saving;
    assert.doesNotMatch(a.status(), /Retrieval priority saved|Confidence and content are unchanged/); assert.match(contents(a.elements.get('knowledgeCards')), /Current dataset memory/);
    assert.equal(a.document.activeElement === focus, true); assert.equal(a.writes().length, 1); assert.equal(a.reads('mara').length, 2);
});

test('a memory draft started for one companion cannot be submitted into another companion\'s scope and cancels native submit synchronously',async()=>{
 const a=app();await a.init();a.elements.get('knowledgeContent').value='Synthetic Mara-only draft';await a.elements.get('knowledgeContent').fire('input');await a.choose('ana');
 assert.equal(a.elements.get('knowledgeContent').value,'Synthetic Mara-only draft');let prevented=false;
 const submitting=a.elements.get('rememberForm').listeners.submit[0]({preventDefault(){prevented=true;},submitter:{disabled:false}});
 assert.equal(prevented,true);await submitting;assert.equal(a.writes().length,0);assert.match(a.status(),/started for Mara/);
});
