// Read-only conversation search and historical mood views.
window.AlteraAuxiliaryUI = (() => {
    const byId = id => document.getElementById(id);
    const node = (tag, value = '', className = '') => {
        const element = document.createElement(tag); element.textContent = String(value); element.className = className; return element;
    };
    const icons = {happy: '😊', sad: '😢', angry: '😠', anxious: '😰', excited: '🤩', calm: '😌', lonely: '🥺', grateful: '🙏', frustrated: '😤', neutral: '😐'};
    const colors = {happy: '#4ade80', sad: '#60a5fa', angry: '#f87171', anxious: '#fbbf24', excited: '#c084fc', calm: '#67e8f9', lonely: '#a78bfa', grateful: '#f472b6', frustrated: '#fb923c', neutral: '#6b6b7d'};
    const mood = value => Object.hasOwn(icons, value) ? value : 'neutral';
    const text = value => typeof value === 'string' ? value : '';
    const count = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : '—';
    const epoch = () => window.__DATA_EPOCH__ || window.AlteraAPI?.epoch || null;
    const openedEpoch = epoch(), controllers = new Map(), revisions = new Map();
    let initialized = false, suspended = false;
    const active = () => owner.active && !suspended && epoch() === openedEpoch;
    const start = area => { const serial = (revisions.get(area) || 0) + 1; revisions.set(area, serial); return () => active() && revisions.get(area) === serial; };
    const tracked = action => (...args) => { const work = owner.run(() => { if (!active()) throw new Error('Reload this page to read current saved state.'); return action(...args); }); work.catch(() => {}); return work; };
    const listen = (element, event, action) => element?.addEventListener(event, (...args) => { if (event === 'submit') args[0].preventDefault(); return tracked(action)(...args).catch(() => {}); });
    async function read(url) {
        const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20000); controllers.set(controller, timer);
        try {
            const response = await fetch(url, {cache: 'no-store', signal: controller.signal});
            const data = await response.json(); owner.assert();
            if (!active() || !response.ok || data?.error) throw new Error('Saved state is unavailable.');
            return data;
        } finally { clearTimeout(timer); controllers.delete(controller); }
    }
    function clearNodes(ids) { for (const id of ids) byId(id)?.replaceChildren(); }
    function notice(id, value) { byId(id)?.replaceChildren(node('p', value, 'timeline-loading')); }
    function renderStats(data) {
        if (byId('totalMessages')) byId('totalMessages').textContent = count(data.total_messages);
        if (byId('totalSessions')) byId('totalSessions').textContent = count(data.total_sessions);
    }
    function highlight(value, query) {
        const content = node('div', '', 'result-content'), haystack = value.toLocaleLowerCase(), needle = query.toLocaleLowerCase();
        // Use original string offsets; lowercasing Unicode can change its length.
        // A literal Unicode regex retains the original matched ranges without parsing HTML.
        const expression = query ? new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'giu') : null;
        let end = 0;
        if (needle && haystack && expression) for (const match of value.matchAll(expression)) {
            content.append(document.createTextNode(value.slice(end, match.index)), node('span', match[0], 'result-highlight')); end = match.index + match[0].length;
        }
        content.append(document.createTextNode(value.slice(end))); return content;
    }
    async function performSearch(term) {
        if (typeof term === 'string') byId('searchInput').value = term;
        const query = byId('searchInput').value.trim(), current = start('search'), statsCurrent = start('stats');
        if (!query) { clearNodes(['resultsContainer']); byId('resultCount').textContent = '0'; return; }
        const params = new URLSearchParams({q: query, limit: String(Math.max(10, Math.min(200, Number(byId('limitInput').value) || 50)))});
        for (const [id, field] of [['moodFilter', 'mood'], ['dateFrom', 'date_from'], ['dateTo', 'date_to'], ['roleFilter', 'role']]) if (byId(id).value) params.set(field, byId(id).value);
        notice('resultsContainer', 'Searching…'); byId('resultCount').textContent = '—';
        try {
            const data = await read('/api/search?' + params);
            if (!current()) return;
            if (!Array.isArray(data.results)) throw new Error('Unreadable search results.');
            byId('resultCount').textContent = count(data.total_results);
            if (data.stats && statsCurrent()) renderStats(data.stats);
            const container = byId('resultsContainer'); container.replaceChildren();
            if (!data.results.length) { notice('resultsContainer', 'No results found. Try different keywords or filters.'); return; }
            for (const result of data.results.slice(0, 200)) {
                const card = node('article', '', 'result-item'), meta = node('div', '', 'result-meta');
                if (result.mood) meta.append(node('span', icons[mood(result.mood)] + ' ' + text(result.mood), 'result-mood-badge mood-' + mood(result.mood)));
                if (result.role) meta.append(node('span', 'Role: ' + text(result.role)));
                if (result.session_id) meta.append(node('span', 'Session #' + String(result.session_id)));
                if (result.timestamp) { const date = new Date(result.timestamp); meta.append(node('span', Number.isFinite(date.getTime()) ? date.toLocaleString() : 'Date unavailable')); }
                card.append(meta, highlight(text(result.content), query)); container.append(card);
            }
        } catch (_) { if (current()) { byId('resultCount').textContent = '—'; notice('resultsContainer', 'Search is unavailable. Keep your query and try again.'); } }
    }
    async function loadStats() {
        const current = start('stats');
        try { const data = await read('/api/search/stats'); if (current()) renderStats(data); }
        catch (_) { if (current()) renderStats({}); }
    }
    async function loadTimeline() {
        const current = start('timeline'); notice('timelineContainer', 'Reading saved timeline…');
        try {
            const history = await read('/api/mood/history?limit=50'); if (!current()) return;
            if (!Array.isArray(history)) throw new Error('Unreadable timeline.');
            const grouped = new Map();
            for (const entry of history.slice(0, 50)) {
                if (!entry?.emotion) continue;
                const date = new Date(entry.timestamp || ''), known = Number.isFinite(date.getTime());
                const label = known ? date.toLocaleDateString(undefined, {weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'}) : 'Date unavailable';
                if (!grouped.has(label)) grouped.set(label, []); grouped.get(label).push({entry, date, known});
            }
            const container = byId('timelineContainer'); container.replaceChildren();
            if (!grouped.size) { notice('timelineContainer', 'No saved mood entries yet. Continue chatting to build your timeline.'); return; }
            container.append(node('div', '', 'timeline-line'));
            for (const [label, entries] of grouped) {
                entries.sort((a, b) => (b.known ? b.date.getTime() : 0) - (a.known ? a.date.getTime() : 0));
                const event = node('section', '', 'timeline-event'); event.append(node('h3', label, 'timeline-date'));
                for (const {entry, date, known} of entries.slice(0, 5)) {
                    const emotion = mood(entry.emotion), card = node('article', '', 'timeline-card'), header = node('div', '', 'timeline-card-header');
                    const intensity = Number.isFinite(entry.intensity) ? Math.min(1, Math.max(0, entry.intensity)) : .3;
                    const bar = node('div', '', 'timeline-intensity-bar'), fill = node('div', '', 'timeline-intensity-fill'); fill.style.width = Math.round(intensity * 100) + '%'; fill.style.background = colors[emotion]; bar.append(fill);
                    header.append(node('span', icons[emotion] + ' ' + text(entry.emotion), 'timeline-mood-badge mood-' + emotion), bar); card.append(header);
                    if (entry.analysis) card.append(node('div', text(entry.analysis), 'timeline-card-content'));
                    if (entry.message_preview) card.append(node('div', '“' + text(entry.message_preview) + '”', 'timeline-card-preview'));
                    card.append(node('div', known ? date.toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'}) : 'Time unavailable', 'timeline-card-meta')); event.append(card);
                }
                if (entries.length > 5) event.append(node('p', '+' + (entries.length - 5) + ' more entries'));
                container.append(event);
            }
        } catch (_) { if (current()) notice('timelineContainer', 'Saved timeline is unavailable. Try Refresh Timeline.'); }
    }
    async function loadMoodSummary() {
        const current = start('summary');
        try {
            const trend = await read('/api/mood/trend?days=7'); if (!current()) return;
            byId('trendIcon').textContent = ({improving: '📈', declining: '📉', stable: '➡️'})[trend.trend] || '📊';
            byId('trendText').textContent = trend.trend === 'unknown' ? 'Not enough saved mood data.' : `${text(trend.dominant_mood)} (${count(trend.count)} entries)`;
            const chips = byId('moodBreakdown'); chips.replaceChildren();
            for (const [emotion, amount] of Object.entries(trend.breakdown || {}).slice(0, 20).sort((a, b) => Number(b[1]) - Number(a[1]))) {
                const chip = node('span', icons[mood(emotion)] + ' ' + count(amount), 'mood-chip'); chip.style.border = '1px solid ' + colors[mood(emotion)]; chips.append(chip);
            }
        } catch (_) { if (current()) { clearNodes(['moodBreakdown']); byId('trendText').textContent = 'Mood summary unavailable.'; } }
    }
    async function loadPersonalityStats() {
        const current = start('personality');
        try {
            const data = await read('/api/personality/evolution'); if (!current()) return;
            byId('peopleCount').textContent = count(data.people_tracked); byId('placesCount').textContent = count(data.places_tracked);
            const style = text(data.communication_style?.formality); byId('styleFormality').textContent = style ? style.charAt(0).toUpperCase() + style.slice(1) : 'Unknown';
        } catch (_) { if (current()) for (const id of ['peopleCount', 'placesCount', 'styleFormality']) byId(id).textContent = 'Unavailable'; }
    }
    function stop() { suspended = true; revisions.clear(); for (const [controller, timer] of controllers) { controller.abort(); clearTimeout(timer); } return true; }
    function clear() {
        clearNodes(['resultsContainer', 'timelineContainer', 'totalMessages', 'totalSessions', 'resultCount', 'trendIcon', 'trendText', 'moodBreakdown', 'peopleCount', 'placesCount', 'styleFormality']);
        for (const id of ['searchInput', 'moodFilter', 'dateFrom', 'dateTo', 'roleFilter', 'limitInput']) if (byId(id)) byId(id).value = '';
        settings(false, false);
        return true;
    }
    function settings(open, focus = true) {
        byId('settingsPanel')?.classList.toggle('open', open);
        byId('settingsPanel')?.setAttribute('aria-hidden', String(!open));
        byId('auxSettings')?.setAttribute('aria-expanded', String(open));
        if (focus && active()) byId(open ? 'auxCloseSettings' : 'auxSettings')?.focus();
    }
    const owner = AlteraPage.component('auxiliary', {stop, clear});
    function suspend() { if (!owner.active) return; stop(); clear(); notice(byId('searchPage') ? 'resultsContainer' : 'timelineContainer', 'This view was suspended or its dataset changed. Reload to read current saved state.'); }
    function init() {
        if (initialized || !byId('searchPage') && !byId('timelinePage')) return;
        initialized = true;
        listen(byId('auxSettings'), 'click', () => settings(true));
        listen(byId('auxCloseSettings'), 'click', () => settings(false));
        listen(window, 'keydown', event => { if (event.key === 'Escape' && byId('settingsPanel')?.classList.contains('open')) settings(false); });
        window.addEventListener('pagehide', suspend); window.addEventListener('altera:dataset-changed', suspend);
        if (byId('searchPage')) {
            listen(byId('searchForm'), 'submit', event => { event.preventDefault(); return performSearch(); });
            listen(byId('searchStats'), 'click', loadStats);
            for (const button of document.querySelectorAll('[data-search-term]')) listen(button, 'click', () => performSearch(button.dataset.searchTerm));
            return loadStats();
        }
        listen(byId('timelineRefresh'), 'click', () => Promise.all([loadTimeline(), loadMoodSummary(), loadPersonalityStats()]));
        return Promise.all([loadTimeline(), loadMoodSummary(), loadPersonalityStats()]);
    }
    const api = {init: tracked(init), performSearch: tracked(performSearch), loadStats: tracked(loadStats), loadTimeline: tracked(loadTimeline), loadMoodSummary: tracked(loadMoodSummary), loadPersonalityStats: tracked(loadPersonalityStats)};
    AlteraPage.ready(api.init); return api;
})();
