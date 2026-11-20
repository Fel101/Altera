window.KnowledgeUI = (() => {
    const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
    const byId = id => document.getElementById(id);
    const explanation = 'This clears this person’s notes and their history, and starts a fresh recall boundary. Earlier chat stays visible but is no longer sent to the model. Other saved memories stay. Existing backups keep their original contents.';
    let page = null, scopes = [], memories = [], loading = false, serial = 0, remembering = false, rebuildPoll = null;
    const dialogs = new Set(), downloads = new Set(), controllers = new Set();
    let newMemoryOrigin = null;
    function captureMemoryDraft() {
        const input = byId('knowledgeContent');
        if (!input?.value.trim()) { newMemoryOrigin = null; return; }
        if (!newMemoryOrigin) newMemoryOrigin = {scope: scope(), name: scopeName(scope())};
    }
    const tracked = action => (...args) => owner.run(() => action(...args)).catch(error => { if (owner.active) throw error; });
    const path = scope => `/api/knowledge/scopes/${encodeURIComponent(scope)}`;
    const datasetEpoch = () => typeof AlteraAPI === 'undefined' ? window.__DATA_EPOCH__ : AlteraAPI.epoch;
    async function api(url, options = {}) {
        owner.assert(); const controller = new AbortController(); controllers.add(controller);
        try {
            const response = await fetch(url, {cache: 'no-store', ...options, signal: controller.signal, headers: {'Content-Type': 'application/json', ...options.headers}});
            let data;
            try { data = await response.json(); } catch { throw new Error('Altera did not return a readable memory response. Please try again.'); }
            owner.assert(); if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
            return data;
        } finally { controllers.delete(controller); }
    }
    function message(text) { if (owner.active && byId('knowledgeStatus')) byId('knowledgeStatus').textContent = text; }
    function scope() {
        const selected = byId('knowledgeScope').value;
        if (!scopes.some(item => item.id === selected)) throw new Error('Choose a companion to continue.');
        return selected;
    }
    function selectionControls(enabled) {
        page.querySelectorAll('button:not([data-export]), select').forEach(control => { control.disabled = !enabled && control.id !== 'knowledgeScope'; });
        byId('knowledgeContent').disabled = !enabled;
    }
    function scopeName(id) { return scopes.find(s => s.id === id)?.name || 'this companion'; }
    function button(label, action) { const b = el('button', label); b.type = 'button'; b.addEventListener('click', tracked(action)); return b; }
    function dialog(title, description, value, actionLabel, action) {
        if (document.querySelector('.knowledge-dialog[open]')) return;
        const previous = document.activeElement;
        const box = el('dialog', null, 'backup-dialog knowledge-dialog');
        const heading = el('h2', title); heading.id = 'knowledgeActionTitle'; box.setAttribute('aria-labelledby', heading.id);
        const text = el('p', description), feedback = el('p'); feedback.setAttribute('role', 'status');
        const form = el('form'), actions = el('div', null, 'knowledge-actions');
        let input = null, busy = false;
        if (value !== null) {
            const label = el('label', 'Memory'); input = el('textarea'); input.value = value; input.maxLength = 10000; input.required = true;
            label.append(input); form.append(label);
        }
        const cancel = button('Cancel', () => box.close());
        const submit = el('button', actionLabel); submit.type = 'submit';
        actions.append(cancel, submit); form.append(feedback, actions); box.append(heading, text, form);
        box.addEventListener('cancel', e => { if (busy) e.preventDefault(); });
        const draft = {box}; dialogs.add(draft);
        box.addEventListener('close', () => { dialogs.delete(draft); box.remove(); if (owner.active) previous?.focus(); });
        form.addEventListener('submit', tracked(async e => {
            e.preventDefault(); if (busy) return;
            if (input && !input.value.trim()) { feedback.textContent = 'Enter a memory first.'; return; }
            busy = true; cancel.disabled = submit.disabled = true; feedback.textContent = 'Saving…';
            try { await action(input?.value.trim()); box.close(); }
            catch (error) { feedback.textContent = error.message; }
            finally { busy = false; cancel.disabled = submit.disabled = false; }
        }));
        document.body.append(box); box.showModal(); (input || cancel).focus();
    }
    async function rememberMessageWork(id) {
        const data = await api(`/api/knowledge/messages/${id}`);
        dialog(`Remember for ${data.name}`, 'Review the fact before saving. It belongs only to this companion and keeps a link to this message.', data.message.content, 'Remember', async content => {
            const intent = {content, type: 'fact', source_message_id: id, session_id: data.message.session_id};
            const result = await api(path(data.scope) + '/memories', {method: 'POST', body: JSON.stringify(intent)});
            if (typeof showReplyNotice === 'function') showReplyNotice(result.status === 'already_processed'
                ? 'This save was already processed. Review saved memories.' : `Memory saved for ${data.name}.`);
        });
    }
    function reviewNotice(text) {
        if (!owner.active) return;
        if (typeof showReplyNotice !== 'function') return;
        showReplyNotice(text);
        const id = typeof Companion !== 'undefined' ? Companion.current?.id : null;
        const link = el('a', 'Review memories');
        link.href =`/memory?scope=${encodeURIComponent(id || '_default')}`;
        byId('replyNotice')?.append(link);
    }
    async function loadWork() {
        const selected = scope(), request = ++serial; loading = true;
        page.querySelectorAll('button:not([data-export]), select').forEach(control => { control.disabled = true; });
        byId('knowledgeCards').replaceChildren(el('p', 'Loading memories…', 'knowledge-muted'));
        byId('knowledgeRepair').hidden = byId('knowledgeBoundary').hidden = true;
        byId('knowledgeReportContent').replaceChildren();
        byId('knowledgeReportStatus').textContent = 'Show or download a fresh report for ' + scopeName(selected) + '.';
        message('Loading memories…');
        try {
            const data = await api(path(selected) + '/memories');
            if (request !== serial) return;
            const epoch = typeof AlteraAPI === 'undefined' ? window.__DATA_EPOCH__ : AlteraAPI.epoch;
            if (data.dataset_epoch && epoch && data.dataset_epoch !== epoch) throw new Error('Altera’s data changed. Reload before reviewing memories.');
            memories = data.memories.map(memory => ({...memory, _epoch: data.dataset_epoch || epoch, _serial: request, _through: data.review_through}));
            byId('knowledgeRepair').hidden = !data.repair_pending && !data.repair_available;
            byId('knowledgeRepairTitle').textContent = data.repair_pending ? 'The conversation changed' : 'Some automatic memories need sources';
            byId('knowledgeRepairText').textContent = data.repair_pending ? 'Old derived facts and notes are paused. Rebuild automatic memories from the current chat to use them again. Manually added or corrected facts stay; changed sources need review.' :
                'Older or changed automatic memories need review. Rebuild them from the current eligible chat. This clears the old notebook and its history; memories you added or corrected stay.';
            const rebuild = data.rebuild, rebuilding = rebuild?.state === 'running';
            byId('repairKnowledge').disabled = rebuilding;
            if (rebuilding) {
                byId('knowledgeRepair').hidden = false;
                byId('knowledgeRepairTitle').textContent = 'Rebuilding automatic memories';
                byId('knowledgeRepairText').textContent = 'The chat model is reading the chat in the background. You can keep chatting; derived memories and notes come back when it finishes.';
                clearTimeout(rebuildPoll);
                rebuildPoll = setTimeout(() => { if (scope() === selected && request === serial) load(); }, 4000);
            } else if (data.repair_pending && rebuild?.error && ['changed', 'error'].includes(rebuild.state)) {
                byId('knowledgeRepairText').textContent = rebuild.error;
            }
            const boundary = byId('knowledgeBoundary'); boundary.hidden = !data.recall_after;
            boundary.textContent = 'A fresh recall boundary is active. Earlier chat stays visible; the model uses newer messages and remaining memories.';
            const noteSources = byId('knowledgeNoteSources'), source = data.notes?.sources;
            noteSources.textContent = source?.source_kind === 'conversation' ? `Conversation ${source.source_session_id}, messages after ${source.source_after || 0} through ${source.source_through}.` +
                (source.includes_user_edits ? ' Includes edits made by you.' : '') + (source.includes_legacy_notes ? ' Includes earlier notes with unrecorded sources.' : '') :
                source?.source_kind === 'user-edited' ? 'These notes include edits made by you.' :
                Object.values(data.notes?.words || {}).some(count => count > 0) ? 'Older notebook: a source range was not recorded. Correcting or forgetting clears it and its history.' : 'No companion notebook is being used yet.';
            renderHeadings(); render(); message(`${scopeName(selected)} · ${memories.length} ${memories.length === 1 ? 'memory' : 'memories'}`); return true;
        } catch (error) { if (request === serial) { memories = []; render(); message(error.message); } return false; }
        finally { if (request === serial) { loading = false; selectionControls(true); } }
    }
    function renderReport(report) {
        const box = byId('knowledgeReportContent'); box.replaceChildren();
        const word = (count, singular, plural = `${singular}s`) => count === 1 ? singular : plural;
        const saved = report.summary.stored_memories, candidates = report.summary.history_candidates;
        box.append(el('h3', report.scope.name), el('p', `Snapshot ${report.generated_at} · ${report.summary.eligible_memories} of ${saved} saved ${word(saved, 'memory', 'memories')} eligible · ${candidates} conversation ${word(candidates, 'candidate')}`, 'knowledge-report-status'));
        const reading = el('details'); reading.append(el('summary', 'How to read this report'));
        for (const text of report.limitations) reading.append(el('p', text, 'knowledge-muted'));
        box.append(reading);
        function entry(item, target) {
            const article = el('article', null, 'knowledge-report-item'), eligible = item.eligibility;
            if (item.origin === 'derived_note') article.append(el('h4', item.id === 'about_you' ? 'About you' : 'Your story together'));
            if (item.origin === 'derived_reflection') article.append(el('h4', item.section === 'about_you' ? 'Reflection about you' : 'Reflection on your story'));
            article.append(el('p', `${eligible.eligible ? 'Eligible input' : 'Stored, excluded from current recall'} · ${item.origin.replaceAll('_', ' ')} · ${item.retrieval.replaceAll('_', ' ')}`, 'knowledge-report-status'), el('p', item.content || '(empty)', 'knowledge-report-text'));
            for (const reason of eligible.reason_codes) article.append(el('p', report.reason_descriptions[reason], 'knowledge-review'));
            if (item.priority) article.append(el('p', `${item.priority.pinned ? 'Pinned' : 'Unpinned'} · importance ${item.priority.salience}/5 · recency factor ${Math.round(item.priority.decay_factor * 100)}%. Priority is separate from confidence.`, 'knowledge-muted'));
            if (item.extracted_by === 'phrases') article.append(el('p', 'Written by the phrase matcher because the chat model was unavailable. Rebuild automatic memories to have the model read it again.', 'knowledge-muted'));
            if (item.uncertainty) article.append(el('p', `Uncertainty: ${item.uncertainty}`, 'knowledge-muted'));
            if (item.sources?.length) {
                const sources = el('details'); sources.append(el('summary', `Message sources (${item.sources.length})`));
                for (const source of item.sources) sources.append(el('div', `Conversation ${source.session_id || '?'} · message ${source.message_id || '?'} · ${source.role || 'unknown role'} · ${source.status}\n${source.content || 'Source unavailable for this companion.'}`, 'knowledge-source'));
                article.append(sources);
            } else if (item.origin === 'user_added' || item.origin === 'user_corrected') article.append(el('p', 'Saved directly or corrected by you. No message source is required.', 'knowledge-muted'));
            if (item.provenance) {
                const p = item.provenance, range = p.latest_update_range;
                article.append(el('p', p.kind === 'derived_aggregate' ? 'Derived aggregate; individual statements have no recorded source citations.' : 'Legacy shared context; message sources were not recorded.', 'knowledge-muted'));
                if (range) article.append(el('p', `Latest update only: conversation ${range.session_id}, after message ${range.after ?? '?'} through ${range.through ?? '?'}. This is not the notebook’s complete source history.`, 'knowledge-muted'));
                if (p.includes_user_edits) article.append(el('p', 'Includes edits made by you.', 'knowledge-muted'));
                if (p.includes_legacy_notes) article.append(el('p', 'Includes earlier notes with unrecorded sources.', 'knowledge-muted'));
                if (p.new_evidence_roles?.join(',') === 'user') article.append(el('p', 'New evidence in the latest notebook update came from user messages; older derived text retains the provenance limits above.', 'knowledge-muted'));
            }
            target.append(article);
        }
        for (const [heading, key] of [['Saved memories', 'memories'], ['Current notebook — derived aggregate', 'notebook'], ['Reflection notebook — derived entries', 'reflection_notebook'], ['Default companion legacy context', 'legacy_context']]) {
            const rows = report[key] || [];
            if (!rows.length && key === 'legacy_context') continue;
            const section = el('details'); section.append(el('summary', `${heading} (${rows.length})`));
            if (!rows.length) section.append(el('p', 'None saved.', 'knowledge-muted'));
            for (const item of rows) entry(item, section);
            box.append(section);
        }
        const conversations = el('details'); conversations.append(el('summary', `Conversation context candidates (${report.conversations.length} ${word(report.conversations.length, 'conversation')})`), el('p', 'These are bounded candidates when a conversation is opened. Token limits, regeneration cutoffs and photo descriptions may change the actual prompt.', 'knowledge-muted'));
        for (const conversation of report.conversations) {
            const count = conversation.history_candidates.length;
            const section = el('details'); section.append(el('summary', `Conversation ${conversation.session_id}${conversation.archived ? ' (archived)' : ''} · ${count} candidate ${word(count, 'message')}`), el('p', `Recall after message ${conversation.recall_after}; current history starts after ${conversation.history_after}.`, 'knowledge-muted'));
            if (conversation.mood_context) {
                const mood = conversation.mood_context, source = el('details');
                source.append(el('summary', `Inferred mood from message ${mood.source.message_id}: ${mood.content}`), el('div', mood.source.content, 'knowledge-source'));
                section.append(source);
            }
            for (const message of conversation.history_candidates) section.append(el('div', `Message ${message.message_id} · ${message.role}\n${message.content}`, 'knowledge-source'));
            conversations.append(section);
        }
        box.append(conversations);
    }
    async function showReportWork() {
        if (loading) return;
        const selected = scope(), request = serial, button = byId('viewKnowledgeReport'); button.disabled = true;
        byId('knowledgeReportStatus').textContent = 'Reading saved context for ' + scopeName(selected) + '…';
        try {
            const report = await api(path(selected) + '/report');
            if (request !== serial) return;
            renderReport(report); byId('knowledgeReportStatus').textContent = 'Current report for ' + scopeName(selected) + '. Eligible does not mean selected or sent to a reply.';
        } catch (error) { if (request === serial) byId('knowledgeReportStatus').textContent = error.message; }
        finally { if (request === serial) button.disabled = false; }
    }
    function priorityDialog(memory, selected) {
        if (document.querySelector('.knowledge-dialog[open]')) return;
        const previous = document.activeElement, box = el('dialog', null, 'backup-dialog knowledge-dialog');
        const heading = el('h2', `Retrieval priority for ${scopeName(selected)}`); heading.id = 'memoryPriorityTitle';
        box.setAttribute('aria-labelledby', heading.id);
        const form = el('form'), label = el('label', 'Importance (1–5)'), select = el('select');
        for (let n = 1; n <= 5; n++) { const option = el('option', String(n)); option.value = String(n); select.append(option); }
        select.value = String(memory.priority.salience); label.append(select);
        const pinLabel = el('label', 'Keep age priority pinned', 'knowledge-check'), pin = el('input'); pin.type = 'checkbox'; pin.checked = memory.priority.pinned;
        pinLabel.prepend(pin);
        const feedback = el('p'); feedback.setAttribute('role', 'status');
        const cancel = button('Cancel', () => { if (!busy) box.close(); }), save = el('button', 'Save priority'); save.type = 'submit';
        let busy = false;
        const actions = el('div', null, 'knowledge-actions'); actions.append(cancel, save);
        form.append(label, pinLabel, feedback, actions);
        box.append(heading, el('p', 'Priority changes retrieval order. A pin prevents age decay; it does not guarantee prompt selection or confirm a fact. Your saved content and confidence stay unchanged.'), form);
        box.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
        const draft = {box}; dialogs.add(draft);
        box.addEventListener('close', () => { dialogs.delete(draft); box.remove(); if (owner.active) { if (previous?.isConnected) previous.focus(); else byId('knowledgeScope')?.focus(); } });
        form.addEventListener('submit', tracked(async event => {
            event.preventDefault(); if (busy) return;
            busy = true; save.disabled = cancel.disabled = true; feedback.textContent = 'Saving priority…';
            try {
                const epoch = datasetEpoch();
                if (memory._serial !== serial || selected !== scope() || !memory._epoch || epoch !== memory._epoch) throw new Error('The displayed data changed. Reload and review this memory before saving.');
                await api(path(selected) + `/memories/${encodeURIComponent(memory.id)}/priority`, {method: 'PATCH', body: JSON.stringify({
                    dataset_epoch: memory._epoch, revision: memory.priority.revision, content_sha256: memory.content_sha256,
                    pinned: pin.checked, salience: Number(select.value)})});
                box.close();
                const currentEpoch = datasetEpoch();
                if (memory._serial === serial && selected === scope() && currentEpoch === memory._epoch) {
                    const refreshed = serial + 1, loaded = await load();
                    if (loaded && refreshed === serial && selected === scope() && datasetEpoch() === memory._epoch) {
                        message('Retrieval priority saved. Confidence and content are unchanged.'); byId('knowledgeScope').focus();
                    }
                }
                else if (memory._serial === serial && selected === scope()) message('Altera’s data changed. Reload before reviewing memories.');
            } catch (error) { feedback.textContent = error.message; }
            finally { busy = false; save.disabled = cancel.disabled = false; }
        }));
        document.body.append(box); box.showModal(); select.focus();
    }

    async function afterMemoryChange(memory, selected, notice) {
        await load(); message(notice);
    }

    function renderHeadings() {
        const selected = scopes.length ? byId('knowledgeScope').value : '';
        const named = id => { const found = scopes.find(s => s.id === id)?.name; return found && found !== 'this companion' ? `${found} — ` : ''; };
        const prefix = named(selected);
        const title = byId('knowledgeReportTitle'), head = document.querySelector('.knowledge-head h1');
        if (title) title.textContent = `${prefix}what is known about you`;
        if (head) head.textContent = `${prefix}what is remembered`;
    }
    function render() {
        const box = byId('knowledgeCards'); box.replaceChildren();
        const query = byId('knowledgeSearch').value.toLowerCase();
        const found = memories.filter(m => `${m.content} ${m.metadata?.type || ''}`.toLowerCase().includes(query));
        byId('knowledgeCount').textContent = String(found.length);
        if (!found.length) box.append(el('p', query ? 'No matching memories.' : 'No memories yet. Add one above or choose Remember on a chat message.', 'knowledge-muted'));
        for (const memory of found) {
            const card = el('article', null, 'knowledge-card'), header = el('header');
            const meta = memory.metadata || {};
            header.append(el('span', `${meta.type || 'fact'} · ${memory.source_kind}${meta.extracted_by === 'phrases' ? ' · phrase fallback' : ''}`), el('span', `${Math.round(Number(meta.confidence || 0) * 100)}% confidence`));
            const actions = el('div', null, 'knowledge-actions');
            const selected = scope();
            if (memory.priority) {
                const p = memory.priority;
                card.append(el('p', `${p.pinned ? 'Pinned' : 'Unpinned'} · retrieval importance ${p.salience}/5 · recency factor ${Math.round(p.decay_factor * 100)}%. ` +
                    (p.age_days === null ? 'Age is unknown; age decay is neutral.' : 'Age changes order, never deletes this memory.') +
                    ` Included in ${p.recall_count} completed reply contexts.`, 'knowledge-muted'));
                actions.append(button('Retrieval priority', () => priorityDialog(memory, selected)));
            }
            actions.append(button('Correct', () => dialog(`Correct memory for ${scopeName(selected)}`, explanation, memory.content, 'Save correction', async content => {
                const result = await api(path(selected) + `/memories/${encodeURIComponent(memory.id)}`, {method: 'PUT', body: JSON.stringify({content})});
                await afterMemoryChange(memory, selected, result.notice);
            })), button('Forget', () => dialog(`Forget this memory for ${scopeName(selected)}?`, explanation, null, 'Forget memory', async () => {
                const result = await api(path(selected) + `/memories/${encodeURIComponent(memory.id)}`, {method: 'DELETE', body: JSON.stringify({confirm: 'forget'})});
                await afterMemoryChange(memory, selected, result.notice);
            })));
            card.append(header, el('p', memory.content, 'knowledge-fact'));
            if (memory.needs_review) card.append(el('p', 'The source is missing, changed or unrecorded. This memory is paused until you review it through Correct or Forget.', 'knowledge-review'));
            const details = el('details'); details.append(el('summary', `Sources (${memory.sources.length})`));
            if (!memory.sources.length) details.append(el('p', memory.source_kind.startsWith('legacy') ? 'This older memory has no recorded message source.' : 'Saved directly by you.'));
            for (const source of memory.sources) details.append(el('div', `Conversation ${source.session_id || '?'} · message ${source.message_id || '?'} · ${source.status}\n${source.content || 'Source no longer available.'}`, 'knowledge-source'));
            card.append(actions, details); box.append(card);
        }
    }
    async function initWork() {
        page = byId('knowledgePage'); if (!page) return;
        try {
            const data = await api('/api/knowledge/scopes'); scopes = data.scopes;
            for (const item of scopes) { const option = el('option', item.name); option.value = item.id; byId('knowledgeScope').append(option); }
            const requested = new URL(location.href).searchParams.get('scope');
            byId('knowledgeScope').value = scopes.some(s => s.id === requested) ? requested : data.active;
            byId('knowledgeScope').addEventListener('change', load);
            byId('knowledgeContent').addEventListener('input', captureMemoryDraft);
            byId('knowledgeSearch').addEventListener('input', render);
            byId('viewKnowledgeReport').addEventListener('click', showReport);
            await load();
            renderHeadings();
        } catch (error) { message(error.message + ' Reload this page to retry.'); return; }
        byId('rememberForm').addEventListener('submit', tracked(async event => {
            event.preventDefault(); if (loading || remembering) return;
            remembering = true;
            const submit = event.submitter; if (submit) submit.disabled = true;
            try {
                captureMemoryDraft();
                if (newMemoryOrigin && newMemoryOrigin.scope !== scope()) throw new Error('This draft was started for ' + newMemoryOrigin.name + '. Return to that companion or clear the draft before saving for another companion.');
                const selected = scope(), name = scopeName(selected), content = byId('knowledgeContent').value, origin = newMemoryOrigin;
                const payload = {content, type: byId('knowledgeType').value};
                const result = await api(path(selected) + '/memories', {method: 'POST', body: JSON.stringify(payload)});
                if (result.status !== 'already_processed' && newMemoryOrigin === origin && byId('knowledgeContent').value === content && scope() === selected) {
                    byId('knowledgeContent').value = ''; newMemoryOrigin = null;
                }
                const loaded = await load();
                if (loaded && scope() === selected)
                    message(result.status === 'already_processed' ? 'This save was already processed. Review saved memories before starting another.' : 'Memory saved for ' + name + '.');
            } catch (error) {
                message(error.message);
            }
            finally { remembering = false; if (submit) submit.disabled = false; }
        }));
        byId('repairKnowledge').addEventListener('click', () => {
            const selected = scope();
            dialog('Rebuild automatic memories?', 'This asks the chat model to read the current eligible chat again and write the automatic memories afresh, in batches. If the model cannot be reached, the phrase matcher stands in and those memories are marked as such. Old notes and note history are cleared. Your added or corrected memories stay, and changed sources remain marked for review.', null, 'Rebuild', async () => {
                const result = await api(path(selected) + '/repair', {method: 'POST', body: JSON.stringify({confirm: 'rebuild'})}); await load(); message(result.notice);
            });
        });
        byId('forgetCompanion').addEventListener('click', () => {
            const selected = scope();
            dialog(`Forget all memories for ${scopeName(selected)}?`, explanation + ' All saved memories for this companion will be removed.', null, 'Forget all', async () => {
                await api(path(selected) + '/memories', {method: 'DELETE', body: JSON.stringify({confirm: 'forget-all'})}); await load(); message('This companion’s memories were forgotten.');
            });
        });
        byId('knowledgeDuplicates').addEventListener('click', tracked(async () => {
            try {
                const selected = scope(), data = await api(path(selected) + '/duplicates');
                if (!data.groups.length) { message('No duplicates found for this companion.'); return; }
                const description = data.groups.map(g => g.memories.map(m => m.content).join(' / ')).join('\n\n');
                dialog('Merge reviewed duplicates?', description + '\n\nOnly this companion’s duplicates will be consolidated. Your corrections are protected.', null, 'Merge duplicates', async () => {
                    const result = await api(path(selected) + '/duplicates', {method: 'POST', body: JSON.stringify({confirm: 'merge'})}); await load(); message(`Merged ${result.merged} duplicate groups.`);
                });
            } catch (error) { message(error.message); }
        }));
        page.querySelectorAll('[data-export]').forEach(b => b.addEventListener('click', tracked(async () => {
            b.disabled = true;
            try {
                const response = await fetch('/api/export', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({format: b.dataset.export})});
                if (!response.ok) throw new Error((await response.json()).error || 'Export failed');
                const blob = await response.blob(); owner.assert();
                const url = URL.createObjectURL(blob), link = el('a'), download = {url, timer: null}; downloads.add(download);
                link.href = url; link.download = (response.headers.get('Content-Disposition') || '').split('filename=')[1]?.replaceAll('"', '') || `altera-export.${b.dataset.export === 'markdown' ? 'md' : b.dataset.export}`;
                link.click(); download.timer = setTimeout(() => { URL.revokeObjectURL(url); downloads.delete(download); }, 1000); message('Export downloaded.');
            } catch (error) { message(error.message); } finally { b.disabled = false; }
        })));
        page.querySelectorAll('[data-report-export]').forEach(b => b.addEventListener('click', tracked(async () => {
            if (loading) return;
            const selected = scope(); b.disabled = true;
            try {
                const format = b.dataset.reportExport;
                const response = await fetch(path(selected) + `/report/export?format=${encodeURIComponent(format)}`, {cache: 'no-store'});
                if (!response.ok) throw new Error((await response.json()).error || 'Report export failed');
                const blob = await response.blob(); owner.assert();
                const url = URL.createObjectURL(blob), link = el('a'), download = {url, timer: null}; downloads.add(download);
                link.href = url; link.download = format === 'markdown' ? 'altera-companion-knowledge.md' : 'altera-companion-knowledge.json';
                link.click(); download.timer = setTimeout(() => { URL.revokeObjectURL(url); downloads.delete(download); }, 1000);
                byId('knowledgeReportStatus').textContent = 'Report downloaded for ' + scopeName(selected) + '.';
            } catch (error) { byId('knowledgeReportStatus').textContent = error.message; }
            finally { b.disabled = false; }
        })));
    }
    const owner = AlteraPage.component('knowledge', {
        stop() {
            serial++; for (const controller of controllers) controller.abort();
            for (const download of downloads) { clearTimeout(download.timer); URL.revokeObjectURL(download.url); } downloads.clear();
            return true;
        },
        clear() {
            scopes = []; memories = []; loading = false; page = null; remembering = false;
            newMemoryOrigin = null;
            for (const draft of [...dialogs]) {
                for (const input of draft.box.querySelectorAll('input, select, textarea')) { input.value = ''; input.checked = false; }
                draft.box.close();
            } dialogs.clear();
            if (byId('knowledgeContent')) byId('knowledgeContent').value = '';
            for (const name of ['knowledgeCards', 'knowledgeReportContent', 'knowledgeReportStatus', 'knowledgeStatus', 'knowledgeNotes']) {
                const node = byId(name); if (node) { node.replaceChildren(); node.textContent = ''; }
            }
            return true;
        }
    });
    const init = tracked(initWork), load = tracked(loadWork), showReport = tracked(showReportWork), rememberMessage = tracked(rememberMessageWork);
    AlteraPage.ready(init);
    return {rememberMessage, reviewNotice};
})();
