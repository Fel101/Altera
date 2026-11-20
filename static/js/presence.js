// Presence work is opt-in on the server. Browser polling only reads its inbox.
window.PresenceUI = (() => {
    const byId = id => document.getElementById(id);
    const fields = ['Enabled', 'Timezone', 'QuietEnabled', 'QuietStart', 'QuietEnd', 'Interval', 'DailyCap', 'NetworkApproval'];
    const requests = new Map();
    let config = {}, supplied = null, context = null, generation = 0, started = false, opened = false, suspended = false;
    let pollTimer = null, failures = 0, inFlight = null, loadingSettings = null, mutating = false;
    let preferences = null, provider = null, items = [], jobs = [], rituals = [], editingRitual = null;
    let checkRequestId = null, uncertainRitualWrite = false, initialized = false;
    let settingsDirty = false, ritualDirty = false;
    const text = value => typeof value === 'string' ? value : '';
    function status(message) { if (byId('presenceStatus')) byId('presenceStatus').textContent = message; }
    function current() {
        const value = config.getContext ? config.getContext() : supplied || {
            sessionId: typeof currentSessionId === 'undefined' ? null : currentSessionId,
            companionId: typeof Companion === 'undefined' ? '' : Companion.active?.id,
            datasetEpoch: typeof AlteraAPI === 'undefined' ? null : AlteraAPI.epoch,
        };
        const sid = value?.sessionId ?? value?.session_id, epoch = value?.datasetEpoch ?? value?.dataset_epoch;
        if (!Number.isSafeInteger(sid) || sid <= 0 || typeof epoch !== 'string' || !epoch) return null;
        return {session_id: sid, avatar_id: String(value.companionId ?? value.avatar_id ?? ''), dataset_epoch: epoch};
    }
    const key = value => value ? JSON.stringify([value.session_id, value.avatar_id, value.dataset_epoch]) : '';
    function stopPoll() { if (pollTimer !== null) clearTimeout(pollTimer); pollTimer = null; }
    function abortRequests() {
        for (const [controller, timeout] of requests) { controller.abort(); clearTimeout(timeout); }
        requests.clear();
    }
    function token() { return {generation, context: {...context}, key: key(context)}; }
    function valid(mark) { return owner.active && !suspended && !document.hidden && mark.generation === generation && mark.key === key(current()); }
    function assertValid(mark) { if (!valid(mark)) { const error = new Error('The displayed companion or dataset changed. Refresh presence for the current conversation.'); error.stale = true; throw error; } }
    function owned(row, mark) {
        return row && row.session_id === mark.context.session_id && String(row.avatar_id || '') === mark.context.avatar_id && row.dataset_epoch === mark.context.dataset_epoch;
    }
    function reset(next) {
        generation++; stopPoll(); abortRequests(); context = next; inFlight = null; loadingSettings = null;
        preferences = null; provider = null; items = []; jobs = []; rituals = []; editingRitual = null;
        checkRequestId = null; failures = 0; uncertainRitualWrite = false;
        if (byId('presenceEnabled')) byId('presenceEnabled').checked = false;
        if (byId('presenceNetworkApproval')) byId('presenceNetworkApproval').checked = false;
        render(); sync();
    }
    function prepare() {
        owner.assert(); init(); const next = current();
        if (key(next) !== key(context)) reset(next);
        return next ? token() : null;
    }
    async function request(url, mark, options = {}) {
        assertValid(mark);
        const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 20000);
        requests.set(controller, timeout);
        try {
            const response = await fetch(url, {...options, signal: controller.signal, cache: 'no-store'});
            const data = await response.json(); assertValid(mark);
            if (!response.ok) { const error = new Error(data.error || `Presence request failed (HTTP ${response.status}).`); error.status = response.status; throw error; }
            return data;
        } finally { clearTimeout(timeout); requests.delete(controller); }
    }
    const json = (method, data) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
    const scopeBody = mark => ({session_id: mark.context.session_id, avatar_id: mark.context.avatar_id, dataset_epoch: mark.context.dataset_epoch});
    function schedulePoll() {
        stopPoll();
        if (!owner.active || !started || suspended || document.hidden || !current() || failures >= 5) return;
        pollTimer = setTimeout(() => { pollTimer = null; refresh(false); }, Math.min(300000, 30000 * 2 ** Math.min(failures, 4)));
    }
    function eligible(item) { return item.state === 'available' && (!item.expires_at || Date.parse(item.expires_at) > Date.now()); }
    function renderBanner() {
        const banner = byId('presenceBanner'); if (!banner) return;
        const unread = items.filter(item => eligible(item) && !item.read_at).length;
        banner.hidden = unread === 0;
        byId('presenceBannerText').textContent = unread ? `Your companion has something to say · ${unread} unread ${unread === 1 ? 'message' : 'messages'}` : '';
    }
    function actionButton(label, action, disabled = false) {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
        button.disabled = disabled || mutating; button.addEventListener('click', action); return button;
    }
    const kindLabel = kind => ({checkin: 'Check-in', greeting: 'Greeting', goodnight: 'Goodnight', weekly: 'Weekly check-in'})[kind] || 'Presence message';
    function displayTime(value) {
        const date = new Date(value);
        if (!Number.isFinite(date.getTime())) return 'unavailable';
        try { return date.toLocaleString(undefined, {timeZone: preferences?.timezone || undefined,
            month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short'}); }
        catch (_) { return date.toLocaleString(); }
    }
    function renderInbox() {
        const box = byId('presenceInbox'); if (!box) return; box.replaceChildren();
        if (!items.length) box.textContent = 'No presence messages in this conversation’s inbox.';
        for (const item of items.slice(0, 100)) {
            const article = document.createElement('article'), label = document.createElement('p'), content = document.createElement('p');
            label.textContent = `${kindLabel(item.kind)} · ${item.state}${item.read_at ? ' · read' : ''}${item.expires_at && item.state !== 'accepted' ? ` · expires ${displayTime(item.expires_at)}` : ''}`;
            content.textContent = text(item.text); article.append(label, content);
            if (item.error) { const error = document.createElement('p'); error.textContent = item.error; article.append(error); }
            if (item.state === 'available' && !item.read_at) article.append(actionButton('Mark read', () => itemAction(item.id, 'read')));
            if (['available', 'stale', 'expired'].includes(item.state)) article.append(actionButton('Dismiss', () => itemAction(item.id, 'dismiss')));
            if (item.state === 'available') article.append(actionButton('Add to original conversation', () => itemAction(item.id, 'accept'), !eligible(item)));
            box.append(article);
        }
        const work = byId('presenceJobs'); work.replaceChildren();
        for (const job of jobs.slice(0, 20)) {
            const row = document.createElement('p');
            row.textContent = `${kindLabel(job.kind)} · ${job.state || 'unknown'}${job.error ? ` · ${job.error}` : ''}`; work.append(row);
        }
    }
    const formatMinute = value => `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
    function parseMinute(value) {
        if (!/^\d{2}:\d{2}$/.test(value)) throw new Error('Choose a valid local time.');
        const [hour, minute] = value.split(':').map(Number);
        if (hour > 23 || minute > 59) throw new Error('Choose a valid local time.');
        return hour * 60 + minute;
    }
    function renderPreferences() {
        if (!preferences) return;
        settingsDirty = false;
        const name = text(config.getContext?.()?.companionName) || 'this companion';
        byId('presenceScope').textContent = `Presence for ${name}${preferences.needs_review ? ' · preferences need review' : ''}`;
        byId('presenceProvider').textContent = `Provider: ${provider.provider_key || 'unknown'} · model: ${provider.model || 'not selected'} · endpoint: ${provider.base_url || 'not specified'} · ${provider.network ? 'network connection' : 'local connection'}`;
        byId('presenceEnabled').checked = preferences.enabled === true;
        byId('presenceTimezone').value = preferences.timezone || 'UTC';
        byId('presenceQuietEnabled').checked = preferences.quiet_enabled === true;
        byId('presenceQuietStart').value = formatMinute(preferences.quiet_start ?? 1320);
        byId('presenceQuietEnd').value = formatMinute(preferences.quiet_end ?? 480);
        byId('presenceInterval').value = String(preferences.interval_minutes ?? 360);
        byId('presenceDailyCap').value = String(preferences.daily_cap ?? 2);
        byId('presenceNetworkApproval').checked = Boolean(provider.network && provider.approved && preferences.provider_approval === provider.fingerprint);
        byId('presenceNetworkText').textContent = provider.network ?
            `Approve background network requests to ${provider.provider_key} · ${provider.model} · ${provider.base_url}` : 'The selected provider is local; background network approval is not needed.';
    }
    function renderRituals() {
        const box = byId('presenceRituals'); if (!box) return; box.replaceChildren();
        if (!rituals.length) box.textContent = 'No rituals scheduled for this companion.';
        const days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
        for (const ritual of rituals.slice(0, 30)) {
            const row = document.createElement('article'), label = document.createElement('p');
            label.textContent = `${ritual.kind} · ${formatMinute(ritual.minute)}${ritual.kind === 'weekly' ? ` · ${days[ritual.weekday] || 'day not set'}` : ''} · ${ritual.enabled ? 'enabled' : 'disabled'}`;
            row.append(label, actionButton('Edit ritual', () => editRitual(ritual.id)), actionButton('Delete ritual', () => deleteRitual(ritual.id))); box.append(row);
        }
    }
    function render() { renderBanner(); renderInbox(); renderRituals(); }
    function sync() {
        if (!byId('presenceDialog')) return;
        for (const field of fields) byId('presence' + field).disabled = mutating || !preferences;
        byId('presenceNetworkApproval').disabled ||= !provider?.network;
        byId('presenceSave').disabled = mutating || !preferences;
        byId('presenceCheckNow').disabled = mutating || !preferences?.enabled || preferences.needs_review || jobs.some(job => ['pending', 'running'].includes(job.state));
        byId('presenceRitualSave').disabled = mutating || !preferences || uncertainRitualWrite;
        byId('presenceReloadSettings').disabled = mutating;
        byId('presenceRefresh').disabled = Boolean(inFlight);
        byId('presenceRitualWeekday').disabled = mutating || byId('presenceRitualKind').value !== 'weekly';
    }
    async function refreshWork(manual = true) {
        const mark = prepare(); started = true;
        if (!mark || document.hidden || suspended) { if (!mark) status('Open a conversation before using companion presence.'); return; }
        if (inFlight?.key === mark.key) return inFlight.promise;
        if (manual) failures = 0;
        const entry = {key: mark.key};
        entry.promise = (async () => {
            try {
                const data = await request(`/api/presence/inbox?session_id=${mark.context.session_id}`, mark);
                if (!Array.isArray(data.items) || !Array.isArray(data.jobs || [])) throw new Error('Altera returned an unreadable inbox.');
                if (data.scope && !owned(data.scope, mark)) throw new Error('Altera returned an inbox for another context.');
                const historical = row => row && row.session_id === mark.context.session_id &&
                    String(row.avatar_id || '') === mark.context.avatar_id &&
                    typeof row.dataset_epoch === 'string' && row.dataset_epoch &&
                    ['stale', 'expired', 'failed'].includes(row.state) && !row.text;
                if ([...data.items, ...(data.jobs || [])].some(row => !owned(row, mark) && !historical(row))) throw new Error('Altera returned presence work from another context. Refresh the current conversation.');
                items = data.items; jobs = data.jobs || []; failures = 0; renderBanner(); renderInbox();
                if (data.expression !== undefined && typeof window.setCompanionExpression === 'function') {
                    window.setCompanionExpression(data.expression, {...mark.context});
                }
                if (manual && opened) status('Inbox refreshed. Reading and adding a message to the conversation are separate actions.');
            } catch (error) {
                if (valid(mark) && !error.stale && error.name !== 'AbortError') {
                    failures++; status(`Inbox refresh failed: ${error.message}${failures >= 5 ? ' Automatic refresh is paused; use Refresh inbox to retry.' : ''}`);
                }
            } finally {
                if (inFlight === entry) inFlight = null;
                if (valid(mark)) { sync(); schedulePoll(); }
            }
        })();
        inFlight = entry; sync(); return entry.promise;
    }
    async function loadSettingsWork() {
        const mark = prepare(); if (!mark) return;
        if (loadingSettings?.key === mark.key) return loadingSettings.promise;
        const entry = {key: mark.key};
        entry.promise = (async () => {
            try {
                const [saved, schedule] = await Promise.all([
                    request(`/api/presence/preferences?session_id=${mark.context.session_id}`, mark),
                    request(`/api/presence/rituals?session_id=${mark.context.session_id}`, mark),
                ]);
                assertValid(mark);
                if (!owned(saved.preferences, mark) || !saved.provider || !Array.isArray(schedule.rituals) || schedule.rituals.some(row => !owned(row, mark))) {
                    throw new Error('Altera returned preferences or rituals from another context.');
                }
                preferences = saved.preferences; provider = saved.provider; rituals = schedule.rituals;
                uncertainRitualWrite = false; editingRitual = null; renderPreferences(); renderRituals(); resetRitualEditor(); sync();
                status(preferences.enabled ? 'Presence is enabled for this companion. Inbox messages still require acceptance.' :
                    'Presence is off for this companion. It starts only after you explicitly enable it and save.');
            } catch (error) { if (valid(mark) && !error.stale && error.name !== 'AbortError') status(`Preferences could not be loaded: ${error.message}`); }
            finally { if (loadingSettings === entry) loadingSettings = null; }
        })();
        loadingSettings = entry; return entry.promise;
    }
    async function mutationWork(action) {
        const mark = prepare(); if (!mark || mutating) return;
        mutating = true; sync(); renderInbox(); renderRituals();
        try { return await action(mark); }
        catch (error) { if (valid(mark) && !error.stale) status(error.name === 'AbortError' ? 'The result was not confirmed. Refresh before retrying.' : `Not confirmed: ${error.message}`); }
        finally { mutating = false; if (valid(mark)) { sync(); renderInbox(); renderRituals(); } }
    }
    const save = () => mutation(async mark => {
        if (!preferences) throw new Error('Load this companion’s saved preferences first.');
        const timezone = byId('presenceTimezone').value.trim();
        try { new Intl.DateTimeFormat('en', {timeZone: timezone}).format(); } catch { throw new Error('Enter a valid IANA timezone, such as America/New_York or UTC.'); }
        const interval = Number(byId('presenceInterval').value), cap = Number(byId('presenceDailyCap').value);
        if (!Number.isSafeInteger(interval) || interval < 15 || interval > 1440 || !Number.isSafeInteger(cap) || cap < 0 || cap > 5) throw new Error('Choose an interval of 15–1440 minutes and a daily limit of 0–5.');
        const approval = byId('presenceNetworkApproval').checked;
        if (approval && (!provider?.network || !provider.fingerprint)) throw new Error('Reload the provider identity before approving network requests.');
        const settings = {enabled: byId('presenceEnabled').checked, timezone, quiet_enabled: byId('presenceQuietEnabled').checked,
            quiet_start: parseMinute(byId('presenceQuietStart').value), quiet_end: parseMinute(byId('presenceQuietEnd').value),
            interval_minutes: interval, daily_cap: cap, provider_approval: approval ? provider.fingerprint : null};
        const result = await request('/api/presence/preferences', mark, json('PATCH', {...scopeBody(mark), revision: preferences.revision, settings}));
        if (!owned(result.preferences, mark) || !result.provider) throw new Error('The saved preferences did not match this companion.');
        preferences = result.preferences; provider = result.provider; renderPreferences();
        status('Presence preferences saved for this companion. Inbox messages still require explicit acceptance.');
    });
    const itemAction = (itemId, action) => mutation(async mark => {
        if (!['read', 'dismiss', 'accept'].includes(action)) throw new Error('Choose a supported inbox action.');
        const before = items.find(item => item.id === itemId);
        if (!before || !owned(before, mark) || (action === 'accept' && !eligible(before))) throw new Error('This inbox message is unavailable or expired. Refresh the inbox.');
        const result = await request(`/api/presence/inbox/${encodeURIComponent(itemId)}/${action}`, mark, json('POST', scopeBody(mark)));
        if (!owned(result.item, mark) || result.item.id !== itemId) throw new Error('The inbox response did not match this conversation.');
        items = items.map(item => item.id === itemId ? result.item : item); renderBanner(); renderInbox();
        if (action === 'accept') {
            if (result.item.state !== 'accepted') throw new Error('Altera did not confirm adding this message to its original conversation.');
            assertValid(mark); if (typeof config.onAccepted === 'function') await config.onAccepted(result.item);
            if (valid(mark)) status('The message was added to its original conversation.');
        } else status(action === 'read' ? 'Marked read. It has not been added to the conversation.' : 'Inbox message dismissed.');
    });
    const checkNow = () => mutation(async mark => {
        if (!preferences?.enabled || preferences.needs_review) throw new Error('Explicitly enable and save presence for this companion first.');
        checkRequestId ||= 'presence_' + crypto.randomUUID().replace(/-/g, '');
        const result = await request('/api/presence/check-now', mark, json('POST', {...scopeBody(mark), request_id: checkRequestId}));
        if (!owned(result.job, mark)) throw new Error('The check was not confirmed for this companion.');
        jobs = [result.job, ...jobs.filter(job => job.id !== result.job.id)]; checkRequestId = null;
        status('Presence check requested. Its result will wait in this inbox; nothing is added to chat automatically.'); renderInbox();
    });
    function resetRitualEditor() {
        ritualDirty = false;
        editingRitual = null; byId('presenceRitualKind').value = 'greeting'; byId('presenceRitualTime').value = '08:00';
        byId('presenceRitualWeekday').value = '0'; byId('presenceRitualEnabled').checked = true; byId('presenceRitualSave').textContent = 'Add ritual'; sync();
    }
    function editRitual(ritualId) {
        if (mutating) return;
        const ritual = rituals.find(item => item.id === ritualId); if (!ritual) return;
        ritualDirty = false;
        editingRitual = ritual.id; byId('presenceRitualKind').value = ritual.kind; byId('presenceRitualTime').value = formatMinute(ritual.minute);
        byId('presenceRitualWeekday').value = String(ritual.weekday ?? 0); byId('presenceRitualEnabled').checked = ritual.enabled;
        byId('presenceRitualSave').textContent = 'Save ritual edit'; sync();
    }
    const saveRitual = () => mutation(async mark => {
        if (!preferences || uncertainRitualWrite) throw new Error('Reload saved preferences and rituals before saving a ritual.');
        const kind = byId('presenceRitualKind').value, weekday = Number(byId('presenceRitualWeekday').value);
        if (!['greeting', 'goodnight', 'weekly'].includes(kind) || (kind === 'weekly' && (!Number.isSafeInteger(weekday) || weekday < 0 || weekday > 6))) throw new Error('Choose a valid ritual and weekday.');
        const existing = rituals.find(ritual => ritual.id === editingRitual);
        const body = {...scopeBody(mark), kind, minute: parseMinute(byId('presenceRitualTime').value), weekday: kind === 'weekly' ? weekday : null,
            enabled: byId('presenceRitualEnabled').checked, ...(existing ? {revision: existing.revision} : {})};
        let result;
        try { result = await request(existing ? `/api/presence/rituals/${encodeURIComponent(existing.id)}` : '/api/presence/rituals', mark, json(existing ? 'PUT' : 'POST', body)); }
        catch (error) { if (valid(mark)) uncertainRitualWrite = true; throw new Error(`${error.message} Reload saved rituals before retrying so a lost response cannot create a duplicate.`); }
        if (!owned(result.ritual, mark)) throw new Error('The saved ritual did not match this companion.');
        rituals = [...rituals.filter(ritual => ritual.id !== result.ritual.id), result.ritual]; resetRitualEditor(); renderRituals(); status('Ritual saved. Presence must also be enabled before it can run.');
    });
    const deleteRitual = ritualId => mutation(async mark => {
        const ritual = rituals.find(value => value.id === ritualId); if (!ritual) throw new Error('Refresh the ritual list first.');
        await request(`/api/presence/rituals/${encodeURIComponent(ritualId)}`, mark, json('DELETE', {...scopeBody(mark), revision: ritual.revision}));
        rituals = rituals.filter(value => value.id !== ritualId); if (editingRitual === ritualId) resetRitualEditor(); renderRituals(); status('Ritual deleted.');
    });
    function close() {
        opened = false; generation++; abortRequests(); inFlight = null; loadingSettings = null;
        byId('presenceDialog')?.close(); schedulePoll();
    }
    function init() {
        if (initialized || !byId('presenceDialog')) return; initialized = true;
        const host = byId('presenceBannerHost'); if (host) host.append(byId('presenceBanner'));
        byId('presenceOpenInbox').addEventListener('click', open);
        byId('presenceClose').addEventListener('click', close);
        byId('presenceRefresh').addEventListener('click', () => refresh());
        byId('presenceReloadSettings').addEventListener('click', loadSettings);
        byId('presenceCheckNow').addEventListener('click', checkNow);
        byId('presencePreferences').addEventListener('submit', event => { event.preventDefault(); save(); });
        byId('presenceRitualForm').addEventListener('submit', event => { event.preventDefault(); saveRitual(); });
        for (const event of ['input', 'change']) {
            byId('presencePreferences').addEventListener(event, () => { settingsDirty = true; });
            byId('presenceRitualForm').addEventListener(event, () => { ritualDirty = true; });
        }
        byId('presenceRitualCancel').addEventListener('click', resetRitualEditor);
        byId('presenceRitualKind').addEventListener('change', sync);
        byId('presenceDialog').addEventListener('cancel', event => { event.preventDefault(); close(); });
        sync();
    }
    async function openWork(value) {
        if (value && !value.type) supplied = value;
        prepare(); if (!byId('presenceDialog')) throw new Error('The presence dialog is unavailable.');
        opened = true; if (!byId('presenceDialog').open) byId('presenceDialog').showModal();
        await Promise.all([refresh(), loadSettings()]);
    }
    const owner = AlteraPage.component('presence', {
        stop() {
            started = false; suspended = true; opened = false; generation++;
            stopPoll(); abortRequests(); byId('presenceDialog')?.close(); return true;
        },
        clear() {
            supplied = context = preferences = provider = inFlight = loadingSettings = null;
            config = {}; items = []; jobs = []; rituals = []; editingRitual = checkRequestId = null;
            mutating = false; uncertainRitualWrite = false; failures = 0;
            settingsDirty = ritualDirty = false;
            for (const name of ['presencePreferences', 'presenceRitualForm']) {
                for (const input of byId(name)?.querySelectorAll?.('input, select, textarea') || []) { input.value = ''; input.checked = false; }
            }
            for (const name of ['presenceInbox', 'presenceJobs', 'presenceRituals']) byId(name)?.replaceChildren();
            for (const name of ['presenceStatus', 'presenceScope', 'presenceProvider', 'presenceNetworkText']) if (byId(name)) byId(name).textContent = '';
            return true;
        }
    });
    const track = action => (...args) => owner.run(() => action(...args)).catch(error => { if (owner.active) throw error; });
    const refresh = track(refreshWork), loadSettings = track(loadSettingsWork), mutation = track(mutationWork), open = track(openWork);
    AlteraPage.ready(init);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) { suspended = true; generation++; stopPoll(); abortRequests(); inFlight = null; loadingSettings = null; }
        else if (owner.active) { suspended = false; if (started) { refresh(); if (opened) loadSettings(); } }
    });
    window.addEventListener('pagehide', () => { suspended = true; generation++; stopPoll(); abortRequests(); inFlight = null; loadingSettings = null; });
    window.addEventListener('pageshow', () => { if (owner.active) { suspended = false; if (started && !document.hidden) refresh(); } });
    return {configure: value => { owner.assert(); config = {...config, ...value}; }, refresh, open, close, loadSettings, save, itemAction, checkNow,
        editRitual, saveRitual, deleteRitual, state: () => ({context, opened, preferences, provider, items, jobs, rituals, mutating, failures})};
})();
