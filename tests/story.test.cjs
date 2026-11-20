const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto, createHash} = require('node:crypto');

const source = fs.readFileSync('static/js/story.js', 'utf8');
const template = fs.readFileSync('templates/story.html', 'utf8');
const copy = value => structuredClone(value);
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; }

class Element {
    constructor(tag = 'div', owner = null) { this.tag = tag; this.owner = owner; this.children = []; this.listeners = {}; this.value = ''; this.textContent = ''; this.checked = false; this.disabled = false; this.hidden = false; this.open = false; this.attributes = {}; this.removed = false; }
    set innerHTML(_) { throw new Error('Story data must be rendered as text'); }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(name, value) { this.attributes[name] = value; }
    removeAttribute(name) { delete this.attributes[name]; }
    focus() { if (this.owner) this.owner.activeElement = this; }
    remove() { this.removed = true; }
    showModal() { this.open = true; }
    close() { this.open = false; for (const callback of this.listeners.close || []) callback(); }
    async fire(name) { for (const callback of this.listeners[name] || []) await callback({preventDefault() {}, target: this}); }
}
const contents = node => [node.textContent, ...node.children.map(contents)].join('\n');
const descendants = node => [node, ...node.children.flatMap(descendants)];
const findButton = (node, label) => descendants(node).find(item => item.tag === 'button' && item.textContent === label);

function world() {
    const contexts = new Map([[1, {session_id: 1, avatar_id: 'mara', dataset_epoch: 'epoch-a'}], [2, {session_id: 2, avatar_id: 'ana', dataset_epoch: 'epoch-a'}]]);
    const preferences = new Map([...contexts].map(([sid, scope]) => [sid, {...scope, enabled: false, revision: 1, timezone: 'UTC', interval_minutes: 1440, cooldown_minutes: 1440,
        daily_cap: 1, quiet_enabled: true, quiet_start: 1320, quiet_end: 480, provider_approval: null, needs_review: false}]));
    const provider = {provider_key: 'local', base_url: 'http://127.0.0.1:1234/v1', model: 'local-model', fingerprint: 'provider-a', network: false, approved: false};
    const episodes = new Map(), jobs = new Map(), notes = new Map(), messages = new Map(), requests = new Map(), restored = new Map();
    const scopes = [{id: 'mara', name: 'Mara', session_id: 1}, {id: 'ana', name: 'Ana', session_id: 2}, {id: '_default', name: 'Default companion', session_id: null}];
    function episode(sid, id, changes = {}) { const row = {id, ...contexts.get(sid), title: `Episode for ${sid}`, summary: 'Words exchanged', uncertainty: '', revision: 1, kind: 'actual_exchange', curation: 'manual_saved', salience: 3,
        in_story: true, pinned: false, confidence: null, status: 'current', eligible: true, created_at: '2026-10-28T12:00:00Z', ranking: {score: .5, recency: 1, half_life_days: 30},
        sources: [{message_id: 10, session_id: sid, role: 'user', start: 0, end: 12, sha256: 'hash', status: 'current', excerpt: 'User words'}], ...changes}; episodes.set(id, row); return row; }
    messages.set(10, {id: 10, session_id: 1, role: 'user', content: 'A source 🙂 with <img src=x onerror=boom>'});
    messages.set(20, {id: 20, session_id: 2, role: 'user', content: 'Private source for Ana'});
    function retained(sid, id, changes = {}) {
        const saved = copy(episode(sid, id, {dataset_epoch: 'epoch-before-restore', revision: 4, kind: 'imagined_scene', curation: 'manual_corrected',
            title: 'A scene saved by you', summary: 'An imagined garden <img onerror=boom>', uncertainty: 'Fiction, not a shared event', pinned: true, in_story: true, salience: 5,
            sources: [{message_id: 10, session_id: sid, role: 'user', start: 2, end: 12, sha256: 'a'.repeat(64), status: 'current', excerpt: '<script>reviewed source</script>'}], ...changes}));
        restored.set(id, saved);
        Object.assign(episodes.get(id), {status: 'stale', title: '', summary: '', uncertainty: '', eligible: false,
            sources: saved.sources.map(ref => ({...ref, status: 'unavailable', excerpt: ''}))});
        return saved;
    }
    return {contexts, preferences, provider, episodes, jobs, notes, messages, requests, scopes, episode, retained, restored};
}

