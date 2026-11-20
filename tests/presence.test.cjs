const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const source = fs.readFileSync('static/js/presence.js', 'utf8');
const copy = value => structuredClone(value);
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; }

class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.listeners = {}; this.value = ''; this.textContent = ''; this.checked = false; this.hidden = false; this.disabled = false; this.open = false; }
    set innerHTML(value) { throw new Error('Presence UI must render data as text, never HTML'); }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    showModal() { this.open = true; }
    close() { this.open = false; }
}

function world() {
    const contexts = new Map([[1, {session_id: 1, avatar_id: 'mara', dataset_epoch: 'epoch-one'}], [2, {session_id: 2, avatar_id: 'ana', dataset_epoch: 'epoch-one'}]]);
    const preferences = new Map([...contexts].map(([sid, scope]) => [sid, {...scope, revision: 0, enabled: false, timezone: 'UTC',
        quiet_enabled: true, quiet_start: 1320, quiet_end: 480, interval_minutes: 360, cooldown_minutes: 360, daily_cap: 2,
        expiry_minutes: 120, provider_approval: null, needs_review: false}]));
    const provider = {provider_key: 'local', model: 'model-a', base_url: 'http://127.0.0.1:1234/v1', network: false, fingerprint: 'provider-a'};
    const items = new Map(), rituals = new Map(), jobs = new Map(), checkKeys = new Map();
    function addItem(sid, id, changes = {}) { const item = {id, job_id: 'job-' + id, ...contexts.get(sid), kind: 'checkin',
        text: `Message for ${sid}`, created_at: '2026-10-27T12:00:00Z', expires_at: '2099-01-01T00:00:00Z', state: 'available',
        read_at: null, dismissed_at: null, accepted_at: null, accepted_message_id: null, error: '', ...changes}; items.set(id, item); return item; }
    return {contexts, preferences, provider, items, rituals, jobs, checkKeys, addItem, nextRitual: 1};
}

