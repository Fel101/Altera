// Conversation controls use saved message IDs. Drafts never masquerade as persisted answers.
window.ChatControls = (() => {
    let active = null;
    const requests = new Set(), timers = new Set(), editors = new Map();
    const context = () => ({session_id: currentSessionId,
        dataset_epoch: typeof AlteraAPI === 'undefined' ? null : AlteraAPI.epoch,
        companion_id: typeof Companion === 'undefined' ? null : Companion.active?.id || null,
        companion_name: typeof Companion === 'undefined' ? null : Companion.active?.name || null});
    const matches = origin => ['session_id', 'dataset_epoch', 'companion_id'].every(key => context()[key] === origin[key]);
    const tracked = action => function (...args) {
        return owner.run(() => action.apply(this, args)).catch(error => { if (owner.active) throw error; });
    };
    const dirty = editor => editor.input.value !== editor.baseline;
    function releaseEditor(editor) {
        editor.input.value = ''; editor.baseline = '';
        editor.form.remove(); editor.original.hidden = false; editor.actions.hidden = false; editors.delete(editor.form);
    }
    async function prepareNavigation() {
        if (!owner.active) return false;
        if ([...editors.values()].some(dirty) && !confirm('Discard unsaved message edits before changing the conversation view?')) return false;
        if (!owner.active) return false;
        for (const editor of [...editors.values()]) releaseEditor(editor);
        return true;
    }
    const owner = AlteraPage.component('message-controls', {
        stop() {
            for (const controller of requests) controller.abort();
            for (const timer of timers) clearTimeout(timer); timers.clear();
            if (active) { settleStop(active, false); active.stopping = false; }
            return true;
        },
        clear() {
            active = null;
            for (const editor of [...editors.values()]) releaseEditor(editor);
            document.querySelectorAll('.message-actions').forEach(node => node.remove());
            return true;
        }
    });
    window.addEventListener('beforeunload', event => { if ([...editors.values()].some(dirty)) { event.preventDefault(); event.returnValue = ''; } });

    function sync() {
        if (!owner.active) return;
        const stop = document.getElementById('stopReplyBtn');
        const send = document.getElementById('sendBtn');
        if (stop) {
            stop.hidden = !isStreaming || !active;
            stop.disabled = Boolean(active?.stopping || (active?.terminal && active.terminal !== 'saved'));
            stop.textContent = active?.stopping ? 'Stopping…' : 'Stop';
        }
        if (send) send.hidden = Boolean(isStreaming && active);
        document.querySelectorAll('[data-chat-action]').forEach(button => {
            button.disabled = isStreaming && !['copy', 'older', 'newer'].includes(button.dataset.chatAction);
        });
        // The version switcher keeps its own disabled state (which end of the list it is at); while a reply streams, both
        // ends are stopped, and afterwards each box re-applies its own ends.
        document.querySelectorAll('.message-versions').forEach(box => {
            const count = box.querySelector('.message-versions-count');
            const index = count ? Number.parseInt(count.textContent, 10) - 1 : 0;
            const buttons = box.querySelectorAll('button');
            if (buttons.length < 2) return;
            const length = Number.parseInt((count ? count.textContent.split(' of ')[1] : '') || '0', 10) || 1;
            buttons[0].disabled = isStreaming || index <= 0;
            buttons[1].disabled = isStreaming || index >= length - 1;
        });
    }

    async function request(url, options = {}) {
        return owner.run(async () => {
            const controller = new AbortController(); requests.add(controller);
            try {
                const response = await fetch(url, { ...options, signal: controller.signal, headers: { 'Content-Type': 'application/json', ...options.headers } });
                const data = await response.json();
                owner.assert();
                if (!response.ok) throw new Error(data?.error || `HTTP ${response.status}`);
                return data;
            } finally { requests.delete(controller); }
        });
    }

    function settleStop(turn, result) {
        turn.resolveStop?.(result);
        turn.resolveStop = null;
    }
    function sendStop(turn) {
        void tracked(dispatchStop)(turn).catch(error => { if (owner.active) showReplyNotice(error.message); });
    }

    async function dispatchStop(turn) {
        if (turn.stopSent || (turn.terminal && turn.terminal !== 'saved')) return;
        turn.stopSent = true;
        const attempt = turn.stopPromise;
        try {
            await request(`/api/chat/requests/${encodeURIComponent(turn.id)}/stop`, {method: 'POST'});
            if (turn.stopPromise !== attempt) return;
            settleStop(turn, true);
        } catch (err) {
            if (!owner.active) return;
            if (turn.stopPromise !== attempt) return;
            // A terminal event can win the race with this HTTP response.
            if (turn.terminal && (turn.terminal !== 'saved' || turn.settled)) { settleStop(turn, true); return; }
            turn.stopping = false;
            turn.stopSent = false;
            settleStop(turn, false);
            sync();
            showReplyNotice(err.message);
        }
    }

    function stop(requestId = active?.id) {
        if (!owner.active) return Promise.resolve(false);
        if (typeof suppressSpeech === 'function') suppressSpeech(requestId);
        else stopSpeech();
        if (!requestId) return Promise.resolve(true);
        // A disconnected durable turn can still be stopped by its original key.
        if (!active || active.id !== requestId) begin(requestId, true);
        const turn = active;
        if (turn.terminal && (turn.terminal !== 'saved' || turn.settled)) return Promise.resolve(true);
        if (turn.stopping) return turn.stopPromise;
        turn.stopping = true;
        turn.stopPromise = owner.run(() => new Promise(resolve => { turn.resolveStop = resolve; }))
            .catch(error => { if (!owner.active) return false; throw error; });
        sync();
        // An early click is remembered until accepted; stopping an unknown key would race a 404.
        if (turn.accepted) sendStop(turn);
        return turn.stopPromise;
    }

    function begin(id, alreadyAccepted = false) {
        if (!owner.active) return;
        if (!active || active.id !== id) {
            if (active) settleStop(active, false);
            active = {id, accepted: alreadyAccepted, stopping: false, terminal: null};
        }
        active.settled = false;
        sync();
    }

    function terminal(id, status) {
        if (!owner.active) return;
        if (active?.id !== id) return;
        active.terminal = status;
        active.accepted = true;
        // Saved text can precede the end of the audio stream. Stop still cancels that drain.
        if (status === 'saved' && active.stopping && !active.settled) {
            active.accepted = true;
            sendStop(active);
        } else settleStop(active, true);
        sync();
    }

    function settled(id, status) {
        if (!owner.active) return;
        if (active?.id !== id) return;
        active.settled = true;
        if (status) terminal(id, status);
        else {
            settleStop(active, false);
            active.stopping = false;
            active.stopSent = false;
            active.stopPromise = null;
            sync();
        }
    }

    async function regenerate(messageId, origin) {
        if (!owner.active || !matches(origin)) return;
        if (isStreaming) return;
        if (pendingForSession()) { showReplyNotice('Reconnect to retrieve the unfinished reply first.'); return; }
        document.getElementById('replyNotice')?.remove();
        stopSpeech();
        isStreaming = true;
        updateSendButton(true);
        showTypingIndicator();
        try {
            await runChatStream({operation: 'regenerate', session_id: origin.session_id, message_id: messageId});
        } catch (err) {
            if (owner.active && matches(origin)) showReplyNotice(err.message);
        } finally {
            if (owner.active && matches(origin)) { hideTypingIndicator(); isStreaming = false; updateSendButton(false); }
        }
    }

    async function edit(element, record, origin) {
        if (!matches(origin) || isStreaming || element.querySelector('.message-editor')) return;
        if (editors.size >= 16) { showReplyNotice('Finish or cancel an open message edit before opening another.'); return; }
        const form = document.createElement('form');
        form.className = 'message-editor';
        const input = document.createElement('textarea');
        input.value = record.content;
        input.setAttribute('aria-label', 'Edit message');
        input.rows = Math.min(12, Math.max(3, record.content.split('\n').length));
        const save = document.createElement('button');
        save.type = 'submit';
        save.textContent = 'Save';
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.textContent = 'Cancel';
        const original = element.querySelector('.message-content');
        const actions = element.querySelector('.message-actions');
        const editor = {form, input, original, actions, context: origin, messageId: record.id, role: record.role,
            baseline: record.content};
        editors.set(form, editor);
        original.hidden = true;
        actions.hidden = true;
        const close = () => { releaseEditor(editor); actions.querySelector('button')?.focus(); };
        cancel.addEventListener('click', tracked(close));
        input.addEventListener('keydown', tracked(event => {
            if (event.key === 'Escape') { event.preventDefault(); close(); }
            if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); form.requestSubmit(); }
        }));
        form.append(input, save, cancel);
        original.after(form);
        input.focus();
        form.addEventListener('submit', tracked(async event => {
            event.preventDefault();
            if (save.disabled || !editors.has(form) || !form.isConnected) return;
            if (!matches(origin)) { showReplyNotice('This edit belongs to an earlier conversation. Review its saved draft before editing that message again.'); return; }
            const submitted = input.value;
            save.disabled = true;
            try {
                const result = await request(`/api/messages/${record.id}`, {method: 'PATCH', body: JSON.stringify({session_id: origin.session_id,
                    dataset_epoch: origin.dataset_epoch, avatar_id: origin.companion_id || '', expected_content: editor.baseline, content: submitted})});
                if (!owner.active || !matches(origin) || !editors.has(form)) return;
                const saved = typeof result.content === 'string' ? result.content : submitted;
                const changedWhileSaving = input.value !== submitted;
                editor.baseline = saved;
                record.content = saved; original.innerHTML = formatMessage(saved);
                if (changedWhileSaving) { showReplyNotice('Message updated. Newer edits are still unsaved.'); return; }
                input.value = saved;
                await loadMessages(origin.session_id);
                if (!owner.active || !matches(origin)) return;
                if (typeof KnowledgeUI !== 'undefined') KnowledgeUI.reviewNotice('Message updated. Review and rebuild derived memories.');
                else showReplyNotice('Message updated.');
            } catch (err) { if (owner.active && matches(origin)) showReplyNotice(err.message); }
            finally { if (owner.active) save.disabled = false; }
        }));
    }

    function addTools(element, record) {
        if (!owner.active || !element) return;
        const origin = context();
        element.querySelector('.message-actions')?.remove();
        element.querySelector('.message-versions')?.remove();
        if (record.id) element.dataset.messageId = record.id;
        if (record.request_id) element.dataset.requestId = record.request_id;
        if (record.retry_message_id) element.dataset.retryMessageId = record.retry_message_id;
        const tools = document.createElement('div');
        tools.className = 'message-actions';
        tools.setAttribute('role', 'group');
        tools.setAttribute('aria-label', record.role === 'user' ? 'Your message actions' : 'Reply actions');
        function button(label, action, callback) {
            const b = document.createElement('button');
            b.type = 'button'; b.textContent = label; b.dataset.chatAction = action;
            b.addEventListener('click', tracked(async () => {
                if (!matches(origin) || !element.isConnected) return;
                try { await callback(b); } catch (err) { if (owner.active && matches(origin)) showReplyNotice(err.message); }
            }));
            tools.append(b);
        }
        button('Copy', 'copy', async b => {
            const content = record.id ? record.content : element.querySelector('.message-content').textContent;
            await navigator.clipboard.writeText(content.replace(/\n?\[\[photo:p-[0-9a-f]{10}\]\]/g, ''));
            owner.assert();
            b.textContent = 'Copied';
            const timer = setTimeout(() => { timers.delete(timer); if (owner.active && b.isConnected) b.textContent = 'Copy'; }, 1500); timers.add(timer);
        });
        if (record.id) {
            if (record.role === 'user' && typeof KnowledgeUI !== 'undefined') button('Remember', 'remember', () => KnowledgeUI.rememberMessage(record.id));
            if (record.role === 'assistant') button('Regenerate', 'regenerate', () => regenerate(record.id, origin));
            button('Edit', 'edit', () => edit(element, record, origin));
            button('Delete', 'delete', async () => {
                if (isStreaming || !confirm('Delete this message and any linked voice-note recordings and reply audio? Later messages will stay in the conversation.')) return;
                await request(`/api/messages/${record.id}`, {method: 'DELETE', body: JSON.stringify({session_id: origin.session_id,
                    dataset_epoch: origin.dataset_epoch, avatar_id: origin.companion_id || '', expected_content: record.content, confirm: 'delete-message'})});
                if (!owner.active || !matches(origin)) return;
                await loadMessages(origin.session_id);
                if (!owner.active || !matches(origin)) return;
                if (typeof KnowledgeUI !== 'undefined') KnowledgeUI.reviewNotice('Message deleted. Review and rebuild derived memories.');
                else showReplyNotice('Message deleted.');
            });
            if (record.role === 'assistant' && record.other_versions > 0) versions(element, record, origin);
        } else if (record.retry_message_id && ['failed', 'interrupted'].includes(record.reply_state)) {
            button('Try again', 'regenerate', () => regenerate(record.retry_message_id, origin));
        }
        element.querySelector('.message-time').after(tools);
        sync();
    }

    // Alternative replies: the earlier answers kept for this one, so the person can move between them. The list is
    // fetched once per message; switching asks the server to show a version, which keeps the message's id (and with it the
    // memories, voice notes and reply history that point at it).
    function versions(element, record, origin) {
        const box = document.createElement('div');
        box.className = 'message-versions';
        box.dataset.chatAction = 'versions';
        const label = document.createElement('span');
        label.className = 'message-versions-label';
        const previous = document.createElement('button');
        previous.type = 'button'; previous.dataset.chatAction = 'older'; previous.textContent = '◀ Earlier';
        const next = document.createElement('button');
        next.type = 'button'; next.dataset.chatAction = 'newer'; next.textContent = 'Later ▶';
        const position = document.createElement('span');
        position.className = 'message-versions-count';
        box.append(previous, position, next);
        element.querySelector('.message-time').after(box);
        let all = null;
        let busy = false;
        function current() { return all ? all.findIndex(version => version.current) : -1; }
        function draw() {
            if (!all) return;
            const index = current();
            position.textContent = `${index + 1} of ${all.length}`;
            previous.disabled = busy || index <= 0;
            next.disabled = busy || index >= all.length - 1;
        }
        async function load() {
            const data = await request(`/api/messages/${record.id}/versions`, {headers: {'Accept': 'application/json'}});
            if (!owner.active || !matches(origin) || !element.isConnected) return;
            if (!Array.isArray(data.versions) || data.versions.length < 2) { box.remove(); return; }
            all = data.versions;
            label.textContent = `${all.length} answers`;
            draw();
        }
        async function show(version) {
            if (busy || !all) return;
            busy = true; draw();
            try {
                const result = await request(`/api/messages/${record.id}/versions/${version.version_id}`, {
                    method: 'POST', body: JSON.stringify({session_id: origin.session_id,
                        dataset_epoch: origin.dataset_epoch, avatar_id: origin.companion_id || '',
                        expected_content: record.content})});
                if (!owner.active || !matches(origin)) return;
                record.content = typeof result.content === 'string' ? result.content : version.content;
                const content = element.querySelector('.message-content');
                if (content) content.innerHTML = formatMessage(record.content);
                all = (await request(`/api/messages/${record.id}/versions`, {headers: {'Accept': 'application/json'}})).versions;
                if (!owner.active || !matches(origin)) return;
                draw();
                if (typeof KnowledgeUI !== 'undefined') KnowledgeUI.reviewNotice('Answer changed. Review and rebuild derived memories.');
            } catch (err) {
                if (owner.active && matches(origin)) showReplyNotice(err.message);
            } finally {
                busy = false; draw();
            }
        }
        previous.addEventListener('click', tracked(() => { const index = current(); if (all && index > 0) return show(all[index - 1]); }));
        next.addEventListener('click', tracked(() => { const index = current(); if (all && index >= 0 && index < all.length - 1) return show(all[index + 1]); }));
        // Older answers arrive after the page is drawn, so the control appears once they are known.
        tracked(load)().catch(() => box.remove());
    }

    function hydrate(container, records) {
        if (!owner.active) return;
        const elements = container.querySelectorAll('.message');
        records.forEach((record, i) => addTools(elements[i], record));
    }

    function finish(element, event, text, payload) {
        const destination = payload.resolved_session_id ?? payload.session_id ?? event.session_id;
        if (!owner.active || destination !== currentSessionId || (event.session_id != null && event.session_id !== currentSessionId) ||
            (payload.dataset_epoch && payload.dataset_epoch !== context().dataset_epoch)) return;
        if (element && payload.operation === 'regenerate') {
            const old = document.querySelector(`.message[data-message-id="${event.message_id}"]`);
            if (old && old !== element) old.replaceWith(element);
            // Remove earlier stopped/failed drafts for this question once a replacement is safely stored.
            document.querySelectorAll('.message[data-request-id]').forEach(draft => {
                if (draft !== element && draft.dataset.retryMessageId === String(event.user_message_id)) draft.remove();
            });
            if (typeof KnowledgeUI !== 'undefined') KnowledgeUI.reviewNotice('Reply replaced. Review and rebuild derived memories.');
        }
        addTools(element, {id: event.message_id, role: 'assistant', content: text,
            other_versions: Number.isFinite(event.other_versions) ? event.other_versions : 0});
        if (event.user_message_id) {
            const user = [...document.querySelectorAll('.message.user')].reverse().find(el => !el.dataset.messageId);
            if (user) addTools(user, {id: event.user_message_id, role: 'user', content: user.querySelector('.message-content').textContent});
        }
    }

    return {
        sync, stop, hydrate, addTools, finish, prepareNavigation: tracked(prepareNavigation),
        begin, terminal, settled,
        accepted: id => {
            if (!owner.active) return;
            if (!active || (id && active.id !== id)) return;
            active.accepted = true;
            if (active.stopping) sendStop(active);
            sync();
        },
    };
})();
