window.DiagnosticsUI = (() => {
    let dialog, serial = 0;
    const controllers = new Map();
    const byId = id => document.getElementById(id);
    const el = (tag, text) => { const node = document.createElement(tag); node.textContent = text; return node; };
    const number = value => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString() : 'Not available';
    const time = value => typeof value === 'number' ? `${(value / 1000).toFixed(2)} s` : 'Not measured';
    function reported(metric, unit = '') {
        if (metric?.value == null) return 'Not reported';
        const coverage = metric.reported_calls < metric.total_calls ? ` · partial (${metric.reported_calls}/${metric.total_calls} calls)` : '';
        return number(metric.value) + unit + coverage;
    }
    function row(box, label, value) { const line = el('p', ''); line.append(el('strong', label + ': '), document.createTextNode(value)); box.append(line); }
    async function refreshWork() {
        const request = ++serial, sid = typeof currentSessionId !== 'undefined' ? currentSessionId : null;
        const status = byId('diagnosticsStatus'), button = byId('diagnosticsRefresh');
        byId('diagnosticsSummary').replaceChildren(); byId('diagnosticsTurns').replaceChildren();
        status.textContent = 'Loading this conversation…'; button.disabled = true;
        const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 5000);
        controllers.set(controller, timer);
        try {
            if (!sid) throw new Error('Open a conversation to inspect its replies.');
            const response = await fetch(`/api/diagnostics?session_id=${encodeURIComponent(sid)}`, {cache: 'no-store', signal: controller.signal});
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Diagnostics could not be loaded.');
            if (!owner.active || request !== serial || sid !== currentSessionId) return;
            const summary = byId('diagnosticsSummary');
            row(summary, 'Active provider', `${data.active.name} · ${data.active.model || 'No model selected'} · ${data.active.network_provider ? 'Network endpoint' : 'This PC'}`);
            const note = data.notes;
            row(summary, 'Notebook', note.paused ? 'Paused after a knowledge change; review memories.' : !note.available ? 'No companion notebook for this conversation.' :
                `${note.busy ? 'Updating' : 'Idle'} · ${number(note.waiting)} user messages since its last update · ${number(note.failures)} failed updates`);
            if (note.available) row(summary, 'Compaction', note.compacted_through ? `Earlier chat through message ${note.compacted_through} is represented by notes.` : 'No chat compacted yet.');
            if (note.recall_after) row(summary, 'Memory boundary', `Messages through ${note.recall_after} are excluded after a correction or forget.`);
            const turns = byId('diagnosticsTurns');
            if (!data.turns.length) turns.append(el('p', 'No measured replies yet. New replies will appear here.'));
            for (const turn of data.turns) {
                const card = el('article', ''); card.className = 'diagnostic-turn';
                card.append(el('h3', `${turn.status} · ${turn.created_at} UTC`));
                row(card, 'Timing', `First text ${time(turn.first_text_ms)} · total ${time(turn.elapsed_ms)}`);
                const m = turn.metrics;
                const models = [...new Set((m.calls || []).map(c => `${c.provider} / ${c.model}`))];
                row(card, 'Model used', models.join(', ') || 'Not recorded for this reply');
                row(card, 'Reported tokens', `Input ${reported(m.prompt_tokens)} · output ${reported(m.completion_tokens)} · total ${reported(m.total_tokens)}`);
                row(card, 'Reported cost', reported(m.reported_cost_credits, ' OpenRouter credits'));
                row(card, 'Token estimates', `Input across calls ${number(m.input_tokens_estimate)} · visible output ${number(m.visible_output_tokens_estimate)}`);
                const c = m.context;
                if (c) {
                    const headroom = c.limit_tokens - c.system_tokens_estimate - c.history_tokens_estimate - c.reply_reserve_estimate;
                    row(card, 'Context estimate', `${number(c.system_tokens_estimate + c.history_tokens_estimate)} prompt tokens · limit ${number(c.limit_tokens)} (${c.limit_source})`);
                    row(card, 'Reply room', `${number(c.reply_reserve_estimate)} reserved in history selection · request maximum ${number(c.max_reply_tokens)} · estimated remaining ${number(headroom)}`);
                    row(card, 'Chat in prompt', `${c.history_messages} of ${c.history_candidates} eligible recent messages${c.history_after ? `, after message ${c.history_after}` : ''}`);
                }
                if (m.notes_action) row(card, 'Notes scheduled', m.notes_action === 'compact' ? 'Compaction requested after this reply' : 'Notebook update requested after this reply');
                turns.append(card);
            }
            status.textContent = data.scope;
        } catch (error) { if (owner.active && request === serial) status.textContent = error.message + ' Use Refresh to retry.'; }
        finally { clearTimeout(timer); controllers.delete(controller); if (owner.active && request === serial) button.disabled = false; }
    }
    function open() { if (!dialog.open) dialog.showModal(); refresh(); }
    const owner = AlteraPage.component('diagnostics', {
        stop() { serial++; for (const [controller, timer] of controllers) { clearTimeout(timer); controller.abort(); } dialog?.close(); return true; },
        clear() {
            controllers.clear();
            byId('diagnosticsSummary')?.replaceChildren(); byId('diagnosticsTurns')?.replaceChildren();
            if (byId('diagnosticsStatus')) byId('diagnosticsStatus').textContent = '';
            dialog = null; return true;
        }
    });
    const refresh = () => owner.run(refreshWork).catch(error => { if (owner.active) throw error; });
    AlteraPage.ready(() => {
        dialog = byId('diagnosticsDialog'); if (!dialog) return;
        byId('diagnosticsClose').addEventListener('click', () => dialog.close());
        byId('diagnosticsRefresh').addEventListener('click', refresh);
    });
    return {open: () => owner.run(open).catch(error => { if (owner.active) throw error; })};
})();