function app(options = {}) {
    const server = options.server || world(), elements = new Map(), documentEvents = {}, windowEvents = {};
    for (const suffix of ['Banner', 'BannerText', 'OpenInbox', 'BannerHost', 'Dialog', 'Close', 'Status', 'Inbox', 'Jobs', 'Scope', 'Provider',
        'Enabled', 'Timezone', 'QuietEnabled', 'QuietStart', 'QuietEnd', 'Interval', 'DailyCap', 'NetworkApproval', 'NetworkText',
        'Save', 'Refresh', 'CheckNow', 'ReloadSettings', 'Preferences', 'Rituals', 'RitualForm', 'RitualKind', 'RitualTime', 'RitualWeekday',
        'RitualEnabled', 'RitualSave', 'RitualCancel']) elements.set('presence' + suffix, new Element());
    elements.get('presenceBanner').hidden = true;
    elements.get('presenceRitualKind').value = 'greeting'; elements.get('presenceRitualTime').value = '08:00';
    elements.get('presenceRitualWeekday').value = '0'; elements.get('presenceRitualEnabled').checked = true;
    const page = {sessionId: 1, companionId: 'mara', datasetEpoch: 'epoch-one'};
    const calls = [], accepted = [], expressions = [], timers = new Map(); let timerId = 0, inboxReads = 0, ritualWrites = 0, checkWrites = 0;
    const context = vm.createContext({
        document: {hidden: false, getElementById: id => elements.get(id), createElement: tag => new Element(tag),
            addEventListener(name, fn) { (documentEvents[name] ||= []).push(fn); }},
        crypto: webcrypto, AbortController, Intl, Date, console,
        Audio: class { constructor() { throw new Error('Presence must not autoplay audio'); } },
        setTimeout(fn, delay) { const id = ++timerId; timers.set(id, {fn, delay}); return id; }, clearTimeout(id) { timers.delete(id); },
        addEventListener(name, fn) { (windowEvents[name] ||= []).push(fn); },
        setCompanionExpression(expression, scope) { expressions.push({expression: copy(expression), scope: copy(scope)}); },
        fetch: async (url, init = {}) => {
            const call = {url, ...init}; if (init.body) call.payload = JSON.parse(init.body); calls.push(call);
            const method = init.method || 'GET', body = call.payload || {}, parsed = new URL(url, 'http://localhost');
            const sid = Number(parsed.searchParams.get('session_id') || body.session_id), scope = server.contexts.get(sid);
            const reply = (data, status = 200) => ({ok: status < 400, status, json: async () => copy(data)});
            if (url.startsWith('/api/presence/inbox?')) {
                inboxReads++;
                if (options.failInbox) return reply({error: 'Inbox temporarily unavailable'}, 503);
                const value = {items: [...server.items.values()].filter(row => row.session_id === sid),
                    jobs: [...server.jobs.values()].filter(row => row.session_id === sid), expression: {state: sid === 1 ? 'thoughtful' : 'neutral'}};
                if (options.inboxGate && inboxReads === 1) return options.inboxGate.promise;
                return reply(value);
            }
            if (parsed.pathname === '/api/presence/preferences') {
                const pref = server.preferences.get(sid);
                if (method === 'PATCH') {
                    if (options.failPreferences) return reply({error: 'Preferences write failed'}, 500);
                    if (body.revision !== pref.revision) return reply({error: 'Preferences revision changed'}, 409);
                    if (body.settings.provider_approval && body.settings.provider_approval !== server.provider.fingerprint) return reply({error: 'Provider identity changed; approve it again'}, 409);
                    Object.assign(pref, body.settings); pref.revision++;
                }
                return reply({preferences: pref, provider: {...server.provider, approved: pref.provider_approval === server.provider.fingerprint}});
            }
            const itemMatch = parsed.pathname.match(/^\/api\/presence\/inbox\/([^/]+)\/(read|dismiss|accept)$/);
            if (itemMatch) {
                const item = server.items.get(itemMatch[1]), action = itemMatch[2];
                assert.equal(body.session_id, item.session_id); assert.equal(body.dataset_epoch, item.dataset_epoch);
                if (action === 'read') item.read_at = '2026-10-27T13:00:00Z';
                if (action === 'dismiss') { item.state = 'dismissed'; item.dismissed_at = '2026-10-27T13:00:00Z'; }
                if (action === 'accept') { item.state = 'accepted'; item.accepted_at = '2026-10-27T13:00:00Z'; item.accepted_message_id = 42; }
                if (options.acceptGate && action === 'accept') return options.acceptGate.promise;
                return reply({item});
            }
            if (parsed.pathname === '/api/presence/check-now') {
                checkWrites++;
                let job = server.checkKeys.get(body.request_id);
                if (!job) { job = {id: 'job-' + body.request_id, ...scope, state: 'pending', kind: 'checkin', error: ''}; server.checkKeys.set(body.request_id, job); server.jobs.set(job.id, job); }
                if (options.loseCheck && checkWrites === 1) throw new TypeError('Check acknowledgment lost');
                return reply({job}, 202);
            }
            if (parsed.pathname === '/api/presence/rituals' && method === 'GET') return reply({rituals: [...server.rituals.values()].filter(row => row.session_id === sid)});
            if (parsed.pathname === '/api/presence/rituals' && method === 'POST') {
                ritualWrites++;
                const ritual = {id: 'ritual-' + server.nextRitual++, revision: 0, ...scope, kind: body.kind, minute: body.minute, weekday: body.weekday, enabled: body.enabled};
                server.rituals.set(ritual.id, ritual);
                if (options.loseRitual && ritualWrites === 1) throw new TypeError('Ritual acknowledgment lost');
                return reply({ritual}, 201);
            }
            const ritualId = parsed.pathname.match(/^\/api\/presence\/rituals\/([^/]+)$/)?.[1];
            if (ritualId) {
                const ritual = server.rituals.get(ritualId); assert.equal(body.revision, ritual.revision);
                if (method === 'DELETE') { server.rituals.delete(ritualId); return reply({status: 'deleted'}); }
                Object.assign(ritual, body); ritual.revision++; return reply({ritual});
            }
            return reply({error: 'Unknown endpoint'}, 404);
        },
    });
    require('./helpers/ordinary-page.cjs')(context);
    context.window = context; vm.runInContext(source, context);
    context.PresenceUI.configure({getContext: () => page, onAccepted: async item => accepted.push(copy(item))});
    return {api: context.PresenceUI, context, page, server, elements, calls, accepted, expressions, timers, options,
        status: () => elements.get('presenceStatus').textContent,
        fireDocument(name) { for (const fn of documentEvents[name] || []) fn(); },
        fireWindow(name) { for (const fn of windowEvents[name] || []) fn(); },
        fireWindow(name) { for (const fn of windowEvents[name] || []) fn(); },
        writes: () => calls.filter(call => call.method && call.method !== 'GET'),
        inboxReads: () => calls.filter(call => call.url.startsWith('/api/presence/inbox?')),
    };
}

