// Settings → Replies: temperature, reply length and the optional sampler settings (docs/REPLY_SETTINGS.md).
(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.AlteraPage.ready(() => api.create({document: root.document, request: root.fetch.bind(root)}).start());
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';
    const OPTIONAL = {top_p: 'replyTopP', min_p: 'replyMinP', repetition_penalty: 'replyRepetition',
        presence_penalty: 'replyPresence', frequency_penalty: 'replyFrequency'};
    const LABELS = {temperature: 'Temperature', max_tokens: 'Reply length', top_p: 'Top P', min_p: 'Min P',
        repetition_penalty: 'Repetition penalty', presence_penalty: 'Presence penalty', frequency_penalty: 'Frequency penalty'};

    function create({document, request}) {
        const $ = id => document.getElementById(id);
        let busy = false;

        function say(message) { const status = $('replyStatus'); if (status) status.textContent = message; }

        async function call(url, init = {}) {
            const response = await request(url, {cache: 'no-store', ...init});
            let data = {};
            try { data = await response.json(); } catch (_) { /* an empty answer is an error below */ }
            if (!response.ok) throw new Error(data.error || 'Altera did not answer. Try again.');
            return data;
        }

        function render(data) {
            $('replyTemperature').value = data.temperature ?? '';
            $('replyLength').value = data.max_tokens ?? '';
            for (const [key, id] of Object.entries(OPTIONAL)) $(id).value = data.sampling?.[key] ?? '';
            const skipped = (data.not_sent || []).map(key => LABELS[key] || key);
            const note = $('replyNotSent');
            note.hidden = !skipped.length;
            note.textContent = skipped.length ? `${skipped.join(' and ')} ${skipped.length === 1 ? 'is' : 'are'} not sent to this provider: its API refuses ${skipped.length === 1 ? 'it' : 'them'}.` : '';
            // Something is set under "More": keep it open, so nothing that changes replies is hidden.
            if ($('replyMore') && Object.keys(data.sampling || {}).length) $('replyMore').open = true;
        }

        async function load() {
            try {
                render(await call('/api/llm/sampling'));
                say('');
            } catch (error) {
                say(error.message || 'Could not load the reply settings.');
            }
        }

        // {body} or {error}: a blank optional setting is null (not sent); the two that always apply must be numbers.
        function read() {
            const body = {};
            for (const [key, id] of [['temperature', 'replyTemperature'], ['max_tokens', 'replyLength'], ...Object.entries(OPTIONAL)]) {
                const text = String($(id).value ?? '').trim();
                if (!text) {
                    if (key === 'temperature' || key === 'max_tokens') return {error: `Give a ${LABELS[key].toLowerCase()}.`};
                    body[key] = null;
                    continue;
                }
                const number = Number(text);
                if (!Number.isFinite(number)) return {error: `${LABELS[key]} must be a number.`};
                body[key] = number;
            }
            return {body};
        }

        async function save(event) {
            event?.preventDefault?.();
            if (busy) return;
            const {body, error} = read();
            if (error) { say(error); return; }
            busy = true;
            $('replySave').disabled = true;
            say('Saving…');
            try {
                render(await call('/api/llm/sampling', {method: 'PUT', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)}));
                say('Saved. The next reply uses these settings.');
            } catch (failure) {
                say(failure.message || 'Could not save. Try again.');
            } finally {
                busy = false;
                $('replySave').disabled = false;
            }
        }

        function start() {
            const form = $('replySettingsForm');
            if (!form) return null;
            form.addEventListener('submit', save);
            return load();
        }

        return Object.freeze({start, load, save, read, render});
    }

    return Object.freeze({create});
});