function app(options = {}) {
    const server = options.server || world(), elements = new Map(), events = {}, documentEvents = {}, timers = new Map(), calls = [], callbacks = [];
    let timerId = 0, episodeReads = 0, createWrites = 0, runWrites = 0, sourceReads = 0;
    const document = {readyState: 'loading', body: null, activeElement: null, hidden: false,
        getElementById: value => elements.get(value), createElement: tag => new Element(tag, document), addEventListener(name, callback) { (documentEvents[name] ||= []).push(callback); }};
    document.body = new Element('body', document); document.activeElement = new Element('button', document);
    for (const match of template.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"[^>]*>/g)) elements.set(match[2], new Element(match[1], document));
    elements.get('storyFilter').value = 'story'; elements.get('storyKind').value = 'actual_exchange'; elements.get('storySalience').value = '3'; elements.get('storyInStory').checked = true;
    const context = vm.createContext({document, crypto: options.crypto || webcrypto, AbortController, TextEncoder, Intl, Date, console, URL,
        __DATA_EPOCH__: 'epoch-a', location: {href: options.url || 'http://localhost/story?scope=mara'}, URLSearchParams, confirm: options.confirm || (() => true),
        Audio: class { constructor() { throw new Error('Story must never play audio'); } },
        localStorage: {getItem() { throw new Error('Story data must not be persisted in browser storage'); }, setItem() { throw new Error('Story data must not be persisted in browser storage'); }},
        setTimeout(callback, delay) { const id = ++timerId; timers.set(id, {callback, delay}); return id; }, clearTimeout(id) { timers.delete(id); },
        addEventListener(name, callback) { (events[name] ||= []).push(callback); },
        fetch: async (url, init = {}) => {
            const payload = init.body ? JSON.parse(init.body) : null, method = init.method || 'GET'; calls.push({url, ...init, payload});
            const parsed = new URL(url, 'http://localhost'), sid = Number(parsed.searchParams.get('session_id') || payload?.session_id), scope = server.contexts.get(sid);
            const reply = (data, status = 200) => ({ok: status < 400, status, json: async () => copy(data)});
            if (options.intercept) { const intercepted = await options.intercept({url, method, payload, server, reply, calls}); if (intercepted !== undefined) return intercepted; }
            if (parsed.pathname === '/api/knowledge/scopes') return reply({scopes: server.scopes, active: 'mara'});
            if (parsed.pathname === '/api/episodes' && method === 'GET') {
                episodeReads++;
                if (options.episodesGate && episodeReads === 1) return options.episodesGate.promise;
                if (options.failEpisodes) return reply({error: 'Episodes unavailable'}, 503);
                return reply({scope, episodes: [...server.episodes.values()].filter(row => row.avatar_id === scope.avatar_id && row.status !== 'forgotten'), recall_after: 0, repair_pending: false,
                    recall_budget: {available: true, daily_used: 0, daily_limit: 2, minimum_turn_spacing: 5}});
            }
            if (parsed.pathname === '/api/episodes/sources') {
                sourceReads++;
                if (options.failSourceChoices) return reply({error: 'Recent messages are unavailable'}, 503);
                if (options.sourceChoicesGate && sourceReads === 1) return options.sourceChoicesGate.promise;
                return reply({scope, sources: [...server.messages.values()].filter(row => row.session_id === sid && ['user', 'assistant'].includes(row.role)).slice(0, 60).map(row => ({message_id: row.id, session_id: sid, role: row.role, preview: Array.from(row.content).slice(0, 120).join(''), timestamp: row.timestamp || '2026-10-28T12:00:00Z'})), repair_pending: false, recall_after: 0});
            }
            if (parsed.pathname === '/api/reflection/preferences') {
                const pref = server.preferences.get(sid);
                if (method === 'PATCH') {
                    assert.equal(payload.avatar_id, scope.avatar_id); assert.equal(payload.dataset_epoch, scope.dataset_epoch); assert.equal(payload.revision, pref.revision);
                    if (options.failPreferences) return reply({error: 'Preference write failed'}, 503);
                    if (payload.settings.provider_approval && payload.settings.provider_approval !== server.provider.fingerprint) return reply({error: 'Provider identity changed; approve again'}, 409);
                    Object.assign(pref, payload.settings); pref.revision++;
                }
                return reply({preferences: pref, provider: {...server.provider, approved: pref.provider_approval === server.provider.fingerprint}});
            }
            if (parsed.pathname === '/api/reflection/reports') return reply({scope, jobs: [...server.jobs.values()].filter(row => row.session_id === sid), reports: [],
                current_notebook: {entries: [...server.notes.values()].filter(row => row.session_id === sid)}});
            const messageId = parsed.pathname.match(/^\/api\/knowledge\/messages\/(\d+)$/)?.[1];
            if (messageId) { const message = server.messages.get(Number(messageId)); return message ? reply({scope: server.contexts.get(message.session_id).avatar_id, message}) : reply({error: 'Message not found'}, 404); }
            if (parsed.pathname === '/api/episodes' && method === 'POST') {
                createWrites++; assert.equal(payload.avatar_id, scope.avatar_id); assert.equal(payload.dataset_epoch, scope.dataset_epoch);
                let row = server.requests.get(payload.request_id);
                if (!row) { row = server.episode(sid, 'created-' + server.requests.size, {...payload, sources: payload.sources.map(ref => ({...ref, session_id: sid, role: 'user', status: 'current', excerpt: 'reviewed source'}))}); server.requests.set(payload.request_id, row); }
                if (options.loseCreate && createWrites === 1) throw new TypeError('Save acknowledgment lost');
                return reply({scope, episode: row}, 201);
            }
            const restoreReviewId = parsed.pathname.match(/^\/api\/episodes\/([^/]+)\/restore-review$/)?.[1];
            if (restoreReviewId) {
                const row = server.episodes.get(restoreReviewId), saved = server.restored.get(restoreReviewId);
                assert.equal(method, 'GET'); assert.equal(parsed.searchParams.get('avatar_id'), scope.avatar_id); assert.equal(parsed.searchParams.get('dataset_epoch'), scope.dataset_epoch); assert.equal(Number(parsed.searchParams.get('revision')), row.revision);
                if (options.failRestoreReview) return reply({error: 'Matching source review unavailable'}, 409);
                const data = {scope, episode: {...saved, status: 'review_ready'}, sources: saved.sources,
                    reviewed_sources: saved.sources.map(({message_id, session_id, role, start, end, sha256}) => ({message_id, session_id, role, start, end, sha256}))};
                if (options.restoreReviewGate) return options.restoreReviewGate.promise;
                return reply(data);
            }
            const restoreCommitId = parsed.pathname.match(/^\/api\/episodes\/([^/]+)\/revalidate$/)?.[1];
            if (restoreCommitId) {
                const row = server.episodes.get(restoreCommitId), saved = server.restored.get(restoreCommitId);
                assert.equal(method, 'POST'); assert.equal(payload.avatar_id, scope.avatar_id); assert.equal(payload.dataset_epoch, scope.dataset_epoch);
                assert.equal(payload.revision, row.revision); assert.equal(payload.confirm, 'restore-curation');
                assert.deepEqual(payload.reviewed_sources, saved.sources.map(({message_id, session_id, role, start, end, sha256}) => ({message_id, session_id, role, start, end, sha256})));
                if (options.failRestoreCommit) return reply({error: 'The exact source context changed'}, 409);
                Object.assign(row, saved, scope, {status: 'current', eligible: true, revision: row.revision + 1});
                if (options.restoreCommitGate) return options.restoreCommitGate.promise;
                return reply({scope, episode: row});
            }
            const episodeId = parsed.pathname.match(/^\/api\/episodes\/([^/]+)$/)?.[1];
            if (episodeId) {
                const row = server.episodes.get(episodeId); assert.equal(payload.avatar_id, scope.avatar_id); assert.equal(payload.revision, row.revision);
                if (options.failPatch) return reply({error: 'Episode write failed'}, 503);
                if (method === 'DELETE') { assert.equal(payload.confirm, 'forget'); row.status = 'forgotten'; } else Object.assign(row, payload, {curation: 'manual_corrected'});
                row.revision++;
                if (options.patchGate) return options.patchGate.promise;
                return reply({scope, episode: row});
            }
            if (parsed.pathname === '/api/reflection/run') {
                runWrites++; assert.equal(payload.avatar_id, scope.avatar_id); assert.equal(payload.dataset_epoch, scope.dataset_epoch);
                let job = server.requests.get(payload.request_id);
                if (!job) { job = {id: 'job-' + payload.request_id, ...scope, state: 'pending', due_at: '2026-10-28T13:00:00Z', error: ''}; server.requests.set(payload.request_id, job); server.jobs.set(job.id, job); }
                if (options.loseRun && runWrites === 1) throw new TypeError('Run acknowledgment lost');
                return reply({job}, 202);
            }
            const noteId = parsed.pathname.match(/^\/api\/reflection\/notebook\/([^/]+)$/)?.[1];
            if (noteId) { const entry = server.notes.get(noteId); assert.equal(payload.avatar_id, scope.avatar_id); assert.equal(payload.dataset_epoch, scope.dataset_epoch); assert.equal(payload.revision, entry.revision);
                if (payload.dismiss) entry.state = 'dismissed'; else Object.assign(entry, payload, {user_edited: true}); entry.revision++; return reply({entry}); }
            return reply({error: 'Unknown endpoint'}, 404);
        },
    });
    const page = require('./helpers/ordinary-page.cjs')(context);
    context.window = context; vm.runInContext(source, context);
    return {api: context.StoryUI, context, elements, server, calls, callbacks, timers, page,
        async init() { await context.StoryUI.init({onChanged: async scope => { callbacks.push(copy(scope)); if (options.callbackFailure) throw new Error('Host failed'); }}); },
        status: () => elements.get('storyStatus').textContent, reflectionStatus: () => elements.get('storyReflectionStatus').textContent,
        writes: () => calls.filter(call => call.method && call.method !== 'GET'),
        fire(name, event = {}) { for (const callback of events[name] || []) callback(event); },
        dialog: () => document.body.children.filter(child => child.tag === 'dialog' && !child.removed).at(-1),
    };
}

test('opening only reads scoped saved state, never schedules a job, polls, stores data or autoplays', async () => {
    const a = app(); a.server.episode(1, 'one'); await a.init();
    assert.equal(a.calls.length, 5); assert.equal(a.writes().length, 0); assert.equal(a.timers.size, 0);
    assert.equal(a.api.state().preferences.enabled, false); assert.equal(a.elements.get('storyReflectionEnabled').checked, false);
    assert.equal(a.elements.get('storyRunReflection').disabled, true);
    assert.match(a.status(), /No reflection was requested/); assert.match(a.elements.get('storyBudget').textContent, /2 uses.*5 turns.*does not spend/);
});

test('actual words, imagined scenes and derived candidates have separate labels with confidence separate from salience', async () => {
    const a = app(); a.server.episode(1, 'real', {title: '<img onerror=boom>', summary: '<script>bad()</script>'});
    a.server.episode(1, 'fiction', {kind: 'imagined_scene', curation: 'manual_curated'});
    a.server.episode(1, 'candidate', {kind: 'actual_exchange', curation: 'reflection', confidence: .42, eligible: false}); await a.init();
    const rendered = contents(a.elements.get('storyCards'));
    assert.match(rendered, /Actual exchange/); assert.match(rendered, /Imagined scene/); assert.match(rendered, /Derived candidate/);
    assert.match(rendered, /Derived by reflection/); assert.match(rendered, /42%.*separate from importance/); assert.match(rendered, /Salience 3\/5/);
    assert.match(rendered, /<img onerror=boom>/); assert.match(rendered, /<script>bad/); assert.match(rendered, /SHA-256: hash/);
    assert.match(template, /does not establish that a described life event happened/);
});

test('foreign scope data and foreign episode rows are rejected without rendering their text', async () => {
    for (const foreign of ['scope', 'row']) {
        const a = app({intercept({url, server, reply}) { if (!url.startsWith('/api/episodes?')) return; return reply({scope: server.contexts.get(foreign === 'scope' ? 2 : 1), episodes: [server.episode(2, 'secret', {title: 'SECRET'})]}); }});
        await a.init(); assert.equal(a.api.state().episodes.length, 0); assert.doesNotMatch(contents(a.elements.get('storyCards')), /SECRET/); assert.match(a.status(), /different|outside/);
    }
});

for (const change of ['companion', 'epoch']) {
    test(`late episode responses from an old ${change} cannot restore old content`, async () => {
        const gate = deferred(), a = app({episodesGate: gate}), old = copy(a.server.episode(1, 'old', {title: 'OLD PRIVATE'}));
        const loading = a.init(); await tick();
        if (change === 'companion') { a.server.episode(2, 'new', {title: 'NEW'}); await a.api.selectScope('ana'); }
        else { a.context.__DATA_EPOCH__ = 'epoch-b'; for (const context of a.server.contexts.values()) context.dataset_epoch = 'epoch-b'; for (const pref of a.server.preferences.values()) pref.dataset_epoch = 'epoch-b'; a.server.episodes.clear(); a.server.episode(1, 'new', {title: 'NEW'}); await a.api.selectScope('mara'); }
        gate.resolve({ok: true, status: 200, json: async () => ({scope: {...old}, episodes: [old]})}); await loading;
        assert.equal(a.api.state().episodes[0].id, 'new'); assert.doesNotMatch(contents(a.elements.get('storyCards')), /OLD PRIVATE/); assert.equal(a.writes().length, 0);
    });
}

test('refresh failure clears old displayed episodes and leaves an actionable error', async () => {
    const a = app(); a.server.episode(1, 'one', {title: 'OLD'}); await a.init(); a.server.episodes.clear(); a.calls.length = 0;
    a.server.contexts.get(1).avatar_id = 'other'; await a.api.refresh();
    assert.equal(a.api.state().episodes.length, 0); assert.doesNotMatch(contents(a.elements.get('storyCards')), /OLD/); assert.match(a.status(), /different/);
});

test('companion selector never opens or creates conversations and query session cannot bypass listed ownership', async () => {
    const a = app({url: 'http://localhost/story?scope=mara&session_id=2'}); await a.init();
    assert.equal(a.api.state().context.session_id, 1); await a.api.selectScope('_default');
    assert.equal(a.api.state().context.session_id, null); assert.equal(a.elements.get('storyCreate').disabled, true); assert.match(a.status(), /No saved conversation/); assert.equal(a.writes().length, 0);
});

test('source review validates owner and session, hashes UTF-8, and uses Unicode codepoint ranges', async () => {
    const a = app(); await a.init(); a.elements.get('storySourceOne').value = '20'; await a.api.reviewSources();
    assert.match(a.status(), /not available/); assert.doesNotMatch(contents(a.elements.get('storySourcePreview')), /Private source/);
    a.server.messages.get(10).content = '🙂'.repeat(2001); a.elements.get('storySourceOne').value = '10'; await a.api.reviewSources();
    a.elements.get('storyTitle').value = 'A reviewed exchange'; await a.api.createEpisode();
    const saved = a.writes()[0].payload; assert.equal(saved.avatar_id, 'mara'); assert.equal(saved.session_id, 1); assert.equal(saved.dataset_epoch, 'epoch-a');
    assert.equal(saved.sources[0].end, 2000); assert.equal(saved.sources[0].sha256, createHash('sha256').update('🙂'.repeat(2001)).digest('hex'));
    assert.equal(a.callbacks.length, 1);
});

test('saving needs deliberate source review and duplicate source IDs never save', async () => {
    const a = app(); await a.init(); a.elements.get('storyTitle').value = 'Title'; a.elements.get('storySourceOne').value = '10';
    await a.api.createEpisode(); assert.match(a.status(), /Review/); assert.equal(a.writes().length, 0);
    a.elements.get('storySourceTwo').value = '10'; await a.api.reviewSources(); assert.match(a.status(), /different messages/); assert.equal(a.writes().length, 0);
});

test('an unacknowledged creation freezes the reviewed payload and retry retrieves one episode', async () => {
    const a = app({loseCreate: true}); await a.init(); a.elements.get('storySourceOne').value = '10'; a.elements.get('storyTitle').value = 'Original title'; await a.api.reviewSources();
    assert.equal(await a.api.createEpisode(), false); assert.match(a.status(), /Retry the same save/); assert.equal(a.elements.get('storyTitle').disabled, true);
    a.elements.get('storyTitle').value = 'Edited after failure'; assert.equal(await a.api.createEpisode(), true);
    const writes = a.writes(); assert.equal(writes[0].payload.request_id, writes[1].payload.request_id); assert.equal(writes[1].payload.title, 'Original title'); assert.equal(a.server.episodes.size, 1); assert.equal(a.callbacks.length, 1);
});

test('pin and curation writes carry revision/owner/epoch and ignore double clicks', async () => {
    const gate = deferred(), a = app({patchGate: gate}); a.server.episode(1, 'one'); await a.init();
    const saving = a.api.patchEpisode('one', {pinned: true}), duplicate = a.api.patchEpisode('one', {pinned: true}); await tick();
    assert.equal(a.writes().length, 1); const write = a.writes()[0].payload; assert.equal(write.revision, 1); assert.equal(write.avatar_id, 'mara'); assert.equal(write.dataset_epoch, 'epoch-a');
    gate.resolve({ok: true, status: 200, json: async () => ({scope: a.server.contexts.get(1), episode: a.server.episodes.get('one')})}); await saving; await duplicate;
    assert.equal(a.api.state().episodes[0].pinned, true); assert.equal(a.callbacks.length, 1);
});

test('failed episode changes keep saved state and block another write until a full refresh', async () => {
    const a = app({failPatch: true}); a.server.episode(1, 'one'); await a.init(); await a.api.patchEpisode('one', {pinned: true});
    assert.equal(a.api.state().episodes[0].pinned, false); assert.equal(a.callbacks.length, 0); assert.equal(a.api.state().uncertain, true); assert.match(a.status(), /Not confirmed.*write failed/);
    await a.api.reloadReflection(); await a.api.patchEpisode('one', {pinned: true}); assert.equal(a.writes().length, 1, 'reloading preferences alone cannot clear an uncertain episode write');
    await a.api.refresh(); assert.equal(a.api.state().uncertain, false);
});

test('late mutation acknowledgment cannot notify or render into a newly selected companion', async () => {
    const gate = deferred(), a = app({patchGate: gate}); a.server.episode(1, 'one'); a.server.episode(2, 'two'); await a.init();
    const saving = a.api.patchEpisode('one', {pinned: true}); await tick(); await a.api.selectScope('ana');
    gate.resolve({ok: true, status: 200, json: async () => ({scope: a.server.contexts.get(1), episode: a.server.episodes.get('one')})}); await saving;
    assert.equal(a.callbacks.length, 0); assert.equal(a.api.state().episodes[0].id, 'two'); assert.equal(a.api.state().busy, false);
});

test('reflection opt-in and exact network approval save separately; changed provider cannot inherit approval', async () => {
    const a = app(); Object.assign(a.server.provider, {network: true, provider_key: 'network-provider', base_url: 'https://provider.example/v1', model: 'model-a'}); await a.init();
    assert.equal(a.elements.get('storyNetworkApproval').checked, false); a.elements.get('storyReflectionEnabled').checked = true; a.elements.get('storyNetworkApproval').checked = true;
    await a.api.saveReflection(); const saved = a.writes()[0].payload;
    assert.equal(saved.settings.enabled, true); assert.equal(saved.settings.provider_approval, 'provider-a'); assert.equal(saved.revision, 1); assert.equal(saved.avatar_id, 'mara'); assert.equal(saved.dataset_epoch, 'epoch-a');
    assert.equal(a.calls.some(call => call.url.includes('/presence/')), false); assert.equal(a.calls.some(call => call.url.includes('/reflection/run')), false);
    Object.assign(a.server.provider, {fingerprint: 'provider-b', model: 'model-b'}); await a.api.saveReflection(); assert.match(a.reflectionStatus(), /Not confirmed.*Provider identity changed/);
    await a.api.refresh(); assert.equal(a.elements.get('storyNetworkApproval').checked, false); assert.match(a.elements.get('storyNetworkText').textContent, /model-b/);
});

test('failed opt-in is never displayed as saved or used to queue inference', async () => {
    const a = app({failPreferences: true}); await a.init(); a.elements.get('storyReflectionEnabled').checked = true; await a.api.saveReflection(); await a.api.runReflection();
    assert.equal(a.api.state().preferences.enabled, false); assert.equal(a.server.preferences.get(1).enabled, false); assert.equal(a.writes().length, 1); assert.match(a.reflectionStatus(), /Not confirmed.*Preference write failed/);
});

test('reflection run retries reuse the same job ID and reports only refresh on deliberate reads', async () => {
    const a = app({loseRun: true}); a.server.preferences.get(1).enabled = true; await a.init();
    assert.equal(a.writes().length, 0); await a.api.runReflection(); assert.match(a.reflectionStatus(), /Retry requests the same job/);
    await a.api.runReflection(); await a.api.runReflection(); const writes = a.writes(); assert.equal(writes.length, 2); assert.equal(writes[0].payload.request_id, writes[1].payload.request_id); assert.equal(a.server.jobs.size, 1);
    assert.equal(a.timers.size, 0); const prior = a.calls.length; await a.api.refreshReports(); assert.equal(a.calls.length, prior + 1); assert.equal(a.writes().length, 2);
});

test('derived notebook remains visibly derived, editable and dismissible with provenance', async () => {
    const a = app(); a.server.notes.set('note-a', {id: 'note-a', ...a.server.contexts.get(1), note: 'about_you', text: '<b>Derived content</b>', uncertainty: 'May be wrong', confidence: .6,
        derived: true, user_edited: false, state: 'current', revision: 3, sources: [{message_id: 10, session_id: 1, role: 'user', status: 'current', sha256: 'hash', start: 0, end: 3}]}); await a.init();
    assert.match(contents(a.elements.get('storyReports')), /Derived summary.*generated by reflection/); assert.match(contents(a.elements.get('storyReports')), /<b>Derived content<\/b>/);
    await a.api.patchNote('note-a', {text: 'My correction', uncertainty: 'Still uncertain'}); const saved = a.writes()[0].payload; assert.equal(saved.revision, 3); assert.equal(saved.avatar_id, 'mara');
    assert.equal(a.api.state().notebook[0].user_edited, true); assert.match(contents(a.elements.get('storyReports')), /corrected by you/);
    await a.api.patchNote('note-a', {dismiss: true}); assert.equal(a.api.state().notebook.length, 0); assert.equal(a.server.notes.get('note-a').state, 'dismissed');
});

test('Forget requires a native review dialog, restores focus, and no callback can undo a saved action', async () => {
    const a = app({callbackFailure: true}); a.server.episode(1, 'one'); await a.init(); const opener = a.context.document.activeElement;
    a.api.forgetEpisode('one'); const dialog = a.dialog(); assert.equal(dialog.open, true); assert.equal(dialog.attributes['aria-labelledby'], 'storyActionTitle'); assert.equal(a.writes().length, 0);
    const form = dialog.children.find(child => child.tag === 'form'); await form.fire('submit');
    assert.equal(a.writes()[0].payload.confirm, 'forget'); assert.equal(a.api.state().episodes.length, 0); assert.equal(dialog.removed, true); assert.equal(a.context.document.activeElement, opener); assert.match(a.status(), /forgotten/);
});

test('dataset change withholds evidence, preserves historical edits and prevents stale writes', async () => {
    const a = app(); a.server.episode(1, 'one', {summary: 'PRIVATE OLD TEXT'}); await a.init(); a.elements.get('storyTitle').value = 'Old draft';
    a.context.__DATA_EPOCH__ = 'epoch-b'; a.fire('altera:dataset-changed');
    assert.equal(a.api.state().context.dataset_epoch, 'epoch-a'); assert.equal(a.elements.get('storyTitle').value, 'Old draft'); assert.doesNotMatch(contents(a.elements.get('storyCards')), /PRIVATE OLD TEXT/);
    await a.api.patchEpisode('one', {pinned: true}); assert.equal(a.writes().length, 0); assert.equal(a.timers.size, 0);
});

test('stale source and episode text are withheld even when a malformed response includes them', async () => {
    const a = app(); a.server.episode(1, 'stale', {status: 'stale', title: 'STALE SECRET', summary: 'STALE SECRET', sources: [{status: 'changed', excerpt: 'STALE SOURCE SECRET', message_id: 10}]}); await a.init();
    const rendered = contents(a.elements.get('storyCards')); assert.doesNotMatch(rendered, /STALE SECRET|STALE SOURCE SECRET/); assert.match(rendered, /withheld/);
});

test('late source preview cannot fill a newly selected companion draft', async () => {
    const gate = deferred(), a = app({intercept({url}) { if (url === '/api/knowledge/messages/10') return gate.promise; }}); await a.init();
    a.elements.get('storySourceOne').value = '10'; const reviewing = a.api.reviewSources(); await tick(); await a.api.selectScope('ana');
    gate.resolve({ok: true, status: 200, json: async () => ({scope: 'mara', message: copy(a.server.messages.get(10))})}); await reviewing;
    assert.equal(a.elements.get('storySourceOne').value, ''); assert.doesNotMatch(contents(a.elements.get('storySourcePreview')), /A source/); assert.equal(a.writes().length, 0);
});

test('changing source IDs after review requires another review before capture', async () => {
    const a = app(); await a.init(); a.elements.get('storySourceOne').value = '10'; a.elements.get('storyTitle').value = 'Title'; await a.api.reviewSources();
    a.elements.get('storySourceOne').value = '20'; await a.elements.get('storySourceOne').fire('input'); await a.api.createEpisode();
    assert.match(a.status(), /Review the current source text/); assert.equal(a.writes().length, 0); assert.equal(a.elements.get('storySourcePreview').children.length, 0);
});

test('foreign derived notebook entries are rejected even under a matching report scope', async () => {
    const a = app({intercept({url, server, reply}) { if (!url.startsWith('/api/reflection/reports?')) return; return reply({scope: server.contexts.get(1), jobs: [], reports: [], current_notebook: {entries: [{id: 'foreign', ...server.contexts.get(2), state: 'current', text: 'FOREIGN PRIVATE NOTE'}]}}); }});
    await a.init(); assert.equal(a.api.state().notebook.length, 0); assert.doesNotMatch(contents(a.elements.get('storyReports')), /FOREIGN PRIVATE NOTE/); assert.match(a.status(), /another context/);
});

test('correction dialog supports source labels and can recover from local validation errors', async () => {
    const a = app(); a.server.episode(1, 'one'); await a.init(); a.api.editEpisode('one'); const dialog = a.dialog(), form = dialog.children.find(child => child.tag === 'form');
    const labels = form.children.filter(child => child.tag === 'label'), fields = labels.map(label => label.children[0]);
    const kind = fields.find(field => field.tag === 'select'), salience = fields.find(field => field.type === 'number'), submit = descendants(form).find(item => item.type === 'submit');
    assert.equal(kind.children.length, 3); kind.value = 'imagined_scene'; salience.value = '8'; await form.fire('submit'); assert.equal(a.writes().length, 0); assert.equal(submit.disabled, false);
    salience.value = '4'; await form.fire('submit'); assert.equal(a.writes()[0].payload.kind, 'imagined_scene'); assert.equal(a.writes()[0].payload.salience, 4); assert.equal(dialog.removed, true);
});

test('source inspection is deliberate, read-only, hash checked and scoped before displaying text', async () => {
    const a = app(); const original = a.server.messages.get(10), sha = createHash('sha256').update(original.content).digest('hex');
    a.server.notes.set('inspect-note', {id: 'inspect-note', ...a.server.contexts.get(1), state: 'current', note: 'about_you', text: 'Derived note', revision: 1,
        sources: [{message_id: 10, session_id: 1, role: 'user', start: 0, end: 12, sha256: sha, status: 'current'}]}); await a.init();
    assert.equal(a.calls.some(call => call.url === '/api/knowledge/messages/10'), false);
    await findButton(a.elements.get('storyReports'), 'Inspect source excerpt').fire('click'); await tick();
    const dialog = a.dialog(); assert.ok(dialog); const area = descendants(dialog).find(item => item.tag === 'textarea'); assert.equal(area.readOnly, true); assert.equal(area.value, Array.from(original.content).slice(0, 12).join(''));
    assert.equal(a.writes().length, 0); dialog.close(); original.content = 'Changed source content';
    await findButton(a.elements.get('storyReports'), 'Inspect source excerpt').fire('click'); await tick(); assert.equal(a.dialog(), undefined); assert.match(a.status(), /source changed/);
});

test('an epoch change without an event invalidates an outstanding write and callback', async () => {
    const gate = deferred(), a = app({patchGate: gate}); a.server.episode(1, 'one'); await a.init();
    const saving = a.api.patchEpisode('one', {pinned: true}); await tick(); a.context.__DATA_EPOCH__ = 'epoch-b';
    gate.resolve({ok: true, status: 200, json: async () => ({scope: a.server.contexts.get(1), episode: a.server.episodes.get('one')})}); await saving;
    assert.equal(a.api.state().context.dataset_epoch, 'epoch-a'); assert.equal(a.callbacks.length, 0); assert.equal(a.api.state().episodes.length, 0); assert.match(a.status(), /dataset changed/);
});

function restoredReview(server, episodeId = 'restored-one', sid = 1) {
    const saved = copy(server.restored.get(episodeId));
    return {scope: copy(server.contexts.get(sid)), episode: {...saved, status: 'review_ready'}, sources: copy(saved.sources),
        reviewed_sources: saved.sources.map(({message_id, session_id, role, start, end, sha256}) => ({message_id, session_id, role, start, end, sha256}))};
}

test('restored moments stay stale on open and explicit review is a scoped GET with no mutation', async () => {
    const a = app(); a.server.retained(1, 'restored-one'); await a.init();
    assert.equal(a.calls.some(call => call.url.includes('/restore-review')), false); assert.equal(a.writes().length, 0);
    assert.doesNotMatch(contents(a.elements.get('storyCards')), /An imagined garden|reviewed source/);
    await findButton(a.elements.get('storyCards'), 'Review restored moment').fire('click');
    const reviewCalls = a.calls.filter(call => call.url.includes('/restore-review')); assert.equal(reviewCalls.length, 1); assert.equal(reviewCalls[0].method, undefined);
    const query = new URL(reviewCalls[0].url, 'http://localhost').searchParams;
    assert.deepEqual(Object.fromEntries(query), {session_id: '1', avatar_id: 'mara', dataset_epoch: 'epoch-a', revision: '4'});
    const dialog = a.dialog(); assert.ok(dialog?.open); assert.match(contents(dialog), /Review restored story moment/); assert.match(contents(dialog), /imagined scene/);
    assert.match(contents(dialog), /<img onerror=boom>/); assert.match(contents(dialog), /<script>reviewed source<\/script>/); assert.match(contents(dialog), /does not confirm real-world facts/);
    assert.equal(a.api.state().episodes[0].status, 'stale'); assert.equal(a.callbacks.length, 0); assert.equal(a.writes().length, 0);
    await findButton(dialog, 'Cancel').fire('click'); assert.equal(a.dialog(), undefined); assert.equal(a.writes().length, 0);
});

test('restored curation commits only after confirmation with exact reviewed hashes/ranges and frozen ownership', async () => {
    const a = app(); const saved = a.server.retained(1, 'restored-one'); await a.init(); await a.api.reviewRestored('restored-one');
    const dialog = a.dialog(), form = dialog.children.find(child => child.tag === 'form'); assert.equal(a.writes().length, 0);
    await form.fire('submit');
    assert.equal(a.writes().length, 1); assert.equal(a.writes()[0].url, '/api/episodes/restored-one/revalidate');
    assert.deepEqual(a.writes()[0].payload, {session_id: 1, avatar_id: 'mara', dataset_epoch: 'epoch-a', revision: 4, confirm: 'restore-curation', reviewed_sources: restoredReview(a.server).reviewed_sources});
    const current = a.api.state().episodes[0]; assert.equal(current.dataset_epoch, 'epoch-a'); assert.equal(current.status, 'current'); assert.equal(current.revision, 5);
    assert.equal(current.pinned, saved.pinned); assert.equal(current.in_story, saved.in_story); assert.equal(current.curation, saved.curation); assert.equal(current.salience, saved.salience); assert.equal(current.kind, 'imagined_scene');
    assert.equal(current.summary, saved.summary); assert.equal(a.callbacks.length, 1); assert.equal(a.dialog(), undefined); assert.match(contents(a.elements.get('storyCards')), /Pinned/);
    assert.match(a.status(), /curation restored/i);
});

test('review and refresh never implicitly restore a retained moment after a failed source review', async () => {
    const a = app({failRestoreReview: true}); a.server.retained(1, 'restored-one'); await a.init();
    await a.api.reviewRestored('restored-one'); assert.equal(a.dialog(), undefined); assert.match(a.status(), /source review unavailable/);
    await a.api.refresh(); assert.equal(a.api.state().episodes[0].status, 'stale'); assert.equal(a.api.state().episodes[0].dataset_epoch, 'epoch-before-restore');
    assert.equal(a.writes().length, 0); assert.equal(a.callbacks.length, 0); assert.equal(a.timers.size, 0);
});

test('failed restored curation commit preserves stale state and requires fresh inspection before another change', async () => {
    const a = app({failRestoreCommit: true}); a.server.retained(1, 'restored-one'); await a.init(); await a.api.reviewRestored('restored-one');
    const dialog = a.dialog(), form = dialog.children.find(child => child.tag === 'form'); await form.fire('submit');
    assert.equal(a.api.state().episodes[0].status, 'stale'); assert.equal(a.api.state().episodes[0].dataset_epoch, 'epoch-before-restore'); assert.equal(a.api.state().uncertain, true);
    assert.equal(a.callbacks.length, 0); assert.equal(dialog.open, true); assert.match(contents(dialog), /not confirmed/); assert.match(a.status(), /Not confirmed.*source context changed/);
    await form.fire('submit'); assert.equal(a.writes().length, 1); dialog.close(); await a.api.refresh();
    assert.equal(a.api.state().episodes[0].status, 'stale'); assert.equal(a.writes().length, 1);
});

for (const mismatch of ['owner', 'epoch', 'episode owner']) {
    test(`a restored review response with mismatched ${mismatch} is suppressed before revealing curation`, async () => {
        const a = app({intercept({url, server, reply}) { if (!url.includes('/restore-review')) return; const data = restoredReview(server); data.episode.title = 'PRIVATE RESTORED CURATION';
            if (mismatch === 'owner') data.scope.avatar_id = 'ana'; else if (mismatch === 'epoch') data.scope.dataset_epoch = 'unrelated-epoch'; else data.episode.avatar_id = 'ana'; return reply(data); }});
        a.server.retained(1, 'restored-one'); await a.init(); await a.api.reviewRestored('restored-one');
        assert.equal(a.dialog(), undefined); assert.match(a.status(), /did not match/); assert.doesNotMatch(contents(a.context.document.body), /PRIVATE RESTORED CURATION/); assert.equal(a.writes().length, 0);
    });
}

for (const change of ['companion', 'epoch']) {
    test(`a late restored review from the old ${change} cannot open a dialog or restore data`, async () => {
        const gate = deferred(), a = app({restoreReviewGate: gate}); a.server.retained(1, 'restored-one'); await a.init(); const old = restoredReview(a.server);
        const reviewing = a.api.reviewRestored('restored-one'); await tick();
        if (change === 'companion') { a.server.episode(2, 'other-current'); await a.api.selectScope('ana'); }
        else { a.context.__DATA_EPOCH__ = 'epoch-b'; for (const scope of a.server.contexts.values()) scope.dataset_epoch = 'epoch-b'; for (const pref of a.server.preferences.values()) pref.dataset_epoch = 'epoch-b'; await a.api.selectScope('mara'); }
        gate.resolve({ok: true, status: 200, json: async () => old}); await reviewing;
        assert.equal(a.dialog(), undefined); assert.equal(a.writes().length, 0); assert.equal(a.callbacks.length, 0); assert.equal(a.api.state().context.dataset_epoch, a.context.__DATA_EPOCH__);
        assert.doesNotMatch(contents(a.context.document.body), /A scene saved by you|reviewed source/);
    });
}

test('duplicate confirmation dispatches restore curation once and a late result cannot cross companions', async () => {
    const gate = deferred(), a = app({restoreCommitGate: gate}); a.server.retained(1, 'restored-one'); a.server.episode(2, 'other-current'); await a.init(); await a.api.reviewRestored('restored-one');
    const dialog = a.dialog(), form = dialog.children.find(child => child.tag === 'form'); const first = form.fire('submit'), duplicate = form.fire('submit'); await tick();
    assert.equal(a.writes().length, 1); const acknowledged = {scope: copy(a.server.contexts.get(1)), episode: copy(a.server.episodes.get('restored-one'))};
    await a.api.selectScope('ana'); gate.resolve({ok: true, status: 200, json: async () => acknowledged}); await first; await duplicate;
    assert.equal(a.api.state().episodes[0].id, 'other-current'); assert.equal(a.callbacks.length, 0); assert.equal(a.dialog(), undefined); assert.equal(a.api.state().busy, false);
});

test('a malformed restored commit cannot render another owner under a matching outer scope', async () => {
    const a = app({intercept({url, server, reply}) { if (!url.endsWith('/revalidate')) return; const saved = copy(server.restored.get('restored-one')); return reply({scope: server.contexts.get(1), episode: {...saved, dataset_epoch: 'epoch-a', avatar_id: 'ana', status: 'current', summary: 'FOREIGN COMMIT PRIVATE'}}); }});
    a.server.retained(1, 'restored-one'); await a.init(); await a.api.reviewRestored('restored-one'); await a.dialog().children.find(child => child.tag === 'form').fire('submit');
    assert.equal(a.api.state().episodes[0].status, 'stale'); assert.equal(a.callbacks.length, 0); assert.doesNotMatch(contents(a.elements.get('storyCards')), /FOREIGN COMMIT PRIVATE/); assert.match(a.status(), /Not confirmed/);
});

test('message pickers show readable roles and Unicode previews while keeping IDs as values', async () => {
    const a = app(); a.server.messages.set(11, {id: 11, session_id: 1, role: 'assistant', content: '🌷'.repeat(101)}); await a.init();
    const first = a.elements.get('storySourceOne'), second = a.elements.get('storySourceTwo');
    assert.equal(first.tag, 'select'); assert.equal(second.tag, 'select'); assert.equal(first.value, ''); assert.equal(second.value, ''); assert.equal(first.disabled, false);
    assert.match(first.children[1].textContent, /^You · A source 🙂 with <img/); assert.equal(first.children[1].value, '10'); assert.doesNotMatch(first.children[1].textContent, /message 10|ID/);
    assert.equal(first.children[2].textContent, 'Mara · ' + '🌷'.repeat(100) + '…'); assert.equal(first.children[2].value, '11');
    assert.doesNotMatch(contents(first), /Private source for Ana/); assert.equal(a.writes().length, 0); assert.equal(a.timers.size, 0);
    assert.match(template, /First message<select/); assert.match(template, /Second message \(optional\)<select/); assert.doesNotMatch(template, /source message ID/);
});

test('a chosen message remains selected during refresh and an unchanged review stays usable', async () => {
    const a = app(); a.server.messages.set(11, {id: 11, session_id: 1, role: 'assistant', content: 'A reply'}); await a.init();
    a.elements.get('storySourceOne').value = '10'; a.elements.get('storySourceTwo').value = '11'; await a.elements.get('storySourceOne').fire('change'); await a.api.reviewSources();
    const preview = contents(a.elements.get('storySourcePreview')); await a.api.refresh();
    assert.equal(a.elements.get('storySourceOne').value, '10'); assert.equal(a.elements.get('storySourceTwo').value, '11'); assert.equal(contents(a.elements.get('storySourcePreview')), preview);
    a.elements.get('storyTitle').value = 'A selected exchange'; assert.equal(await a.api.createEpisode(), true); assert.deepEqual(a.writes()[0].payload.sources.map(row => row.message_id), [10, 11]);
});

test('a changed or unavailable selected message invalidates its reviewed evidence without losing the title', async () => {
    const a = app(); await a.init(); a.elements.get('storySourceOne').value = '10'; a.elements.get('storyTitle').value = 'My draft'; await a.api.reviewSources();
    a.server.messages.get(10).content = 'Changed current source'; await a.api.refresh();
    assert.equal(a.elements.get('storySourceOne').value, '10'); assert.equal(a.elements.get('storyTitle').value, 'My draft'); assert.equal(a.elements.get('storySourcePreview').children.length, 0);
    assert.equal(await a.api.createEpisode(), false); assert.match(a.status(), /Review the current source text/); assert.equal(a.writes().length, 0);
    await a.api.reviewSources(); a.server.messages.delete(10); await a.api.refresh();
    assert.equal(a.elements.get('storySourceOne').value, '10'); assert.match(contents(a.elements.get('storySourceOne')), /unavailable/); assert.equal(a.elements.get('storySourceOne').disabled, true); assert.equal(a.elements.get('storyReviewSources').disabled, true); assert.equal(a.elements.get('storyCreate').disabled, true);
    assert.match(a.elements.get('storySourceStatus').textContent, /No eligible messages/); assert.equal(a.elements.get('storyTitle').value, 'My draft');
});

test('unavailable source lists withhold previews and disable writes while preserving selected source IDs', async () => {
    let fail = false;
    const a = app({intercept({url, reply}) { if (fail && url.startsWith('/api/episodes/sources?')) return reply({error: 'Source list temporarily offline'}, 503); }}); await a.init();
    a.elements.get('storySourceOne').value = '10'; await a.api.reviewSources(); fail = true; await a.api.refresh();
    for (const [suffix, value] of [['SourceOne', '10'], ['SourceTwo', '']]) { const select = a.elements.get('story' + suffix); assert.equal(select.value, value); assert.equal(select.children.length, value ? 2 : 1); assert.equal(select.disabled, true); }
    assert.equal(a.elements.get('storySourcePreview').children.length, 0); assert.equal(a.elements.get('storyCreate').disabled, true); assert.match(a.elements.get('storySourceStatus').textContent, /unavailable/);
    assert.equal(a.writes().length, 0); fail = false; await a.api.refresh(); assert.equal(a.elements.get('storySourceOne').disabled, false); assert.equal(a.elements.get('storySourceOne').value, '10'); assert.equal(a.writes().length, 0);
});

for (const mismatch of ['owner', 'epoch', 'session row', 'role']) {
    test(`a source list with a mismatched ${mismatch} is rejected before exposing message previews`, async () => {
        const a = app({intercept({url, server, reply}) { if (!url.startsWith('/api/episodes/sources?')) return; const scope = copy(server.contexts.get(1));
            const row = {message_id: 10, session_id: 1, role: 'user', preview: 'FOREIGN PREVIEW PRIVATE', timestamp: '2026-10-28T12:00:00Z'};
            if (mismatch === 'owner') scope.avatar_id = 'ana'; if (mismatch === 'epoch') scope.dataset_epoch = 'epoch-before'; if (mismatch === 'session row') row.session_id = 2; if (mismatch === 'role') row.role = 'system';
            return reply({scope, sources: [row]}); }}); await a.init();
        assert.equal(a.elements.get('storySourceOne').disabled, true); assert.doesNotMatch(contents(a.elements.get('storySourceOne')), /FOREIGN PREVIEW PRIVATE/); assert.equal(a.writes().length, 0); assert.match(a.status(), /different companion/);
    });
}

for (const change of ['companion', 'epoch']) {
    test(`a late message list from the previous ${change} cannot replace the current pickers`, async () => {
        const gate = deferred(), a = app({sourceChoicesGate: gate}); const initializing = a.init(); await tick();
        const old = {scope: copy(a.server.contexts.get(1)), sources: [{message_id: 10, session_id: 1, role: 'user', preview: 'OLD PREVIEW PRIVATE', timestamp: '2026-10-28T12:00:00Z'}]};
        if (change === 'companion') await a.api.selectScope('ana');
        else { a.context.__DATA_EPOCH__ = 'epoch-b'; for (const scope of a.server.contexts.values()) scope.dataset_epoch = 'epoch-b'; for (const pref of a.server.preferences.values()) pref.dataset_epoch = 'epoch-b'; a.server.messages.get(10).content = 'New dataset message'; await a.api.selectScope('mara'); }
        gate.resolve({ok: true, status: 200, json: async () => old}); await initializing;
        const first = a.elements.get('storySourceOne'); assert.doesNotMatch(contents(first), /OLD PREVIEW PRIVATE/); assert.match(contents(first), change === 'companion' ? /Private source for Ana/ : /New dataset message/);
        assert.equal(first.value, ''); assert.equal(first.disabled, false); assert.equal(a.writes().length, 0);
    });
}

test('picker changes clear the old review and cannot select a message outside the returned list', async () => {
    const a = app(); a.server.messages.set(11, {id: 11, session_id: 1, role: 'assistant', content: 'A reply'}); await a.init();
    a.elements.get('storySourceOne').value = '10'; await a.api.reviewSources(); a.elements.get('storySourceOne').value = '11'; await a.elements.get('storySourceOne').fire('change');
    assert.equal(a.elements.get('storySourcePreview').children.length, 0); a.elements.get('storySourceOne').value = '20'; await a.api.reviewSources();
    assert.match(a.status(), /not available/); assert.equal(a.calls.some(call => call.url === '/api/knowledge/messages/20'), false); assert.equal(a.writes().length, 0);
});

test('a confirmed new episode immediately exposes usable curation actions without refreshing', async () => {
    const a = app(); await a.init(); a.elements.get('storySourceOne').value = '10'; a.elements.get('storyTitle').value = 'A moment to keep'; await a.api.reviewSources();
    assert.equal(await a.api.createEpisode(), true); assert.equal(a.api.state().busy, false);
    const cards = a.elements.get('storyCards');
    for (const label of ['Pin', 'Remove from story', 'Correct', 'Forget']) { const action = findButton(cards, label); assert.ok(action, label + ' should be available after save'); assert.equal(action.disabled, false, label + ' should be enabled immediately'); }
    await findButton(cards, 'Pin').fire('click'); assert.equal(a.writes().length, 2); assert.equal(a.api.state().episodes[0].pinned, true);
    assert.equal(findButton(a.elements.get('storyCards'), 'Unpin').disabled, false);
});

test('requesting reflection immediately leaves existing derived-note actions usable', async () => {
    const a = app(); a.server.preferences.get(1).enabled = true;
    a.server.notes.set('available-note', {id: 'available-note', ...a.server.contexts.get(1), note: 'our_story', text: 'A current derived note', state: 'current', revision: 1, sources: []});
    await a.init(); assert.equal(await a.api.runReflection(), true); assert.equal(a.api.state().busy, false);
    for (const label of ['Correct derived note', 'Dismiss derived note']) assert.equal(findButton(a.elements.get('storyReports'), label).disabled, false);
    assert.equal(a.elements.get('storyRunReflection').disabled, true, 'the queued job still prevents duplicate requests');
});

test('saved moments use local time and keep newest-first order with stable ID ties after save and restoration', async () => {
    const a = app(); a.server.episode(1, 'z-tied', {title: 'Tied Z', created_at: '2026-10-28T14:00:00Z'});
    a.server.episode(1, 'a-tied', {title: 'Tied A', created_at: '2026-10-28T14:00:00Z'});
    a.server.retained(1, 'restored-one', {title: 'Older restored', created_at: '2026-10-27T12:00:00Z'}); await a.init();
    const titles = () => a.elements.get('storyCards').children.map(card => card.children.find(child => child.tag === 'h3')?.textContent);
    assert.deepEqual(titles(), ['Tied A', 'Tied Z', 'Episode needs review']);
    assert.match(contents(a.elements.get('storyCards')), new RegExp('Saved ' + new Date('2026-10-28T14:00:00Z').toLocaleString().replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
    assert.doesNotMatch(contents(a.elements.get('storyCards')), /Saved 2026-10-28T14:00:00Z/);
    a.elements.get('storySourceOne').value = '10'; a.elements.get('storyTitle').value = 'New saved moment'; await a.api.reviewSources(); await a.api.createEpisode();
    assert.deepEqual(titles(), ['Tied A', 'Tied Z', 'New saved moment', 'Episode needs review']);
    await a.api.reviewRestored('restored-one'); await a.dialog().children.find(child => child.tag === 'form').fire('submit');
    assert.deepEqual(titles(), ['Tied A', 'Tied Z', 'New saved moment', 'Older restored']);
});

test('ordinary companion change requires discard confirmation and keeps edits when declined', async () => {
    let prompts = 0; const a = app({confirm: () => { prompts++; return false; }}); await a.init(); a.elements.get('storyTitle').value = 'Keep me';
    assert.equal(await a.api.selectScope('ana'), false); assert.equal(prompts, 1); assert.equal(a.elements.get('storyTitle').value, 'Keep me'); assert.equal(a.api.state().context.avatar_id, 'mara');
});

test('the latest companion selection wins when two are made at once', async () => {
    const a = app(); await a.init(); a.elements.get('storyTitle').value = 'Keep my original scope';
    const first = a.api.selectScope('ana'), second = a.api.selectScope('mara'); await Promise.all([first, second]);
    assert.equal(a.api.state().context.avatar_id, 'mara'); assert.equal(a.calls.some(row => row.url.includes('session_id=2')), false);
});

test('refresh preserves dirty preferences and refuses a stale revision or newly selected provider approval', async () => {
    const a = app(); Object.assign(a.server.provider, {network: true}); await a.init();
    a.elements.get('storyTimezone').value = 'America/New_York'; a.elements.get('storyNetworkApproval').checked = true;
    a.server.preferences.get(1).revision++; Object.assign(a.server.provider, {fingerprint: 'provider-b', model: 'new-model'}); await a.api.refresh();
    assert.equal(a.elements.get('storyTimezone').value, 'America/New_York'); assert.equal(a.elements.get('storyNetworkApproval').checked, true);
    assert.equal(await a.api.saveReflection(), false); assert.equal(a.writes().length, 0); assert.match(a.reflectionStatus(), /explicitly reload/);
    await a.api.reloadReflection(); assert.equal(a.elements.get('storyNetworkApproval').checked, false); assert.match(a.elements.get('storyNetworkText').textContent, /new-model/);
});

test('failed preference reads keep unsaved fields and disable saving instead of clearing them', async () => {
    let fail = false; const a = app({intercept({url, reply}) { if (fail && url.startsWith('/api/reflection/preferences')) return reply({error: 'Offline'}, 503); }}); await a.init();
    a.elements.get('storyTimezone').value = 'My unfinished zone'; a.elements.get('storyNetworkApproval').checked = true; fail = true; await a.api.refresh();
    assert.equal(a.elements.get('storyTimezone').value, 'My unfinished zone'); assert.equal(a.elements.get('storyNetworkApproval').checked, true); assert.equal(a.elements.get('storySaveReflection').disabled, true); assert.equal(a.writes().length, 0);
});

test('reflection reload refuses to overwrite text typed while its saved response was pending', async () => {
    let hold = false; const gate = deferred(), a = app({intercept({url, reply, server}) { if (hold && url.startsWith('/api/reflection/preferences')) return {ok: true, json: async () => { await gate.promise; return copy({preferences: server.preferences.get(1), provider: server.provider}); }}; }});
    await a.init(); a.elements.get('storyTimezone').value = 'First unfinished zone'; hold = true; const reload = a.api.reloadReflection(); await tick(); a.elements.get('storyTimezone').value = 'Newer unfinished zone'; gate.resolve(); await reload;
    assert.equal(a.elements.get('storyTimezone').value, 'Newer unfinished zone'); assert.match(a.reflectionStatus(), /changed during reload/); assert.equal(a.writes().length, 0);
});

test('successful preference save keeps newer local edits and uses the accepted revision for their next save', async () => {
    const gate = deferred(); let hold = true;
    const a = app({intercept({url, method, payload, server}) { if (url !== '/api/reflection/preferences' || method !== 'PATCH' || !hold) return;
        Object.assign(server.preferences.get(1), payload.settings, {revision: 2}); return {ok: true, json: async () => { await gate.promise; return copy({preferences: server.preferences.get(1), provider: server.provider}); }};
    }}); await a.init(); a.elements.get('storyTimezone').value = 'America/New_York'; const saving = a.api.saveReflection(); await tick(); a.elements.get('storyTimezone').value = 'Europe/Paris'; gate.resolve(); await saving;
    assert.equal(a.elements.get('storyTimezone').value, 'Europe/Paris'); hold = false; assert.equal(await a.api.saveReflection(), true); assert.equal(a.writes()[1].payload.revision, 2); assert.equal(a.server.preferences.get(1).timezone, 'Europe/Paris');
});

test('successful episode creation does not erase a newer form edit', async () => {
    const gate = deferred(), a = app({intercept({url, method, payload, server}) { if (url === '/api/episodes' && method === 'POST') { const episode = server.episode(1, 'new', payload); return {ok: true, json: async () => { await gate.promise; return copy({scope: server.contexts.get(1), episode}); }}; } }});
    await a.init(); a.elements.get('storySourceOne').value = '10'; a.elements.get('storyTitle').value = 'Submitted title'; await a.api.reviewSources(); const saving = a.api.createEpisode(); await tick(); a.elements.get('storyTitle').value = 'Newer title'; gate.resolve(); await saving;
    assert.equal(a.elements.get('storyTitle').value, 'Newer title'); assert.equal(a.api.state().createAttempt, null); assert.match(a.status(), /newer edits.*unsaved/); assert.equal(await a.api.createEpisode(), false); assert.equal(a.writes().length, 1);
});

test('successful correction keeps a dialog open for edits typed after submission', async () => {
    const gate = deferred(), a = app({patchGate: gate}); a.server.episode(1, 'one'); await a.init(); await a.api.editEpisode('one');
    const dialog = a.dialog(), form = dialog.children.find(row => row.tag === 'form'), input = descendants(form).find(row => row.tag === 'input'); input.value = 'Submitted correction';
    const saving = form.fire('submit'); await tick(); input.value = 'Newer correction'; gate.resolve({ok: true, json: async () => copy({scope: a.server.contexts.get(1), episode: a.server.episodes.get('one')})}); await saving;
    assert.equal(dialog.open, true); assert.equal(input.value, 'Newer correction'); assert.match(contents(dialog), /newer edits.*unsaved/); assert.equal(a.api.state().episodes[0].title, 'Submitted correction');
});