test('opening and polling only read saved state; opt-in starts off and messages are never inserted or played automatically', async () => {
    const a = app(); a.server.addItem(1, 'inbox-a', {text: '<img src=x onerror=alert(1)>'}); await a.api.open();
    assert.equal(a.api.state().preferences.enabled, false); assert.equal(a.elements.get('presenceEnabled').checked, false);
    assert.equal(a.elements.get('presenceBanner').hidden, false); assert.match(a.elements.get('presenceBannerText').textContent, /has something to say/);
    assert.equal(a.elements.get('presenceInbox').children[0].children[1].textContent, '<img src=x onerror=alert(1)>');
    assert.equal(a.writes().length, 0); assert.equal(a.accepted.length, 0); assert.equal(a.server.items.get('inbox-a').read_at, null);
    assert.equal(a.expressions.length, 1); assert.equal(a.expressions[0].scope.avatar_id, 'mara');
    assert.equal([...a.timers.values()].some(timer => timer.delay === 30000), true);
});

test('restored historical work stays readable as stale without rejecting the current inbox', async () => {
    const a = app();
    a.server.addItem(1, 'restored', {dataset_epoch: 'old-epoch', state: 'stale', text: ''});
    await a.api.open();
    assert.equal(a.api.state().items.length, 1);
    assert.equal(a.api.state().items[0].state, 'stale');
    assert.equal(a.api.state().failures, 0);
});

test('settings save explicit opt-in, quiet times and exact provider approval; changed models never inherit approval', async () => {
    const a = app(); Object.assign(a.server.provider, {provider_key: 'cloud', model: 'exact-model-a', base_url: 'https://provider.example/v1', network: true});
    await a.api.open(); assert.equal(a.elements.get('presenceNetworkApproval').checked, false);
    assert.match(a.elements.get('presenceNetworkText').textContent, /cloud.*exact-model-a.*provider\.example/);
    a.elements.get('presenceEnabled').checked = true; a.elements.get('presenceNetworkApproval').checked = true;
    a.elements.get('presenceQuietStart').value = '22:00'; a.elements.get('presenceQuietEnd').value = '22:00'; await a.api.save();
    const saved = a.writes()[0].payload;
    assert.equal(saved.settings.enabled, true); assert.equal(saved.settings.provider_approval, 'provider-a');
    assert.equal(saved.settings.quiet_start, 1320); assert.equal(saved.settings.quiet_end, 1320);
    assert.equal(saved.session_id, 1); assert.equal(saved.avatar_id, 'mara'); assert.equal(saved.dataset_epoch, 'epoch-one'); assert.match(a.status(), /preferences saved/);
    Object.assign(a.server.provider, {model: 'exact-model-b', fingerprint: 'provider-b'});
    await a.api.save(); assert.match(a.status(), /Not confirmed.*Provider identity changed/);
    await a.api.loadSettings(); assert.equal(a.elements.get('presenceNetworkApproval').checked, false);
    assert.match(a.elements.get('presenceNetworkText').textContent, /exact-model-b/);
    a.elements.get('presenceEnabled').checked = false; await a.api.save();
    assert.equal(a.server.preferences.get(1).enabled, false); assert.equal(a.server.preferences.get(1).provider_approval, null);
});

