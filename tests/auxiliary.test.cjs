const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/auxiliary.js', 'utf8');
const copy = value => structuredClone(value);
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return {promise, resolve}; };
class Element {
    constructor(tag = 'div', value = '') { this.tag = tag; this.textContent = value; this.children = []; this.value = ''; this.listeners = {}; this.style = {}; this.dataset = {}; this.className = ''; this.attributes = {}; this.classes = new Set(); this.classList = {contains: value => this.classes.has(value), toggle: (value, enabled) => { if (enabled) this.classes.add(value); else this.classes.delete(value); }}; }
    set innerHTML(_) { throw new Error('Conversation history must use text nodes'); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; this.textContent = ''; }
    addEventListener(event, callback) { (this.listeners[event] ||= []).push(callback); }
    setAttribute(name, value) { this.attributes[name] = value; }
    focus() { this.focused = true; }
    async fire(event, data = {preventDefault() {}}) { for (const callback of this.listeners[event] || []) await callback(data); }
}
const contents = node => [node?.textContent || '', ...(node?.children || []).map(contents)].join('');
const descendants = node => [node, ...(node.children || []).flatMap(descendants)];
function app(kind = 'search', options = {}) {
    const template = fs.readFileSync(`templates/${kind}.html`, 'utf8'), elements = new Map(), events = {}, calls = [], timers = new Map(), buttons = [];
    let timerId = 0;
    for (const match of template.matchAll(/<([a-z]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) elements.set(match[2], new Element(match[1]));
    for (const match of template.matchAll(/data-search-term="([^"]+)"/g)) { const button = new Element('button'); button.dataset.searchTerm = match[1]; buttons.push(button); }
    if (elements.has('limitInput')) elements.get('limitInput').value = '50';
    const document = {getElementById: id => elements.get(id), createElement: tag => new Element(tag), createTextNode: value => new Element('#text', value), querySelectorAll: () => buttons, addEventListener() {}};
    const data = {
        '/api/search/stats': {total_messages: 12, total_sessions: 2},
        '/api/search': {results: [{content: 'A Happy conversation', mood: 'happy', role: 'user', session_id: 1, timestamp: '2026-11-05T12:00:00Z'}], total_results: 1, stats: {total_messages: 12, total_sessions: 2}},
        '/api/mood/history': [{emotion: 'happy', intensity: .5, analysis: 'A remembered analysis', message_preview: 'A remembered message', timestamp: '2026-11-05T12:00:00Z'}],
        '/api/mood/trend': {trend: 'improving', dominant_mood: 'happy', count: 1, breakdown: {happy: 1}},
        '/api/personality/evolution': {people_tracked: 3, places_tracked: 4, communication_style: {formality: 'casual'}}
    };
    const context = vm.createContext({document, URLSearchParams, AbortController, console, Date, __DATA_EPOCH__: 'epoch-a',
        addEventListener(name, callback) { (events[name] ||= []).push(callback); },
        setTimeout(callback, delay) { const id = ++timerId; timers.set(id, {callback, delay}); return id; }, clearTimeout(id) { timers.delete(id); },
        fetch: async (url, init) => {
            calls.push({url, init}); const reply = (value, status = 200) => ({ok: status < 400, status, json: async () => copy(value)});
            if (options.intercept) { const response = await options.intercept({url, init, calls, data, reply}); if (response !== undefined) return response; }
            return reply(data[url.split('?')[0]]);
        }
    });
    const page = require('./helpers/ordinary-page.cjs')(context); context.window = context; vm.runInContext(source, context);
    return {api: context.AlteraAuxiliaryUI, page, context, elements, calls, timers, data, buttons, template,
        async fire(event, data = {}) { for (const callback of events[event] || []) await callback(data); },
        owner: () => page.components.get('auxiliary')};
}

test('search initialization reads only statistics, quick filters submit deliberately and parameters are encoded', async () => {
    const a = app(); await a.api.init(); assert.equal(a.calls.length, 1); assert.equal(a.calls[0].url, '/api/search/stats');
    await a.buttons[0].fire('click'); assert.equal(a.elements.get('searchInput').value, 'happy'); assert.match(contents(a.elements.get('resultsContainer')), /A Happy conversation/);
    a.elements.get('searchInput').value = 'hello & role=assistant'; a.elements.get('moodFilter').value = 'happy&limit=1'; a.elements.get('limitInput').value = '1000'; await a.api.performSearch();
    const url = new URL(a.calls.at(-1).url, 'http://local'); assert.equal(url.searchParams.get('q'), 'hello & role=assistant'); assert.equal(url.searchParams.get('mood'), 'happy&limit=1'); assert.equal(url.searchParams.get('limit'), '200');
    assert.equal(a.calls.every(call => !call.init.method), true); assert.equal(a.timers.size, 0);
});

test('search result text, labels and session IDs cannot create HTML and highlights match literal Unicode text', async () => {
    const a = app(); await a.api.init(); const attack = '<img src=x onerror=boom>';
    a.data['/api/search'].results[0] = {content: attack + ' [.*] 🙂 İ &amp;', mood: attack, role: attack, session_id: attack, timestamp: attack}; a.elements.get('searchInput').value = '[.*]'; await a.api.performSearch();
    const container = a.elements.get('resultsContainer'), nodes = descendants(container); assert.match(contents(container), /<img src=x onerror=boom>/); assert.match(contents(container), /Date unavailable/);
    assert.equal(nodes.filter(row => row.className === 'result-highlight').map(contents).join(''), '[.*]'); assert.equal(nodes.some(row => row.tag === 'img'), false); assert.ok(nodes.some(row => row.className === 'result-mood-badge mood-neutral'));
    a.elements.get('searchInput').value = '🙂'; await a.api.performSearch(); assert.equal(descendants(container).find(row => row.className === 'result-highlight').textContent, '🙂');
});

test('an older search parser cannot replace newer results or stats', async () => {
    const gate = deferred(), a = app('search', {intercept({url}) { if (url.includes('q=first')) return {ok: true, json: async () => { await gate.promise; return {results: [{content: 'OLD PRIVATE'}], total_results: 99, stats: {total_messages: 99, total_sessions: 99}}; }}; }});
    await a.api.init(); a.elements.get('searchInput').value = 'first'; const old = a.api.performSearch(); await tick(); a.elements.get('searchInput').value = 'second'; await a.api.performSearch(); gate.resolve(); await old;
    assert.doesNotMatch(contents(a.elements.get('resultsContainer')), /OLD PRIVATE/); assert.equal(a.elements.get('totalMessages').textContent, '12'); assert.equal(a.elements.get('resultCount').textContent, '1');
});

test('emptying a query invalidates pending results and older errors cannot erase a new search', async () => {
    const gate = deferred(), a = app('search', {intercept({url}) { if (url.includes('q=old')) return {ok: false, json: async () => { await gate.promise; return {error: 'Sensitive internal error'}; }}; }});
    await a.api.init(); a.elements.get('searchInput').value = 'old'; const old = a.api.performSearch(); await tick(); a.elements.get('searchInput').value = ''; await a.api.performSearch(); gate.resolve(); await old;
    assert.equal(contents(a.elements.get('resultsContainer')), ''); assert.equal(a.elements.get('resultCount').textContent, '0');
});

test('late standalone stats cannot overwrite counts from a newer search', async () => {
    const gate = deferred(), a = app('search', {intercept({url}) { if (url === '/api/search/stats') return {ok: true, json: async () => { await gate.promise; return {total_messages: 999, total_sessions: 999}; }}; }});
    const opening = a.api.init(); await tick(); a.elements.get('searchInput').value = 'happy'; await a.api.performSearch(); gate.resolve(); await opening; assert.equal(a.elements.get('totalMessages').textContent, '12');
});

test('timeline initializes three read-only endpoints once and renders literal historical data with bounded styles', async () => {
    const a = app('timeline'), attack = 'happy" onmouseover="boom';
    a.data['/api/mood/history'][0] = {emotion: attack, intensity: 100000, analysis: '<script>boom()</script>', message_preview: '<img onerror=boom>', timestamp: 'bad-date'};
    a.data['/api/mood/trend'].breakdown = {happy: '<img onerror=boom>'}; await a.api.init();
    assert.equal(a.calls.length, 3); assert.equal(a.calls.filter(row => row.url === '/api/personality/evolution').length, 1);
    const container = a.elements.get('timelineContainer'); assert.match(contents(container), /happy" onmouseover="boom/); assert.match(contents(container), /<script>boom/); assert.match(contents(container), /Date unavailable/);
    const fill = descendants(container).find(row => row.className === 'timeline-intensity-fill'); assert.equal(fill.style.width, '100%'); assert.equal(fill.style.background, '#6b6b7d');
    assert.equal(descendants(container).some(row => row.className.includes('onmouseover')), false); assert.doesNotMatch(contents(a.elements.get('moodBreakdown')), /<img/); assert.equal(a.elements.get('peopleCount').textContent, '3');
});

test('a newer timeline refresh wins over an old parsed response and each refresh also updates summary/statistics', async () => {
    const gate = deferred(); let reads = 0;
    const a = app('timeline', {intercept({url}) { if (url.startsWith('/api/mood/history') && ++reads === 1) return {ok: true, json: async () => { await gate.promise; return [{emotion: 'sad', analysis: 'OLD PRIVATE', timestamp: '2026-11-04'}]; }}; }});
    const opening = a.api.init(); await tick(); await a.elements.get('timelineRefresh').fire('click'); gate.resolve(); await opening;
    assert.doesNotMatch(contents(a.elements.get('timelineContainer')), /OLD PRIVATE/); assert.equal(a.calls.length, 6); assert.match(contents(a.elements.get('timelineContainer')), /remembered analysis/);
});

for (const kind of ['search', 'timeline']) test(`${kind} dataset replacement and suspension clear derived state and do not resume on pageshow`, async () => {
    const a = app(kind); await a.api.init(); if (kind === 'search') { a.elements.get('searchInput').value = 'Sensitive query'; await a.api.performSearch(); }
    a.context.__DATA_EPOCH__ = 'epoch-b'; a.fire('altera:dataset-changed'); const count = a.calls.length; a.fire('pageshow'); await assert.rejects(a.api.loadStats()); assert.equal(a.calls.length, count);
    assert.match(contents(a.elements.get(kind === 'search' ? 'resultsContainer' : 'timelineContainer')), /Reload/); assert.equal(a.elements.get(kind === 'search' ? 'totalMessages' : 'peopleCount').textContent, '');
});

test('HTTP errors remove stale derived results without exposing server exception text', async () => {
    const a = app(); await a.api.init(); a.elements.get('searchInput').value = 'happy'; await a.api.performSearch(); a.data['/api/search'] = {error: 'PRIVATE PATH AND SECRET'}; await a.api.performSearch();
    assert.match(contents(a.elements.get('resultsContainer')), /unavailable.*query/); assert.doesNotMatch(contents(a.elements.get('resultsContainer')), /PRIVATE|Happy conversation/); assert.equal(a.elements.get('resultCount').textContent, '—');
});

test('auxiliary settings lead to the authoritative chat controls and never send legacy opt-ins', async () => {
    const a = app('timeline'); await a.api.init(); await a.elements.get('auxSettings').fire('click');
    assert.equal(a.elements.get('settingsPanel').classes.has('open'), true); assert.equal(a.elements.get('settingsPanel').attributes['aria-hidden'], 'false'); assert.equal(a.elements.get('auxCloseSettings').focused, true);
    await a.fire('keydown', {key: 'Escape'}); assert.equal(a.elements.get('settingsPanel').classes.has('open'), false); assert.equal(a.elements.get('auxSettings').focused, true);
    await a.elements.get('auxSettings').fire('click'); await a.elements.get('auxCloseSettings').fire('click'); assert.equal(a.elements.get('settingsPanel').attributes['aria-hidden'], 'true');
    assert.match(a.template, /Check-ins and rituals/); assert.doesNotMatch(a.template, /proactiveToggle|ttsToggle|onclick=|onchange=/); assert.equal(a.calls.some(row => row.init.method), false);
});

test('Escape leaves search focus alone while its settings panel is already closed', async () => {
    const a = app(); await a.api.init(); await a.fire('keydown', {key: 'Escape'}); assert.equal(a.elements.get('auxSettings').focused, undefined);
});
