// Saved state only on open. Reflection jobs require a deliberate, separately opted-in action.
window.StoryUI = (() => {
    const byId = id => document.getElementById(id);
    const text = value => typeof value === 'string' ? value : '';
    const el = (tag, value, cls) => { const node = document.createElement(tag); if (value != null) node.textContent = String(value); if (cls) node.className = cls; return node; };
    const json = (method, body) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    let options = {}, scopes = [], context = null, generation = 0, initialized = false;
    let episodes = [], preferences = null, provider = null, reports = [], jobs = [], notebook = [];
    let sourceChoices = [], sourcesReady = false;
    let busy = false, reading = null, reviewed = null, sourceSerial = 0, createAttempt = null, runId = null;
    let uncertain = false, dialogBox = null, dialogDraft = null, suspended = false, scopeChange = 0;
    let episodeBaseline = '', reflectionBaseline = '', reflectionView = null, draftOrigin = null;
    let episodeSlot = '', reflectionSlot = '', runSlot = '';
    const episodeFields = ['Title', 'Kind', 'Summary', 'Uncertainty', 'Salience', 'InStory', 'SourceOne', 'SourceTwo'];
    const reflectionFields = ['ReflectionEnabled', 'Timezone', 'Interval', 'Cooldown', 'DailyCap', 'QuietEnabled', 'QuietStart', 'QuietEnd', 'NetworkApproval'];
    const checks = new Set(['InStory', 'ReflectionEnabled', 'QuietEnabled', 'NetworkApproval']);
    const values = fields => Object.fromEntries(fields.map(field => [field, checks.has(field) ? Boolean(byId('story' + field)?.checked) : byId('story' + field)?.value || '']));
    const episodeDirty = () => Boolean(draftOrigin && (JSON.stringify(values(episodeFields)) !== episodeBaseline || createAttempt));
    const reflectionDirty = () => Boolean(reflectionView && JSON.stringify(values(reflectionFields)) !== reflectionBaseline);
    const viewIdentity = () => JSON.stringify([key(context), preferences?.revision, provider?.fingerprint]);
    const dialogValues = draft => Object.fromEntries(Object.entries(draft.inputs).filter(([, input]) => !input.readOnly).map(([field, input]) => [field, String(input.value)]));
    const origin = () => context ? {...context, companion_name: name()} : null;
    const slot = () => 'story_' + id().replaceAll('-', '');
    function drafts() {
        const result = [];
        const add = (key, type, original, content) => result.push({key, message: 'Unsent Our story work. Review and re-enter manually for the original companion and conversation. Nothing in this draft authorizes a save, provider approval, retry or reflection job. A request may already have reached Altera; inspect saved state before replacing it.\n' + JSON.stringify({type, original_context: original, ...content}, null, 2)});
        if (episodeDirty()) add(episodeSlot, 'episode', draftOrigin, {fields: values(episodeFields), unconfirmed_submission: createAttempt?.payload || null});
        if (reflectionDirty()) add(reflectionSlot, 'reflection_preferences', reflectionView.origin, {fields: values(reflectionFields), saved_revision: reflectionView.revision, provider: reflectionView.provider});
        if (dialogDraft && (dialogDraft.pending || JSON.stringify(dialogValues(dialogDraft)) !== dialogDraft.baseline)) add(dialogDraft.slot, dialogDraft.title, dialogDraft.origin, {reference: dialogDraft.reference, fields: dialogValues(dialogDraft), unconfirmed_submission: dialogDraft.pending});
        if (runId) add(runSlot, 'unconfirmed_reflection_request', draftOrigin, {request_id: runId});
        return result;
    }
    async function prepareReplacement() {
        return !drafts().length || window.confirm('Discard unfinished Our story edits and leave this view?');
    }
    // Synchronous invocation preserves form preventDefault during DOM dispatch.
    const tracked = action => (...args) => {
        const work = owner.run(() => action(...args)); work.catch(() => {}); return work;
    };
    const listen = (node, event, action) => node.addEventListener(event, (...args) => { if (event === 'submit') args[0].preventDefault(); return tracked(action)(...args).catch(error => { if (owner.active) message(error.message); }); });
    const requests = new Map();
    const epoch = () => options.getEpoch ? options.getEpoch() : window.AlteraAPI?.epoch || window.__DATA_EPOCH__ || null;
    const key = value => value ? JSON.stringify([value.avatar_id, value.session_id, value.dataset_epoch]) : '';
    const mark = () => ({generation, context: context ? {...context} : null, key: key(context)});
    const valid = token => owner.active && !suspended && token.generation === generation && token.key === key(context) && token.context?.dataset_epoch === epoch();
    const owned = (value, token) => value && value.session_id === token.context.session_id && String(value.avatar_id || '') === token.context.avatar_id && value.dataset_epoch === token.context.dataset_epoch;
    const body = token => ({session_id: token.context.session_id, avatar_id: token.context.avatar_id, dataset_epoch: token.context.dataset_epoch});
    const name = () => scopes.find(row => (row.id === '_default' ? '' : row.id) === context?.avatar_id)?.name || 'this companion';
    const message = value => { if (byId('storyStatus')) byId('storyStatus').textContent = value; };
    const reflectionMessage = value => { if (byId('storyReflectionStatus')) byId('storyReflectionStatus').textContent = value; };
    const id = () => crypto.randomUUID();
    function assertValid(token) { if (!valid(token)) { if (owner.active && token.generation === generation && token.context?.dataset_epoch !== epoch()) suspend(); const error = new Error('The companion or dataset changed. Refresh this page before saving.'); error.stale = true; throw error; } }
    function abortRequests() { for (const [controller, timer] of requests) { controller.abort(); clearTimeout(timer); } requests.clear(); }
    const request = tracked(async (url, token, init = {}) => {
        if (token) assertValid(token);
        const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20000);
        requests.set(controller, timer);
        try {
            const response = await fetch(url, {...init, cache: 'no-store', signal: controller.signal});
            let data; try { data = await response.json(); } catch (_) { throw new Error('Altera returned an unreadable response. Refresh saved state before retrying.'); }
            owner.assert(); if (token) assertValid(token);
            if (!response.ok) { const error = new Error(text(data.error) || `Request failed (HTTP ${response.status}).`); error.status = response.status; throw error; }
            return data;
        } finally { clearTimeout(timer); requests.delete(controller); }
    });
    function button(label, action, disabled = false, focusKey = '') { const node = el('button', label); node.type = 'button'; if (focusKey) node.id = 'storyAction-' + focusKey; node.disabled = disabled || busy || uncertain; listen(node, 'click', action); return node; }
    function sourceBlock(source) {
        const box = el('div', null, 'story-source');
        const start = Number.isInteger(source.start) ? source.start : '?', end = Number.isInteger(source.end) ? source.end : '?';
        box.append(el('p', `Conversation ${source.session_id || '?'} · message ${source.message_id || '?'} · ${text(source.role) || 'unknown role'} · ${text(source.status) || 'unavailable'}`, 'story-source-meta'));
        box.append(el('p', `Characters ${start}–${end}${source.original_session_id && source.original_session_id !== source.session_id ? ` · originally conversation ${source.original_session_id}` : ''}`, 'story-source-meta'));
        if (source.sha256) box.append(el('p', `SHA-256: ${text(source.sha256)}`, 'story-source-meta'));
        box.append(el('p', source.status === 'current' ? text(source.excerpt) || text(source.content) || 'No source excerpt returned.' : 'Source text is withheld because it changed or is unavailable.'));
        if (source.status === 'current' && !source.excerpt && !source.content && Number.isSafeInteger(source.message_id)) box.append(button('Inspect source excerpt', () => inspectSource(source)));
        return box;
    }
    function sourcesDetails(sources = []) {
        const details = el('details'); details.append(el('summary', `Sources (${sources.length})`));
        if (!sources.length) details.append(el('p', 'No inspectable sources. Treat this material as unavailable for recall.', 'story-warning'));
        for (const source of sources.slice(0, 20)) details.append(sourceBlock(source));
        return details;
    }
    function kindLabel(item) {
        if (item.kind === 'imagined_scene') return 'Imagined scene';
        if (item.curation === 'reflection' || item.kind === 'derived_summary') return 'Derived candidate';
        if (item.kind === 'actual_exchange') return 'Actual exchange';
        return 'Unclassified candidate';
    }
    function curationLabel(item) {
        if (item.curation === 'reflection') return 'Derived by reflection · review before relying on it';
        if (item.curation === 'manual_corrected') return 'Corrected / curated by you';
        if (item.curation === 'manual_curated') return 'Description curated by you';
        if (item.curation === 'manual_saved') return 'Exchange saved by you';
        return 'Curation origin unavailable';
    }
    const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
    function renderEpisodes() {
        const cards = byId('storyCards'); if (!cards) return; cards.replaceChildren();
        const query = text(byId('storySearch')?.value).toLocaleLowerCase(), filter = byId('storyFilter')?.value || 'story';
        const savedTime = item => { const value = Date.parse(text(item.created_at)); return Number.isFinite(value) ? value : 0; };
        const shown = episodes.filter(item => (filter === 'all' || (filter === 'story' && item.in_story) || (filter === 'pinned' && item.pinned) || (filter === 'review' && (!item.eligible || item.curation === 'reflection'))) &&
            `${text(item.title)} ${text(item.summary)}`.toLocaleLowerCase().includes(query)).sort((a, b) => savedTime(b) - savedTime(a) || (text(a.id) < text(b.id) ? -1 : text(a.id) > text(b.id) ? 1 : 0));
        byId('storyCount').textContent = `${shown.length} of ${episodes.length}`;
        if (!shown.length) cards.append(el('p', episodes.length ? 'No episodes match this view. Choose All episodes or change your search.' : 'Your story is open. Save a source-backed exchange below when you choose.', 'story-empty'));
        for (const item of shown) {
            const card = el('article', null, 'story-card'), header = el('header'), badges = el('div', null, 'story-badges');
            badges.append(el('span', kindLabel(item), 'story-pill ' + (item.kind === 'imagined_scene' ? 'imagined' : item.curation === 'reflection' || item.kind === 'derived_summary' ? 'derived' : '')));
            if (item.pinned) badges.append(el('span', 'Pinned', 'story-pill pinned'));
            header.append(badges, el('span', text(item.status) || 'unavailable', 'story-muted')); card.append(header);
            const current = item.status === 'current' && item.dataset_epoch === context?.dataset_epoch;
            card.append(el('h3', current ? text(item.title) || 'Untitled episode' : 'Episode needs review'));
            if (current) card.append(el('p', text(item.summary), 'story-content'));
            else card.append(el('p', 'Saved text is withheld because its sources or dataset changed.', 'story-warning'));
            card.append(el('p', curationLabel(item), 'story-muted'));
            if (item.created_at) card.append(el('p', 'Saved ' + formatTime(item.created_at) + ' · source messages establish what was said, not when a real-world event happened.', 'story-muted'));
            if (current && item.uncertainty) card.append(el('p', 'Uncertainty: ' + text(item.uncertainty), 'story-warning'));
            const ranking = item.ranking || {};
            card.append(el('p', `Salience ${number(item.salience)}/5 · recency ${Math.round(number(ranking.recency) * 100)}% · retrieval priority ${number(ranking.score).toFixed(2)}`, 'story-muted'));
            card.append(el('p', `Age half-life ${number(ranking.half_life_days) || 30} days. Decay changes priority; it does not delete this saved episode.`, 'story-muted'));
            card.append(el('p', item.confidence == null ? 'Extraction confidence: not scored' : `Extraction confidence: ${Math.round(number(item.confidence) * 100)}% · separate from importance`, 'story-muted'));
            card.append(el('p', item.eligible ? 'Eligible for occasional recall within its frequency budget; selection is not guaranteed.' : 'Excluded from recall until its sources and curation are eligible.', item.eligible ? 'story-muted' : 'story-warning'));
            const actions = el('div', null, 'story-actions');
            if (current) actions.append(button(item.pinned ? 'Unpin' : 'Pin', () => patchEpisode(item.id, {pinned: !item.pinned})),
                button(item.in_story ? 'Remove from story' : 'Curate into story', () => patchEpisode(item.id, {in_story: !item.in_story})),
                button('Correct', () => editEpisode(item.id), false, 'episode-' + item.id + '-correct'));
            if (item.status === 'stale' && item.dataset_epoch !== context?.dataset_epoch) actions.append(button('Review restored moment', () => reviewRestored(item.id)));
            if (!current && item.status !== 'forgotten') card.append(el('p', 'Save a new episode from current source evidence before correcting or curating this one.', 'story-muted'));
            if (item.status !== 'forgotten') actions.append(button('Forget', () => forgetEpisode(item.id), false, 'episode-' + item.id + '-forget'));
            card.append(actions, sourcesDetails(item.sources)); cards.append(card);
        }
    }
    function formatTime(value) { const date = new Date(value); return Number.isFinite(date.getTime()) ? date.toLocaleString() : 'time unavailable'; }
    function renderReports() {
        const box = byId('storyReports'); if (!box) return; box.replaceChildren();
        const currentNotes = el('section'); currentNotes.append(el('h3', 'Current derived notebook'));
        if (!notebook.length) currentNotes.append(el('p', 'No current derived notebook entries.', 'story-muted'));
        for (const entry of notebook) {
            const row = el('article', null, 'story-report'); row.append(el('h4', entry.note === 'about_you' ? 'About you' : 'Our story'));
            row.append(el('p', `Derived summary · ${entry.user_edited ? 'corrected by you' : 'generated by reflection'} · ${text(entry.state) || 'unavailable'}`, 'story-muted'));
            if (entry.state === 'current') row.append(el('p', text(entry.text), 'story-report-text'));
            if (entry.uncertainty && entry.state === 'current') row.append(el('p', 'Uncertainty: ' + text(entry.uncertainty), 'story-warning'));
            row.append(el('p', entry.confidence == null ? 'Extraction confidence: not scored' : `Extraction confidence: ${Math.round(number(entry.confidence) * 100)}%`, 'story-muted'));
            row.append(sourcesDetails(entry.sources));
            if (entry.state === 'current') { const actions = el('div', null, 'story-actions'); actions.append(button('Correct derived note', () => editNote(entry.id), false, 'note-' + entry.id + '-correct'), button('Dismiss derived note', () => dismissNote(entry.id), false, 'note-' + entry.id + '-dismiss')); row.append(actions); }
            currentNotes.append(row);
        }
        box.append(currentNotes, el('h3', 'Run reports'));
        if (!jobs.length && !reports.length) box.append(el('p', 'No reflection runs saved for this companion.', 'story-muted'));
        for (const job of jobs.slice(0, 30)) {
            const row = el('article', null, 'story-report'); row.append(el('p', `Job ${text(job.id)} · ${text(job.state) || 'unknown'} · ${formatTime(job.created_at || job.due_at)}`, 'story-muted'));
            if (job.error) row.append(el('p', text(job.error), 'story-warning'));
            box.append(row);
        }
        for (const report of reports.slice(0, 30)) {
            const row = el('details', null, 'story-report'); row.append(el('summary', `${formatTime(report.created_at)} · ${text(report.state) || 'saved'} · derived reflection`));
            row.append(el('p', text(report.summary), 'story-report-text'));
            row.append(el('p', `${Array.isArray(report.episode_ids) ? report.episode_ids.length : 0} episode candidates · generated text does not prove a real event.`, 'story-muted'));
            for (const proposal of (report.notebook_proposals || []).slice(0, 20)) {
                row.append(el('h4', `${proposal.note === 'about_you' ? 'About you' : 'Our story'} · ${text(proposal.state) || 'derived candidate'}`), el('p', text(proposal.text), 'story-report-text'));
                if (proposal.uncertainty) row.append(el('p', 'Uncertainty: ' + text(proposal.uncertainty), 'story-warning'));
            }
            row.append(sourcesDetails(report.sources)); box.append(row);
        }
    }
    const minuteText = value => `${String(Math.floor(number(value) / 60)).padStart(2, '0')}:${String(number(value) % 60).padStart(2, '0')}`;
    function parseMinute(value) { if (!/^\d{2}:\d{2}$/.test(value)) throw new Error('Choose valid quiet hours.'); const [hour, minute] = value.split(':').map(Number); if (hour > 23 || minute > 59) throw new Error('Choose valid quiet hours.'); return hour * 60 + minute; }
    function renderPreferences(force = false) {
        if (!preferences || !provider) return;
        if (!force && reflectionDirty()) { reflectionMessage('Your unsaved preferences are still here. Reload saved preferences before saving if the provider or saved revision changed.'); return; }
        byId('storyReflectionEnabled').checked = preferences.enabled === true;
        byId('storyTimezone').value = preferences.timezone || 'UTC';
        byId('storyInterval').value = String(preferences.interval_minutes ?? 1440);
        byId('storyCooldown').value = String(preferences.cooldown_minutes ?? 1440);
        byId('storyDailyCap').value = String(preferences.daily_cap ?? 1);
        byId('storyQuietEnabled').checked = preferences.quiet_enabled === true;
        byId('storyQuietStart').value = minuteText(preferences.quiet_start ?? 1320);
        byId('storyQuietEnd').value = minuteText(preferences.quiet_end ?? 480);
        byId('storyProvider').textContent = `Provider: ${text(provider.provider_key) || 'unavailable'} · model: ${text(provider.model) || 'unselected'} · endpoint: ${text(provider.base_url) || 'unavailable'} · ${provider.network ? 'network connection' : 'local connection'}`;
        byId('storyNetworkApproval').checked = Boolean(provider.network && provider.approved && preferences.provider_approval === provider.fingerprint);
        byId('storyNetworkText').textContent = provider.network ? `Approve reflection requests to ${text(provider.provider_key)} · ${text(provider.model)} · ${text(provider.base_url)}` : 'The selected provider is local; separate network approval is not needed.';
        byId('storyReflectionState').textContent = preferences.needs_review ? 'Preferences need review' : preferences.enabled ? 'Schedule enabled' : 'Schedule off';
        reflectionBaseline = JSON.stringify(values(reflectionFields));
        reflectionView = {identity: viewIdentity(), origin: origin(), revision: preferences.revision, provider: {key: provider.provider_key, model: provider.model}};
    }
    function sync() {
        const unavailable = !owner.active || suspended || !context || !Number.isSafeInteger(context.session_id) || context.dataset_epoch !== epoch();
        for (const suffix of ['Create', 'ReviewSources']) if (byId('story' + suffix)) byId('story' + suffix).disabled = unavailable || busy || uncertain || Boolean(reading) || (suffix === 'ReviewSources' ? Boolean(createAttempt) || !sourcesReady : !sourcesReady && !createAttempt);
        for (const suffix of ['Title', 'Kind', 'Summary', 'Uncertainty', 'Salience', 'InStory']) byId('story' + suffix).disabled = unavailable || busy || Boolean(createAttempt);
        for (const suffix of ['SourceOne', 'SourceTwo']) byId('story' + suffix).disabled = unavailable || busy || Boolean(createAttempt) || Boolean(reading) || !sourcesReady;
        for (const suffix of ['ReflectionEnabled', 'Timezone', 'Interval', 'Cooldown', 'DailyCap', 'QuietEnabled', 'QuietStart', 'QuietEnd', 'SaveReflection']) byId('story' + suffix).disabled = unavailable || busy || uncertain || !preferences;
        byId('storyNetworkApproval').disabled = unavailable || busy || uncertain || !provider?.network;
        byId('storyRunReflection').disabled = unavailable || busy || uncertain || !preferences?.enabled || preferences.needs_review || preferences.daily_cap === 0 || jobs.some(job => ['pending', 'running'].includes(job.state));
        byId('storyReloadReflection').disabled = unavailable || busy;
        byId('storyRefreshReports').disabled = unavailable || busy;
        byId('storyRefresh').disabled = Boolean(reading) || busy;
        byId('storyScope').disabled = !scopes.length;
        if (createAttempt) byId('storyCreate').textContent = 'Retry the same episode save'; else byId('storyCreate').textContent = 'Save reviewed episode';
        byId('storyDiscardAttempt').hidden = !createAttempt; byId('storyDiscardAttempt').disabled = busy;
    }
    function clearDraft() {
        sourceSerial++; reviewed = null; createAttempt = null; byId('storySourcePreview')?.replaceChildren();
        for (const suffix of ['Title', 'Summary', 'Uncertainty', 'SourceOne', 'SourceTwo']) if (byId('story' + suffix)) byId('story' + suffix).value = '';
        byId('storyKind').value = 'actual_exchange'; byId('storySalience').value = '3'; byId('storyInStory').checked = true;
        episodeBaseline = JSON.stringify(values(episodeFields));
        episodeSlot = slot();
    }
    function reset(next) {
        generation++; abortRequests(); context = next; reading = null; busy = false; uncertain = false; suspended = false;
        episodes = []; preferences = provider = null; reports = []; jobs = []; notebook = []; runId = null;
        sourceChoices = []; sourcesReady = false;
        if (dialogBox) dialogBox.close(); clearDraft();
        renderSourceChoices(); byId('storySourceStatus').textContent = 'Choose a companion to see recent messages.';
        byId('storyReflectionEnabled').checked = byId('storyNetworkApproval').checked = false;
        for (const field of reflectionFields.filter(field => !checks.has(field))) byId('story' + field).value = '';
        byId('storyQuietEnabled').checked = false;
        reflectionBaseline = ''; reflectionView = null; draftOrigin = origin();
        episodeSlot = slot(); reflectionSlot = slot(); runSlot = slot();
        byId('storyProvider').textContent = byId('storyNetworkText').textContent = ''; byId('storyReflectionState').textContent = 'Off until you opt in';
        byId('storyBoundary').hidden = true; byId('storyBudget').textContent = ''; reflectionMessage(''); renderEpisodes(); renderReports(); sync();
    }
    async function selectScope(scopeId, sessionId = null) {
        const scope = scopes.find(item => item.id === scopeId);
        if (!scope) return;
        const change = ++scopeChange, oldScope = context?.avatar_id || '_default', before = JSON.stringify(drafts());
        try {
            if (!await prepareReplacement()) { if (change === scopeChange) byId('storyScope').value = oldScope; return false; }
            if (JSON.stringify(drafts()) !== before) throw new Error('Your edits changed while leaving this view. Keep them open and retry.');
        } catch (error) { if (owner.active && change === scopeChange) { byId('storyScope').value = oldScope; message(error.message); } return false; }
        if (!owner.active || change !== scopeChange) return false;
        // Only server-listed owned sessions can bind a standalone page. URL values never create a session.
        const allowed = [scope.session_id, ...(Array.isArray(scope.session_ids) ? scope.session_ids : [])].filter(Number.isSafeInteger);
        const selectedSession = allowed.includes(sessionId) ? sessionId : scope.session_id;
        reset({avatar_id: scope.id === '_default' ? '' : scope.id, session_id: selectedSession, dataset_epoch: epoch()});
        byId('storyScope').value = scope.id;
        for (const id of ['storyMemoryLink', 'storyFooterMemoryLink']) {
            byId(id).href = `/memory?scope=${encodeURIComponent(scope.id)}`;
        }
        if (!Number.isSafeInteger(selectedSession) || selectedSession <= 0 || !epoch()) { message('No saved conversation is available for this companion. Open a conversation in chat before saving episodes.'); sync(); return; }
        await refresh();
    }
    function acceptEpisodes(data, token) {
        if (!owned(data.scope, token) || !Array.isArray(data.episodes)) throw new Error('Altera returned episodes for a different companion, conversation or dataset.');
        if (data.episodes.some(item => !item || String(item.avatar_id || '') !== token.context.avatar_id || (item.status === 'current' && item.dataset_epoch !== token.context.dataset_epoch))) throw new Error('Altera returned an episode outside this companion’s dataset.');
        episodes = data.episodes;
        const notice = byId('storyBoundary'); notice.hidden = !data.recall_after && !data.repair_pending;
        notice.textContent = data.repair_pending ? 'Sources changed. Derived context is paused until it is reviewed or rebuilt. Saved episodes may need correction.' : 'A fresh recall boundary is active. Earlier sources stay visible in chat but may be excluded from recall.';
        const budget = data.recall_budget;
        byId('storyBudget').textContent = budget ? `Occasional recall budget: ${number(budget.daily_used)} of ${number(budget.daily_limit)} uses today · at least ${number(budget.minimum_turn_spacing)} turns between recalls. ${budget.available ? 'Budget available.' : 'Budget currently unavailable.'} Reading this page does not spend the budget.` : '';
    }
    function acceptReports(data, token) {
        if (!owned(data.scope, token) || !Array.isArray(data.jobs) || !Array.isArray(data.reports)) throw new Error('Altera returned reflection reports for a different context.');
        for (const job of data.jobs) if (String(job.avatar_id || '') !== token.context.avatar_id || job.session_id !== token.context.session_id) throw new Error('A reflection job belongs to another context.');
        const entries = Array.isArray(data.current_notebook?.entries) ? data.current_notebook.entries : [];
        if (entries.some(entry => !owned(entry, token))) throw new Error('A derived notebook entry belongs to another context.');
        jobs = data.jobs; reports = data.reports; notebook = entries;
    }
    function invalidateSourceReview() { sourceSerial++; reviewed = null; byId('storySourcePreview').replaceChildren(); }
    function chosenEvidence() {
        return ['storySourceOne', 'storySourceTwo'].map(field => {
            const selected = byId(field).value, source = sourceChoices.find(row => String(row.message_id) === selected);
            return source ? [source.message_id, source.session_id, source.role, source.preview, source.timestamp] : null;
        });
    }
    function renderSourceChoices() {
        for (const [field, placeholder] of [['storySourceOne', 'Choose a message…'], ['storySourceTwo', 'No second message']]) {
            const select = byId(field), selected = select.value;
            const blank = el('option', placeholder); blank.value = ''; select.replaceChildren(blank);
            for (const source of sourceChoices) {
                const preview = Array.from(text(source.preview).replace(/\s+/g, ' ').trim());
                const label = (source.role === 'user' ? 'You' : name()) + ' · ' + (preview.length ? preview.slice(0, 100).join('') + (preview.length > 100 ? '…' : '') : '(message without a preview)');
                const option = el('option', label); option.value = String(source.message_id); select.append(option);
            }
            if (selected && !sourceChoices.some(source => String(source.message_id) === selected)) { const unavailable = el('option', 'Previously selected message is unavailable; choose and review a current source.'); unavailable.value = selected; select.append(unavailable); }
            select.value = selected;
        }
    }
    function clearSourceChoices(notice) {
        sourceChoices = []; sourcesReady = false; invalidateSourceReview(); renderSourceChoices(); byId('storySourceStatus').textContent = notice;
    }
    function acceptSourceChoices(data, token) {
        if (!owned(data.scope, token) || !Array.isArray(data.sources) || data.sources.length > 60 || data.sources.some(source => !source || !Number.isSafeInteger(source.message_id) || source.message_id <= 0 || source.session_id !== token.context.session_id || !['user', 'assistant'].includes(source.role) || typeof source.preview !== 'string') || new Set(data.sources.map(source => source.message_id)).size !== data.sources.length) throw new Error('Altera returned messages for a different companion, conversation or dataset.');
        const before = JSON.stringify(chosenEvidence());
        sourceChoices = data.repair_pending ? [] : data.sources; sourcesReady = sourceChoices.length > 0; renderSourceChoices();
        if (before !== JSON.stringify(chosenEvidence())) invalidateSourceReview();
        byId('storySourceStatus').textContent = data.repair_pending ? 'The conversation changed. Review its memory controls before saving a moment.' : sourcesReady ? 'Choose from recent eligible messages, then review the full source excerpts.' : 'No eligible messages yet. Continue the conversation in chat, then refresh saved state.';
    }
    async function readSettings(token) {
        const data = await request(`/api/reflection/preferences?session_id=${token.context.session_id}`, token);
        if (!owned(data.preferences, token) || !Number.isSafeInteger(data.preferences.revision) || !data.provider) throw new Error('Altera returned reflection preferences for another context.');
        preferences = data.preferences; provider = data.provider; renderPreferences();
    }
    async function refresh() {
        if (busy) return;
        if (!context || !Number.isSafeInteger(context.session_id)) return;
        if (context.dataset_epoch !== epoch()) { suspend(); return; }
        if (reading) return reading.promise;
        const token = mark(), entry = {}; reading = entry; sync(); message('Reading saved episodes and reflection state…');
        entry.promise = (async () => {
            const results = await Promise.allSettled([
                request(`/api/episodes?session_id=${token.context.session_id}`, token),
                readSettings(token),
                request(`/api/reflection/reports?session_id=${token.context.session_id}`, token),
                request(`/api/episodes/sources?session_id=${token.context.session_id}`, token),
            ]);
            if (!valid(token)) return;
            const errors = [];
            try { if (results[0].status === 'fulfilled') acceptEpisodes(results[0].value, token); else throw results[0].reason; } catch (error) { episodes = []; errors.push(error.message); }
            if (results[1].status === 'rejected') { preferences = provider = null; byId('storyProvider').textContent = ''; byId('storyNetworkText').textContent = ''; byId('storyReflectionState').textContent = 'Preferences unavailable; unsaved edits kept'; errors.push(results[1].reason.message); }
            try { if (results[2].status === 'fulfilled') acceptReports(results[2].value, token); else throw results[2].reason; } catch (error) { reports = []; jobs = []; notebook = []; errors.push(error.message); }
            try { if (results[3].status === 'fulfilled') acceptSourceChoices(results[3].value, token); else throw results[3].reason; } catch (error) { clearSourceChoices('Recent messages are unavailable. Refresh saved state to try again.'); errors.push(error.message); }
            uncertain = false; renderEpisodes(); renderReports();
            message(errors.length ? errors.join(' ') : `${name()} · saved state refreshed. No reflection was requested.`);
        })().finally(() => { if (valid(token) && reading === entry) { reading = null; sync(); } });
        return entry.promise;
    }
    async function mutate(work, success, target = message) {
        if (busy || uncertain || !context) return false;
        const token = mark(); busy = true; sync(); renderEpisodes(); renderReports();
        try { assertValid(token); await work(token); assertValid(token); target(success); if (options.onChanged) { try { await options.onChanged({...token.context}); } catch (_) { /* Saved action does not depend on a host callback. */ } } return true; }
        catch (error) { if (valid(token)) { uncertain = true; target('Not confirmed: ' + error.message + ' Refresh saved state before another change.'); } return false; }
        finally { if (valid(token)) { busy = false; sync(); renderEpisodes(); renderReports(); } }
    }
    async function patchEpisode(episodeId, changes) {
        const item = episodes.find(row => row.id === episodeId); if (!item || item.status === 'forgotten') return false;
        return mutate(async token => {
            const data = await request(`/api/episodes/${encodeURIComponent(item.id)}`, token, json('PATCH', {...body(token), revision: item.revision, ...changes}));
            if (!owned(data.scope, token) || data.episode?.id !== item.id || String(data.episode.avatar_id || '') !== token.context.avatar_id) throw new Error('The saved episode response did not match this companion.');
            episodes = episodes.map(row => row.id === item.id ? data.episode : row);
        }, 'Episode saved for ' + name() + '.');
    }
    function dialog(title, description, fields, actionLabel, action, reference = null) {
        if (dialogBox || busy || uncertain) return;
        const token = mark(), previous = document.activeElement, box = el('dialog', null, 'story-dialog'); dialogBox = box;
        const heading = el('h2', title); heading.id = 'storyActionTitle'; box.setAttribute('aria-labelledby', heading.id);
        const form = el('form'), feedback = el('p'); feedback.setAttribute('role', 'status');
        const inputs = {};
        for (const field of fields) { const label = el('label', field.label), input = el(field.tag || 'input'); for (const choice of field.options || []) { const option = el('option', choice[1]); option.value = choice[0]; input.append(option); } input.value = field.value ?? ''; if (field.max) input.maxLength = field.max; if (field.required) input.required = true; if (field.type) input.type = field.type; if (field.min) input.min = field.min; if (field.maxValue) input.max = field.maxValue; if (field.readOnly) input.readOnly = true; label.append(input); inputs[field.key] = input; form.append(label); }
        const draft = {slot: slot(), origin: origin(), title, reference, inputs, baseline: '', pending: null};
        draft.baseline = JSON.stringify(dialogValues(draft)); dialogDraft = draft;
        const actions = el('div', null, 'story-actions'), cancel = button('Cancel', () => box.close()), submit = el('button', actionLabel); submit.type = 'submit'; actions.append(cancel, submit); form.append(feedback, actions);
        box.append(heading, el('p', description, 'story-dialog-description'), form); let saving = false;
        box.addEventListener('cancel', event => { if (saving || !owner.active) event.preventDefault(); });
        box.addEventListener('close', () => { box.remove(); if (dialogBox === box) dialogBox = null; if (dialogDraft === draft) dialogDraft = null; if (valid(token)) { const replacement = previous?.id ? byId(previous.id) : null; if (replacement) replacement.focus(); else if (previous?.isConnected !== false) previous?.focus(); else byId('storyEpisodes')?.focus(); } });
        listen(form, 'submit', async event => { event.preventDefault(); if (saving || !valid(token)) return; saving = true; submit.disabled = cancel.disabled = true; feedback.textContent = 'Saving…';
            const submitted = JSON.stringify(dialogValues(draft)); draft.pending = {fields: JSON.parse(submitted), reference: draft.reference};
            try { const values = {}; for (const [field, input] of Object.entries(inputs)) values[field] = String(input.value).trim(); const done = await action(values, token); if (done && valid(token)) { draft.pending = null; draft.baseline = submitted; if (JSON.stringify(dialogValues(draft)) === submitted) box.close(); else feedback.textContent = 'Earlier edits saved. Your newer edits are still unsaved.'; } else if (valid(token)) { feedback.textContent = 'The change was not confirmed. Close this dialog and refresh saved state.'; submit.disabled = true; } }
            catch (error) { if (valid(token)) feedback.textContent = error.message; }
            finally { saving = false; if (valid(token)) { cancel.disabled = false; if (!uncertain) submit.disabled = false; } }
        });
        document.body.append(box); box.showModal(); (Object.values(inputs)[0] || cancel).focus();
    }
    function editEpisode(episodeId) {
        const item = episodes.find(row => row.id === episodeId); if (!item) return;
        dialog('Correct this episode', 'Your edit is labelled as curation. Original source ranges and hashes remain inspectable; source integrity does not prove a claim is true.', [
            {key: 'title', label: 'Title', value: item.title, max: 160, required: true}, {key: 'summary', label: 'Your description', value: item.summary, tag: 'textarea', max: 1200},
            {key: 'kind', label: 'Source label', value: item.kind, tag: 'select', options: [['actual_exchange', 'Actual exchange'], ['imagined_scene', 'Imagined scene'], ['derived_summary', 'Derived candidate / your interpretation']]},
            {key: 'uncertainty', label: 'Uncertainty', value: item.uncertainty, tag: 'textarea', max: 400}, {key: 'salience', label: 'Salience (1–5)', value: item.salience, type: 'number', min: 1, maxValue: 5},
        ], 'Save correction', values => { const salience = Number(values.salience); if (!values.title || !Number.isInteger(salience) || salience < 1 || salience > 5) throw new Error('Enter a title and salience from 1 to 5.'); return patchEpisode(episodeId, {...values, salience}); }, {type: 'episode', id: item.id, revision: item.revision});
    }
    async function reviewRestored(episodeId) {
        const item = episodes.find(row => row.id === episodeId), token = mark();
        if (!item || !context) return;
        try {
            const query = new URLSearchParams({...body(token), revision: String(item.revision)});
            const data = await request(`/api/episodes/${encodeURIComponent(item.id)}/restore-review?${query}`, token);
            if (!owned(data.scope, token) || data.episode?.id !== item.id || String(data.episode?.avatar_id || '') !== token.context.avatar_id ||
                data.episode.status !== 'review_ready' || !Array.isArray(data.sources) || !Array.isArray(data.reviewed_sources)) throw new Error('The restored review did not match this companion.');
            const stored = data.episode;
            const evidence = data.sources.map(source => `Message ${source.message_id} · ${source.role}: ${text(source.excerpt)}`).join('\n\n');
            dialog('Review restored story moment', `Your saved ${text(stored.kind).replaceAll('_', ' ')}: ${text(stored.title)}\n${text(stored.summary)}\nUncertainty: ${text(stored.uncertainty) || 'not recorded'}\n\nCurrent matching source excerpts:\n${evidence}\n\nRestoring keeps your saved curation and pins. It does not confirm real-world facts or override a changed recall boundary.`, [], 'Restore saved curation', () => mutate(async current => {
                if (!valid(token) || current.key !== token.key) throw new Error('The displayed data changed. Review this moment again.');
                const saved = await request(`/api/episodes/${encodeURIComponent(item.id)}/revalidate`, current, json('POST', {
                    ...body(current), revision: item.revision, confirm: 'restore-curation', reviewed_sources: data.reviewed_sources}));
                if (!owned(saved.scope, current) || saved.episode?.id !== item.id || saved.episode.dataset_epoch !== current.context.dataset_epoch || String(saved.episode.avatar_id || '') !== current.context.avatar_id) throw new Error('The restored moment response did not match this dataset.');
                episodes = [...episodes.filter(row => row.id !== item.id), saved.episode]; renderEpisodes();
            }, 'Saved curation restored with matching source evidence.'), {type: 'episode_restore', id: item.id, revision: item.revision});
        } catch (error) { if (valid(token)) message(error.message); }
    }

    function forgetEpisode(episodeId) {
        const item = episodes.find(row => row.id === episodeId); if (!item) return;
        dialog('Forget this episode?', 'This removes the episode, clears this companion’s notebooks and their history, and starts a fresh recall boundary. Earlier chat remains visible but is no longer sent to the model. Other saved memories remain; existing backups keep their original contents.', [], 'Forget episode', () => mutate(async token => {
            const data = await request(`/api/episodes/${encodeURIComponent(item.id)}`, token, json('DELETE', {...body(token), revision: item.revision, confirm: 'forget'}));
            if (!owned(data.scope, token) || data.episode?.id !== item.id) throw new Error('The Forget response did not match this companion.');
            // Forget can advance the companion's recall boundary. Drop cached derived text immediately.
            episodes = []; reports = []; jobs = []; notebook = []; clearSourceChoices('Refreshing eligible messages after Forget…'); sync(); renderEpisodes(); renderReports();
            const refreshed = await Promise.allSettled([request(`/api/episodes?session_id=${token.context.session_id}`, token), request(`/api/reflection/reports?session_id=${token.context.session_id}`, token), request(`/api/episodes/sources?session_id=${token.context.session_id}`, token)]);
            assertValid(token);
            const errors = [];
            try { if (refreshed[0].status === 'fulfilled') acceptEpisodes(refreshed[0].value, token); else throw refreshed[0].reason; } catch (error) { errors.push(error.message); }
            try { if (refreshed[1].status === 'fulfilled') acceptReports(refreshed[1].value, token); else throw refreshed[1].reason; } catch (error) { errors.push(error.message); }
            try { if (refreshed[2].status === 'fulfilled') acceptSourceChoices(refreshed[2].value, token); else throw refreshed[2].reason; } catch (error) { clearSourceChoices('Recent messages are unavailable. Refresh saved state to try again.'); errors.push(error.message); }
            if (errors.length) reflectionMessage('The episode was forgotten. Some saved context could not be reloaded: ' + errors.join(' '));
        }, 'Episode forgotten for ' + name() + '.'), {type: 'episode_forget', id: item.id, revision: item.revision});
    }
    function sourceIds() {
        if (!sourcesReady) throw new Error('Recent messages are unavailable. Refresh saved state before choosing messages.');
        if (!byId('storySourceOne').value) throw new Error('Choose the first message before reviewing sources.');
        return ['storySourceOne', 'storySourceTwo'].map(value => byId(value).value.trim()).filter(Boolean).map(value => {
            const parsed = Number(value); if (!Number.isSafeInteger(parsed) || parsed <= 0 || !sourceChoices.some(source => source.message_id === parsed)) throw new Error('That message is not available in this companion’s recent conversation. Refresh and choose a current message.'); return parsed;
        });
    }
    async function hash(value) { const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)); return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join(''); }
    async function inspectSource(source) {
        const token = mark();
        try {
            const data = await request(`/api/knowledge/messages/${source.message_id}`, token), messageSource = data.message;
            if (data.scope !== (token.context.avatar_id || '_default') || messageSource?.id !== source.message_id || messageSource.session_id !== source.session_id || (source.sha256 && await hash(text(messageSource.content)) !== source.sha256)) throw new Error('The source changed or belongs to another context. Refresh saved state.');
            assertValid(token);
            const characters = Array.from(text(messageSource.content)), start = Number.isSafeInteger(source.start) ? source.start : 0, end = Number.isSafeInteger(source.end) ? source.end : Math.min(characters.length, start + 2000);
            dialog('Original source excerpt', `Conversation ${source.session_id} · message ${source.message_id} · ${text(source.role)} · characters ${start}–${end}. Source integrity does not verify a life event.`, [{key: 'source', label: 'Source text', value: characters.slice(start, end).join(''), tag: 'textarea', readOnly: true}], 'Close', async () => true);
        } catch (error) { if (valid(token)) message(error.message); }
    }
    async function reviewSources() {
        if (busy || uncertain || reading || !context) return;
        const token = mark(), requestNumber = ++sourceSerial; reviewed = null; byId('storySourcePreview').replaceChildren();
        try {
            const ids = sourceIds(); if (!ids.length || new Set(ids).size !== ids.length) throw new Error('Choose two different messages, or leave the second message empty.');
            const results = await Promise.all(ids.map(sourceId => request(`/api/knowledge/messages/${sourceId}`, token)));
            if (!valid(token) || requestNumber !== sourceSerial) return;
            const sources = [];
            for (let index = 0; index < results.length; index++) {
                const data = results[index], source = data.message;
                if (data.scope !== (token.context.avatar_id || '_default') || !source || source.id !== ids[index] || source.session_id !== token.context.session_id || !['user', 'assistant'].includes(source.role)) throw new Error('A source is outside this companion’s current conversation.');
                const characters = Array.from(text(source.content)), excerpt = characters.slice(0, 2000).join(''); if (!excerpt) throw new Error('An empty message cannot be a source.');
                sources.push({message_id: source.id, session_id: source.session_id, role: source.role, start: 0, end: Math.min(characters.length, 2000), sha256: await hash(text(source.content)), status: 'current', excerpt});
            }
            if (!valid(token) || requestNumber !== sourceSerial || JSON.stringify(ids) !== JSON.stringify(sourceIds())) return;
            reviewed = {key: token.key, ids: JSON.stringify(ids), sources};
            const preview = byId('storySourcePreview'); preview.append(el('p', 'These exact ranges will be saved (up to the first 2,000 characters per message).', 'story-muted')); for (const source of sources) preview.append(sourceBlock(source));
            message('Sources reviewed. Confirm the title, label and description before saving.');
        } catch (error) { if (valid(token) && requestNumber === sourceSerial) message(error.message); }
    }
    async function createEpisode() {
        if (busy || uncertain || !context) return false;
        const token = mark();
        try {
            let payload;
            if (createAttempt) { if (createAttempt.key !== token.key) throw new Error('The saved draft belongs to another context.'); payload = createAttempt.payload; }
            else {
                if (!reviewed || reviewed.key !== token.key || reviewed.ids !== JSON.stringify(sourceIds())) throw new Error('Review the current source text before saving.');
                const title = byId('storyTitle').value.trim(); if (!title) throw new Error('Give this episode a title.');
                const salience = Number(byId('storySalience').value); if (!Number.isInteger(salience) || salience < 1 || salience > 5) throw new Error('Choose salience from 1 to 5.');
                payload = {...body(token), request_id: id(), title, kind: byId('storyKind').value, summary: byId('storySummary').value.trim(), uncertainty: byId('storyUncertainty').value.trim(), salience, in_story: byId('storyInStory').checked,
                    sources: reviewed.sources.map(source => ({message_id: source.message_id, start: source.start, end: source.end, sha256: source.sha256}))};
                createAttempt = {key: token.key, payload, form: JSON.stringify(values(episodeFields))};
            }
            busy = true; sync();
            const data = await request('/api/episodes', token, json('POST', payload));
            if (!owned(data.scope, token) || !data.episode || String(data.episode.avatar_id || '') !== token.context.avatar_id) throw new Error('The new episode response did not match this companion.');
            episodes = [...episodes.filter(row => row.id !== data.episode.id), data.episode];
            const unchanged = JSON.stringify(values(episodeFields)) === createAttempt.form;
            if (unchanged) clearDraft(); else { episodeBaseline = createAttempt.form; createAttempt = null; episodeSlot = slot(); invalidateSourceReview(); }
            renderEpisodes(); message('Episode saved as your curation for ' + name() + '.' + (unchanged ? '' : ' Your newer edits are still unsaved; review sources again before saving them.')); if (options.onChanged) { try { await options.onChanged({...token.context}); } catch (_) { /* A host callback cannot undo a saved episode. */ } } return true;
        } catch (error) { if (valid(token)) message(createAttempt ? 'Save not confirmed: ' + error.message + ' Retry the same save to retrieve its result; edited form fields will not create another episode.' : error.message); return false; }
        finally { if (valid(token)) { busy = false; sync(); renderEpisodes(); } }
    }
    function discardAttempt() {
        if (!createAttempt) return;
        dialog('Stop retrying this save?', 'The server may already have saved this episode. Review All episodes before saving a replacement. This releases the frozen local draft; it does not delete a saved episode.', [], 'Keep draft for editing', async () => {
            createAttempt = null; reviewed = null; byId('storySourcePreview').replaceChildren(); sync(); message('Draft released for editing. Refresh All episodes to check for the earlier save, then review sources before saving again.'); return true;
        });
    }
    async function reloadReflection() {
        if (busy || !context) return;
        const token = mark();
        try {
            if (reflectionDirty() && !await prepareReplacement()) return;
            assertValid(token);
            const before = JSON.stringify(values(reflectionFields));
            await readSettings(token); assertValid(token);
            if (reflectionDirty() && (JSON.stringify(values(reflectionFields)) !== before)) throw new Error('Your edits changed during reload. Keep them open and reload again.');
            renderPreferences(true); reflectionSlot = slot();
            reflectionMessage('Saved reflection preferences reloaded.' + (uncertain ? ' Refresh all saved state before another change.' : ''));
        } catch (error) { if (valid(token)) reflectionMessage(error.message); }
        finally { if (valid(token)) sync(); }
    }
    async function saveReflection() {
        if (!preferences || !provider) return false;
        if (!reflectionView || reflectionView.identity !== viewIdentity()) { reflectionMessage('Saved preferences or the provider changed. Keep your edits, then explicitly reload and review the current provider before saving.'); return false; }
        const submitted = JSON.stringify(values(reflectionFields)), submittedProvider = provider.fingerprint;
        let settings;
        try {
            const interval = Number(byId('storyInterval').value), cooldown = Number(byId('storyCooldown').value), cap = Number(byId('storyDailyCap').value);
            if (!Number.isInteger(interval) || interval < 60 || interval > 10080 || !Number.isInteger(cooldown) || cooldown < 60 || cooldown > 10080 || !Number.isInteger(cap) || cap < 0 || cap > 2) throw new Error('Choose interval and cooldown from 60 to 10,080 minutes and a daily cap from 0 to 2.');
            const timezone = byId('storyTimezone').value.trim(); new Intl.DateTimeFormat(undefined, {timeZone: timezone}).format();
            settings = {enabled: byId('storyReflectionEnabled').checked, timezone, interval_minutes: interval, cooldown_minutes: cooldown, daily_cap: cap, quiet_enabled: byId('storyQuietEnabled').checked,
                quiet_start: parseMinute(byId('storyQuietStart').value), quiet_end: parseMinute(byId('storyQuietEnd').value), provider_approval: provider.network && byId('storyNetworkApproval').checked ? provider.fingerprint : null};
        } catch (error) { reflectionMessage(error.message); return false; }
        return mutate(async token => {
            const data = await request('/api/reflection/preferences', token, json('PATCH', {...body(token), revision: preferences.revision, settings}));
            if (!owned(data.preferences, token) || !data.provider) throw new Error('The preferences response did not match this companion.'); preferences = data.preferences; provider = data.provider;
            reflectionBaseline = submitted;
            reflectionSlot = slot();
            if (JSON.stringify(values(reflectionFields)) === submitted) renderPreferences(true);
            else if (provider.fingerprint === submittedProvider) reflectionView = {...reflectionView, identity: viewIdentity(), revision: preferences.revision};
        }, 'Reflection preferences saved. Presence preferences are separate.', reflectionMessage);
    }
    async function runReflection() {
        if (busy || uncertain || !preferences?.enabled || preferences.needs_review || preferences.daily_cap === 0 || jobs.some(job => ['pending', 'running'].includes(job.state))) return false;
        const token = mark(); runId ||= id(); busy = true; sync();
        try {
            const data = await request('/api/reflection/run', token, json('POST', {...body(token), request_id: runId}));
            if (!owned(data.job, token)) throw new Error('The reflection job belongs to another context.');
            jobs = [...jobs.filter(job => job.id !== data.job.id), data.job]; runId = null; runSlot = slot(); renderReports(); reflectionMessage('Reflection queued. Refresh run reports when you want to inspect the outcome.'); return true;
        } catch (error) { if (valid(token)) reflectionMessage('Request not confirmed: ' + error.message + ' Retry requests the same job.'); return false; }
        finally { if (valid(token)) { busy = false; sync(); renderReports(); } }
    }
    async function refreshReports() {
        if (busy || !context) return; const token = mark();
        try { const data = await request(`/api/reflection/reports?session_id=${token.context.session_id}`, token); acceptReports(data, token); renderReports(); reflectionMessage('Saved reports refreshed. No reflection was requested.'); }
        catch (error) { if (valid(token)) reflectionMessage(error.message); }
        finally { if (valid(token)) sync(); }
    }
    async function patchNote(noteId, changes) {
        const entry = notebook.find(row => row.id === noteId); if (!entry) return false;
        return mutate(async token => {
            const data = await request(`/api/reflection/notebook/${encodeURIComponent(entry.id)}`, token, json('PATCH', {...body(token), revision: entry.revision, ...changes}));
            if (!data.entry || data.entry.id !== entry.id || !owned(data.entry, token)) throw new Error('The derived note response did not match the saved entry.');
            notebook = changes.dismiss ? notebook.filter(row => row.id !== entry.id) : notebook.map(row => row.id === entry.id ? data.entry : row);
        }, 'Derived notebook entry updated. Its source provenance remains inspectable.', reflectionMessage);
    }
    function editNote(noteId) { const entry = notebook.find(row => row.id === noteId); if (!entry) return; dialog('Correct this derived note', 'This updates the separate derived notebook layer. Your manually edited notebook text stays separate. The entry retains its original sources and derived label.', [
        {key: 'text', label: 'Corrected note', value: entry.text, tag: 'textarea', max: 1200, required: true}, {key: 'uncertainty', label: 'Uncertainty', value: entry.uncertainty, tag: 'textarea', max: 400},
    ], 'Save correction', values => { if (!values.text) throw new Error('Enter the corrected note.'); return patchNote(noteId, values); }, {type: 'derived_note', id: entry.id, revision: entry.revision}); }
    function dismissNote(noteId) { const entry = notebook.find(row => row.id === noteId); if (!entry) return; dialog('Dismiss this derived note?', 'This removes the derived entry from current recall. Its report remains an inspectable record; it is not independent evidence.', [], 'Dismiss derived note', () => patchNote(noteId, {dismiss: true}), {type: 'derived_note_dismiss', id: entry.id, revision: entry.revision}); }
    function suspend() {
        if (!owner.active) return;
        generation++; scopeChange++; sourceSerial++; abortRequests(); reading = null; busy = false; suspended = true; uncertain = true;
        episodes = []; reports = []; jobs = []; notebook = []; preferences = provider = reviewed = null;
        // Preserve user fields and their historical context, while withholding cached evidence.
        sourceChoices = sourceChoices.map(source => ({message_id: source.message_id, session_id: source.session_id, role: source.role, preview: ''})); sourcesReady = false;
        renderSourceChoices(); byId('storySourcePreview').replaceChildren();
        for (const field of ['Provider', 'NetworkText', 'Budget', 'Boundary', 'ReflectionStatus']) byId('story' + field).textContent = '';
        byId('storyBoundary').hidden = true;
        byId('storySourceStatus').textContent = 'Source text withheld until saved state is refreshed.';
        byId('storyReflectionState').textContent = 'Paused; unfinished edits kept';
        renderEpisodes(); renderReports(); sync(); message('This view was suspended or its dataset changed. Your edits are kept here. Choose a companion to review current saved state.');
    }
    async function init(config = {}) {
        options = {...options, ...config}; if (initialized || !byId('storyPage')) return; initialized = true;
        listen(byId('storyScope'), 'change', () => selectScope(byId('storyScope').value));
        listen(byId('storySearch'), 'input', renderEpisodes); listen(byId('storyFilter'), 'change', renderEpisodes);
        listen(byId('storyRefresh'), 'click', refresh); listen(byId('storyReviewSources'), 'click', reviewSources);
        for (const suffix of ['SourceOne', 'SourceTwo']) for (const event of ['input', 'change']) listen(byId('story' + suffix), event, invalidateSourceReview);
        listen(byId('storyCreateForm'), 'submit', event => { event.preventDefault(); return createEpisode(); });
        listen(byId('storyDiscardAttempt'), 'click', discardAttempt);
        listen(byId('storyReflectionForm'), 'submit', event => { event.preventDefault(); return saveReflection(); });
        listen(byId('storyReloadReflection'), 'click', reloadReflection); listen(byId('storyRunReflection'), 'click', runReflection); listen(byId('storyRefreshReports'), 'click', refreshReports);
        window.addEventListener('pagehide', suspend);
        window.addEventListener('altera:dataset-changed', suspend);
        window.addEventListener('beforeunload', event => { if (drafts().length) { event.preventDefault(); event.returnValue = ''; } });
        sync();
        try {
            const data = await request('/api/knowledge/scopes'); if (!Array.isArray(data.scopes)) throw new Error('Altera returned an unreadable companion list.'); scopes = data.scopes;
            const select = byId('storyScope'); select.replaceChildren(); for (const scope of scopes) { const option = el('option', scope.name); option.value = scope.id; select.append(option); }
            const url = new URL(window.location.href), requested = url.searchParams.get('scope'), session = Number(url.searchParams.get('session_id'));
            await selectScope(scopes.some(scope => scope.id === requested) ? requested : scopes.some(scope => scope.id === data.active) ? data.active : scopes[0]?.id, session);
        } catch (error) { if (owner.active) message(error.message + ' Reload this page to try again.'); }
    }
    const owner = AlteraPage.component('story', {
        stop() { generation++; scopeChange++; sourceSerial++; abortRequests(); suspended = true; return true; },
        clear() {
            if (dialogDraft) for (const input of Object.values(dialogDraft.inputs)) input.value = '';
            if (dialogBox) dialogBox.close(); dialogDraft = dialogBox = null;
            scopes = []; context = draftOrigin = reflectionView = null; episodes = []; preferences = provider = null; reports = []; jobs = []; notebook = []; sourceChoices = []; sourcesReady = false;
            reviewed = createAttempt = runId = reading = null; busy = false; uncertain = true; options = {};
            episodeBaseline = reflectionBaseline = episodeSlot = reflectionSlot = runSlot = '';
            for (const field of [...episodeFields, ...reflectionFields, 'Search', 'Filter', 'Scope']) { const input = byId('story' + field); if (input) { input.value = ''; input.checked = false; input.disabled = true; } }
            for (const field of ['SourceOne', 'SourceTwo', 'Scope', 'Cards', 'Reports', 'SourcePreview', 'Status', 'ReflectionStatus', 'Provider', 'NetworkText', 'Budget', 'Boundary', 'SourceStatus', 'ReflectionState', 'Count']) { const node = byId('story' + field); if (node) { node.replaceChildren(); node.textContent = ''; } }
            for (const link of ['storyMemoryLink', 'storyFooterMemoryLink']) byId(link).href = '/memory';
            sync(); return true;
        }
    });
    return {init: tracked(init), selectScope: tracked(selectScope), refresh: tracked(refresh), reviewSources: tracked(reviewSources), createEpisode: tracked(createEpisode), patchEpisode: tracked(patchEpisode), editEpisode: tracked(editEpisode), forgetEpisode: tracked(forgetEpisode), reviewRestored: tracked(reviewRestored), saveReflection: tracked(saveReflection), reloadReflection: tracked(reloadReflection), runReflection: tracked(runReflection), refreshReports: tracked(refreshReports), patchNote: tracked(patchNote),
        state: () => ({context, episodes, preferences, provider, reports, jobs, notebook, busy, uncertain, createAttempt, runId})};
})();
AlteraPage.ready(() => window.StoryUI.init());