test('failed preference writes do not become saved opt-in', async () => {
    const a = app({failPreferences: true}); await a.api.open(); a.elements.get('presenceEnabled').checked = true; await a.api.save();
    assert.equal(a.api.state().preferences.enabled, false); assert.equal(a.server.preferences.get(1).enabled, false);
    assert.match(a.status(), /Not confirmed.*write failed/); assert.equal(a.accepted.length, 0);
});

test('read, dismiss and explicit acceptance are distinct; acceptance reload callback runs once for the original chat', async () => {
    const a = app(); a.server.addItem(1, 'read-a'); a.server.addItem(1, 'dismiss-a'); a.server.addItem(1, 'accept-a'); await a.api.open();
    await a.api.itemAction('read-a', 'read'); assert.equal(a.server.items.get('read-a').state, 'available'); assert.equal(a.accepted.length, 0);
    await a.api.itemAction('dismiss-a', 'dismiss'); assert.equal(a.server.items.get('dismiss-a').state, 'dismissed'); assert.equal(a.accepted.length, 0);
    const first = a.api.itemAction('accept-a', 'accept'), second = a.api.itemAction('accept-a', 'accept'); await first; await second;
    assert.equal(a.accepted.length, 1); assert.equal(a.accepted[0].session_id, 1); assert.equal(a.accepted[0].accepted_message_id, 42);
    assert.equal(a.writes().filter(call => call.url.endsWith('/accept')).length, 1);
    assert.equal(a.elements.get('presenceBanner').hidden, true);
});

test('expired items and failed or stale work remain visible without acceptance', async () => {
    const a = app(); a.server.addItem(1, 'expired-a', {state: 'expired', error: 'Context expired'});
    a.server.jobs.set('failed-a', {id: 'failed-a', ...a.server.contexts.get(1), state: 'failed', kind: 'weekly', error: 'Local model unavailable'});
    await a.api.open(); await a.api.itemAction('expired-a', 'accept');
    assert.equal(a.writes().length, 0); assert.equal(a.accepted.length, 0); assert.match(a.status(), /unavailable or expired/);
    assert.match(a.elements.get('presenceJobs').children[0].textContent, /failed.*Local model unavailable/);
    assert.match(a.elements.get('presenceInbox').children[0].children[0].textContent, /expired/);
});

for (const changed of ['companion', 'epoch']) {
    test(`late inbox and expression responses from an old ${changed} cannot render`, async () => {
        const gate = deferred(), a = app({inboxGate: gate}); const old = copy(a.server.addItem(1, 'old-a'));
        const first = a.api.refresh(); await tick();
        if (changed === 'companion') { a.page.sessionId = 2; a.page.companionId = 'ana'; a.server.addItem(2, 'new-a'); }
        else {
            a.page.datasetEpoch = 'epoch-two'; a.server.contexts.get(1).dataset_epoch = 'epoch-two'; a.server.preferences.get(1).dataset_epoch = 'epoch-two';
            a.server.items.clear(); a.server.addItem(1, 'new-a');
        }
        await a.api.refresh();
        gate.resolve({ok: true, status: 200, json: async () => ({items: [old], jobs: [], expression: {state: 'wrong-old-expression'}})}); await first;
        assert.equal(a.api.state().items.length, 1); assert.equal(a.api.state().items[0].id, 'new-a');
        assert.equal(a.expressions.length, 1); assert.notEqual(a.expressions[0].expression.state, 'wrong-old-expression');
        assert.equal(a.api.state().context.dataset_epoch, a.page.datasetEpoch);
    });
}

test('a late accepted response after navigation cannot reload another companion’s chat', async () => {
    const gate = deferred(), a = app({acceptGate: gate}); a.server.addItem(1, 'old-a'); a.server.addItem(2, 'new-a'); await a.api.open();
    const accepting = a.api.itemAction('old-a', 'accept'); await tick();
    a.page.sessionId = 2; a.page.companionId = 'ana'; await a.api.refresh();
    gate.resolve({ok: true, status: 200, json: async () => ({item: copy(a.server.items.get('old-a'))})}); await accepting;
    assert.equal(a.accepted.length, 0); assert.equal(a.api.state().items[0].id, 'new-a');
    assert.equal(a.server.items.get('old-a').state, 'accepted', 'the server action remains bound to its original chat');
});

