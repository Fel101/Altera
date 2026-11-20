// Browser notifications for check-ins (docs/PRESENCE.md, "Notifications"). Off until the person switches them on, for
// this browser only, and only while Altera is open in a tab or as the installed app. A notification says who has a
// check-in, not what it says, unless the person chose to see the text. Nothing in discreet mode; the server never lists
// a kids pal's check-ins and refuses a browser in the kids view.
(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.AlteraPage.ready(() => {
        const controller = api.create({
            document: root.document, storage: root.AlteraStorage.local, request: root.fetch.bind(root),
            Notification: root.Notification, serviceWorker: root.navigator.serviceWorker,
            setTimer: root.setTimeout.bind(root), clearTimer: root.clearTimeout.bind(root),
            openPerson: async ({avatar_id: avatarId, session_id: sessionId}) => {
                root.focus();
                // The chat already showing this person: open its inbox. Anyone else: open them, then the chat.
                if (typeof currentSessionId !== 'undefined' && currentSessionId === sessionId && root.PresenceUI) return root.PresenceUI.open();
                if (avatarId) await root.fetch(`/api/companions/${encodeURIComponent(avatarId)}/open`, {method: 'POST'});
                root.location.assign('/');
            },
        });
        root.CheckinNotices = Object.freeze({state: controller.state});
        controller.start();
    });
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';
    const KEY = 'altera_checkin_notices';
    const POLL_MS = 60000;
    const TEXT_MAX = 180;
    const OFF_HINT = 'Off. For this browser only, while Altera is open in a tab or as the installed app. Never for a kids pal or in discreet mode.';

    function create({document, storage, request, Notification, serviceWorker, setTimer, clearTimer, openPerson}) {
        let prefs = {enabled: false, text: false, after: null};
        let timer = null, stopped = false, polling = false;
        const shown = new Set();
        const $ = id => document.getElementById(id);
        const supported = () => typeof Notification === 'function';
        const discreet = () => document.documentElement?.classList?.contains('discreet') === true;
        const away = () => document.hidden === true || (typeof document.hasFocus === 'function' && !document.hasFocus());

        function say(message) { const status = $('checkinNoticeStatus'); if (status) status.textContent = message; }

        function load() {
            try {
                const saved = JSON.parse(storage.getItem(KEY) || 'null');
                if (saved && typeof saved.enabled === 'boolean') {
                    prefs = {enabled: saved.enabled, text: saved.text === true,
                        after: typeof saved.after === 'number' && Number.isFinite(saved.after) ? saved.after : null};
                }
            } catch (_) { /* off when unreadable */ }
        }

        function save() {
            try { storage.setItem(KEY, JSON.stringify(prefs)); } catch (_) { /* storage blocked: kept for this page only */ }
        }

        function render() {
            const toggle = $('checkinNoticeToggle'), text = $('checkinNoticeText');
            if (toggle) toggle.checked = prefs.enabled;
            if (text) { text.checked = prefs.text; text.disabled = !prefs.enabled; }
        }

        function schedule() {
            if (timer !== null) clearTimer(timer);
            timer = null;
            if (!stopped && prefs.enabled) timer = setTimer(() => { timer = null; void poll(); }, POLL_MS);
        }

        function stop(message) {
            prefs.enabled = false;
            save(); render(); schedule();
            if (message) say(message);
        }

        async function show(notice) {
            const title = prefs.text && notice.text ? notice.name : 'Altera';
            const body = prefs.text && notice.text
                ? (notice.text.length > TEXT_MAX ? `${notice.text.slice(0, TEXT_MAX - 1)}…` : notice.text)
                : `${notice.name} has a check-in for you.`;
            const options = {body, tag: `altera-checkin-${notice.id}`, icon: '/static/icons/icon-192.png',
                data: {avatar_id: notice.avatar_id, session_id: notice.session_id}};
            // The installed app on a phone can only show one through its service worker (sw.js handles the click).
            const registration = await serviceWorker?.getRegistration?.().catch?.(() => null);
            if (registration && typeof registration.showNotification === 'function') {
                await registration.showNotification(title, options);
                return;
            }
            const note = new Notification(title, options);
            note.onclick = () => { note.close?.(); void openPerson(options.data); };
        }

        async function poll() {
            if (stopped || polling || !prefs.enabled) return;
            if (!supported() || Notification.permission !== 'granted') {
                stop('Notifications are blocked for Altera in this browser. Allow them in the browser\'s site settings, then switch this on again.');
                return;
            }
            polling = true;
            try {
                const query = prefs.after === null ? '' : `?after=${encodeURIComponent(prefs.after)}${prefs.text ? '&text=1' : ''}`;
                const response = await request(`/api/presence/notices${query}`, {cache: 'no-store'});
                if (response.status === 403 || response.status === 423) {
                    // The kids view, or a locked browser: nothing to tell, and nothing to ask again.
                    stop('Notifications are off while this browser is locked or in the kids view.');
                    return;
                }
                if (!response.ok) return;
                const data = await response.json();
                const notices = Array.isArray(data.notices) ? data.notices : [];
                for (const notice of [...notices].reverse()) {                          // oldest first
                    if (shown.has(notice.id)) continue;
                    shown.add(notice.id);
                    if (!discreet() && away()) await show(notice);                       // in front, the banner says it
                }
                if (typeof data.now === 'number') prefs.after = Math.max(prefs.after ?? 0, data.now);
                save();
            } catch (_) {
                /* Altera is not reachable right now: try again at the next poll */
            } finally {
                polling = false;
                schedule();
            }
        }

        async function switched() {
            const toggle = $('checkinNoticeToggle');
            if (!toggle?.checked) { stop(OFF_HINT); return; }
            if (!supported()) { toggle.checked = false; say('This browser cannot show notifications.'); return; }
            const permission = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
            if (permission !== 'granted') {
                toggle.checked = false;
                say('Notifications are blocked for Altera in this browser. Allow them in the browser\'s site settings, then try again.');
                return;
            }
            prefs = {...prefs, enabled: true, after: null};                              // from now on, not the backlog
            save(); render();
            say('On. A notification says who has a check-in, not what it says, unless you choose to see the text.');
            await poll();
        }

        function textSwitched() {
            prefs.text = $('checkinNoticeText')?.checked === true;
            save();
            say(prefs.text ? 'Notifications show the check-in\'s text, so anyone who can see this screen can read it.'
                : 'Notifications say only who has a check-in.');
        }

        function onMessage(event) {
            if (event?.data?.type === 'altera-checkin') void openPerson(event.data);
        }

        function start() {
            load(); render();
            if (prefs.enabled && (!supported() || Notification.permission !== 'granted')) prefs.enabled = false;
            say(prefs.enabled ? 'On. A notification says who has a check-in, not what it says, unless you choose to see the text.'
                : OFF_HINT);
            $('checkinNoticeToggle')?.addEventListener('change', () => { void switched(); });
            $('checkinNoticeText')?.addEventListener('change', textSwitched);
            serviceWorker?.addEventListener?.('message', onMessage);
            render();
            return prefs.enabled ? poll() : null;
        }

        function close() {
            stopped = true;
            if (timer !== null) clearTimer(timer);
            timer = null;
            serviceWorker?.removeEventListener?.('message', onMessage);
        }

        return Object.freeze({start, poll, switched, textSwitched, close, state: () => ({...prefs})});
    }

    return Object.freeze({create, KEY});
});
