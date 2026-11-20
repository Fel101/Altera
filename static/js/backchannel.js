// Short, explicitly prepared filler audio; never part of a saved conversation.
(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else {
        let owner;
        const controller = api.create({document: root.document, storage: root.AlteraStorage.local,
            request: root.fetch.bind(root), makeAudio: url => new Audio(url),
            setTimer: root.setTimeout.bind(root), clearTimer: root.clearTimeout.bind(root),
            now: () => Date.now(), flush: () => root.AlteraPage.flush(),
            active: () => owner.active,
            eligible: turn => typeof activeChatTurn !== 'undefined' && activeChatTurn === turn && isStreaming && voiceEnabled
                && !isRecording && !recordingStarting && !voiceTranscribing && !speechAudio
                && !(typeof VoiceNotes !== 'undefined' && VoiceNotes.state().opened),
            canPrepare: () => !isStreaming && !isRecording && !recordingStarting && !voiceTranscribing && !speechAudio
                && !(typeof VoiceNotes !== 'undefined' && VoiceNotes.state().opened)});
        owner = root.AlteraPage.component('backchannel', {stop: controller.close, clear: () => true});
        root.AlteraBackchannel = Object.freeze({schedule: controller.schedule, stop: controller.stop});
        root.AlteraPage.ready(() => controller.start(action => owner.run(action).catch(() => {})));
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';
    const KEY = 'altera_backchannel';
    const URL_PATTERN = /^\/static\/audio\/tts\/[a-zA-Z0-9_-]{1,160}\.(?:wav|mp3)$/;
    const defaultHint = 'Off by default. Preparing sends “Mm-hmm” to your configured speech service, which may be remote or Microsoft. Clips use the ordinary speech cache and may need preparing again after it expires.';
    function create({document, storage, request, makeAudio, setTimer, clearTimer, now, flush, active, eligible, canPrepare}) {
        let closed = false, audio = null, timer = null, epoch = 0, lastPlayed = -Infinity;
        let enabled = false, url = '', toggle, button, previewButton, status, controller = null, track = fn => fn();
        const live = () => !closed && active();
        function hint(message) { if (live() && status) status.textContent = message; }
        function save() { storage.setItem(KEY, JSON.stringify({enabled, url})); return flush(); }
        function stop() {
            epoch++;
            if (timer !== null) { clearTimer(timer); timer = null; }
            if (audio) {
                const previous = audio; audio = null; previous.onended = previous.onerror = null;
                previous.pause(); previous.removeAttribute?.('src'); previous.load?.();
            }
        }
        function playClip() {
            try {
                const clip = makeAudio(url); audio = clip; clip.volume = .4;
                clip.onended = () => { if (audio === clip) audio = null; };
                clip.onerror = () => { if (audio === clip) { stop(); hint('The prepared clip is unavailable. Prepare it again when ready.'); } };
                const result = clip.play();
                if (result?.catch) result.catch(() => { if (audio === clip) { stop(); hint('Playback was blocked or the clip expired. Prepare it again when ready.'); } });
            } catch (_) { stop(); hint('The prepared clip could not be played.'); }
        }
        function schedule(turn) {
            stop();
            if (!live() || !enabled || !url || document.hidden || now() - lastPlayed < 60000) return;
            const mark = epoch;
            timer = setTimer(() => {
                timer = null;
                if (!live() || mark !== epoch || !enabled || !url || document.hidden || !eligible(turn)) return;
                playClip(); lastPlayed = now();
            }, 3000);
        }
        async function prepare() {
            if (!live() || controller) return;
            if (!canPrepare()) { hint('Finish the current reply or recording before preparing a clip.'); return; }
            stop(); controller = new AbortController(); const current = controller;
            if (button) button.disabled = true;
            hint('Preparing the clip with the configured speech service…');
            try {
                const response = await request('/api/voice/tts', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({text: 'Mm-hmm.'}), signal: current.signal});
                if (!response.ok) throw Error();
                const result = await response.json();
                if (!live() || controller !== current || !URL_PATTERN.test(result.audio_url || '')) throw Error();
                url = result.audio_url; await save();
                if (live()) {
                    if (previewButton) previewButton.disabled = false;
                    hint('Clip prepared. Preview it, then turn on quiet backchannels to use it; prepare it again if the speech cache expires.');
                }
            } catch (_) { hint('The clip could not be prepared or saved. Check the configured voice service and retry.'); }
            finally { if (controller === current) controller = null; if (live() && button) button.disabled = false; }
        }
        async function changed() {
            if (!live()) return;
            enabled = Boolean(toggle.checked); stop();
            try { await save(); hint(enabled && !url ? 'Prepare a voice clip before backchannels can play.' : defaultHint); }
            catch (_) { hint('The preference could not be saved. Keep this page open and retry.'); }
        }
        function visibility() { if (document.hidden) stop(); }
        function preview() {
            if (!live() || !url) return;
            if (!canPrepare()) { hint('Finish the current reply or recording before previewing the clip.'); return; }
            stop(); playClip();
        }
        function start(run) {
            if (!live()) return;
            track = run;
            toggle = document.getElementById('backchannelToggle'); button = document.getElementById('backchannelPrepare');
            previewButton = document.getElementById('backchannelPreview');
            status = document.getElementById('backchannelStatus');
            try {
                const data = JSON.parse(storage.getItem(KEY) || 'null');
                if (data && typeof data.enabled === 'boolean' && typeof data.url === 'string'
                        && (!data.url || URL_PATTERN.test(data.url))) { enabled = data.enabled; url = data.url; }
            } catch (_) { /* Default off if unavailable or malformed. */ }
            if (toggle) { toggle.checked = enabled; toggle.addEventListener('change', onChange); }
            if (button) button.addEventListener('click', onPrepare);
            if (previewButton) { previewButton.disabled = !url; previewButton.addEventListener('click', onPreview); }
            document.addEventListener('visibilitychange', visibility);
        }
        function onChange() { void track(changed); }
        function onPrepare() { void track(prepare); }
        function onPreview() { preview(); }
        function close() {
            closed = true; stop(); controller?.abort(); controller = null;
            toggle?.removeEventListener('change', onChange); button?.removeEventListener('click', onPrepare);
            previewButton?.removeEventListener('click', onPreview);
            document.removeEventListener('visibilitychange', visibility);
            if (toggle) toggle.checked = false;
            enabled = false; url = ''; toggle = button = previewButton = status = null;
            return true;
        }
        return Object.freeze({start, schedule, stop, close});
    }
    return Object.freeze({create});
});