test('duplicate refreshes share one read, hidden pages stop polling and late hidden responses are ignored', async () => {
    const gate = deferred(), a = app({inboxGate: gate}); a.server.addItem(1, 'inbox-a');
    const first = a.api.refresh(), again = a.api.refresh(); await tick(); assert.equal(a.inboxReads().length, 1);
    a.context.document.hidden = true; a.fireDocument('visibilitychange');
    assert.equal(a.calls[0].signal.aborted, true); assert.equal(a.timers.size, 0);
    gate.resolve({ok: true, status: 200, json: async () => ({items: [copy(a.server.items.get('inbox-a'))], jobs: [], expression: {state: 'late'}})});
    await first; await again; assert.equal(a.expressions.length, 0); assert.equal(a.elements.get('presenceBanner').hidden, true);
    await a.api.refresh(); assert.equal(a.inboxReads().length, 1);
    a.context.document.hidden = false; a.fireDocument('visibilitychange'); await tick();
    assert.equal(a.inboxReads().length, 2); assert.equal(a.expressions.length, 1); assert.equal(a.writes().length, 0);
});

test('read failures back off and pause after five errors until explicit refresh', async () => {
    const a = app({failInbox: true});
    for (let count = 0; count < 5; count++) await a.api.refresh(false);
    assert.equal(a.api.state().failures, 5); assert.equal(a.timers.size, 0); assert.match(a.status(), /Automatic refresh is paused/);
    a.options.failInbox = false; await a.api.refresh();
    assert.equal(a.api.state().failures, 0); assert.equal([...a.timers.values()][0].delay, 30000); assert.equal(a.writes().length, 0);
});

test('a lost explicit Check now response retries its stable key without creating duplicate work', async () => {
    const a = app({loseCheck: true}); await a.api.open(); await a.api.checkNow(); assert.equal(a.writes().length, 0);
    a.elements.get('presenceEnabled').checked = true; await a.api.save();
    await a.api.checkNow(); assert.match(a.status(), /acknowledgment lost/); await a.api.checkNow();
    const checks = a.writes().filter(call => call.url.endsWith('/check-now'));
    assert.equal(checks.length, 2); assert.equal(checks[0].payload.request_id, checks[1].payload.request_id);
    assert.equal(a.server.jobs.size, 1); assert.equal(a.elements.get('presenceCheckNow').disabled, true); assert.equal(a.accepted.length, 0);
});

test('uncertain ritual creation requires reload before retry, and edits/deletes use the saved revision', async () => {
    const a = app({loseRitual: true}); await a.api.open();
    a.elements.get('presenceRitualKind').value = 'weekly'; a.elements.get('presenceRitualTime').value = '18:30'; a.elements.get('presenceRitualWeekday').value = '6';
    await a.api.saveRitual(); assert.match(a.status(), /Reload saved rituals before retrying/);
    await a.api.saveRitual(); assert.equal(a.writes().filter(call => call.url === '/api/presence/rituals').length, 1);
    await a.api.loadSettings(); assert.equal(a.api.state().rituals.length, 1);
    const ritual = a.api.state().rituals[0]; a.api.editRitual(ritual.id); a.elements.get('presenceRitualTime').value = '19:00';
    await a.api.saveRitual(); assert.equal(a.server.rituals.get(ritual.id).minute, 1140); assert.equal(a.server.rituals.get(ritual.id).revision, 1);
    await a.api.deleteRitual(ritual.id); assert.equal(a.server.rituals.size, 0);
    assert.ok(a.writes().every(call => call.payload.avatar_id === 'mara'));
    assert.equal(a.writes().at(-1).payload.revision, 1); assert.equal(a.server.preferences.get(1).enabled, false);
    assert.equal(a.writes().filter(call => call.url.endsWith('/check-now')).length, 0);
});
