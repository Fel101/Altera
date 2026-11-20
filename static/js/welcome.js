// The chat page's welcome: a first run is sent to /setup (it has its own page so that it can end on Home), then the
// greeting by name, a few conversation starters, and feature hints shown one at a time and never again once dismissed.
// app.js wires it to the page's lifecycle (MainUI) and the composer, under the names the page's buttons call:
// openSetupCheck, useStarter and dismissFeatureHint.
(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.Welcome = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';
    const HINTS = Object.freeze([
        'You can talk with your microphone: click the microphone button.',
        'What someone remembers about you is on the Memory page in the sidebar.',
        'You can search every conversation from the Search page in the sidebar.',
        'Change the accent colour or theme in Settings.',
        'Everyone you add has their own separate conversation.',
    ]);
    const HINTS_KEY = 'altera_dismissed_hints';
    const STARTERS_SHOWN = 4;
    const HINT_DELAY = 8000, STARTER_SEND_DELAY = 300;

    /**
     * json(url) -> {res, data}; later(fn, ms) schedules on the page; active() says whether the page is still open;
     * context() is the chat the page shows now; insert(text) puts text in the composer (false when it already holds
     * some); busy() is true while a reply streams; assign(url) navigates.
     */
    function create({document, storage, json, later, active, context, insert, busy, assign, warn = () => {}, now = () => new Date()}) {
        const same = origin => JSON.stringify(context()) === JSON.stringify(origin);

        function dismissed() {
            try {
                const list = JSON.parse(storage.getItem(HINTS_KEY) || '[]');
                return Array.isArray(list) ? list.filter(Number.isInteger) : [];
            } catch (_) {
                return [];
            }
        }

        async function check() {
            try {
                const {res, data} = await json('/api/onboarding/status');
                if (!res.ok) throw new Error('Setup status is unavailable');
                if (!data.complete) { assign('/setup'); return; }
                void greet();
                void starters();
                scheduleHint();
            } catch (error) {
                if (!active()) return;
                warn('Onboarding check failed:', error);
                void starters();                                       // the starters are worth showing anyway
            }
        }

        function greeting(hour) {
            if (hour < 12) return 'Good morning';
            if (hour < 17) return 'Good afternoon';
            if (hour < 22) return 'Good evening';
            return 'Hey, night owl';
        }

        async function greet() {
            const origin = context();
            try {
                const {res, data: profile} = await json('/api/profile');
                if (!res.ok || !same(origin)) return;
                const name = profile?.name || '';
                if (!name) return;                                     // the page's own greeting stays
                const title = document.getElementById('welcomeTitle'), text = document.getElementById('welcomeText');
                if (title) title.textContent = `${greeting(now().getHours())}, ${name}.`;
                if (text) text.textContent = 'Who would you like to talk to?';
                document.getElementById('welcomeMessage')?.classList.add('personalized');
            } catch (error) {
                if (active()) warn('Failed to load profile for welcome:', error);
            }
        }

        async function starters() {
            const origin = context();
            try {
                const {res, data} = await json('/api/conversation-starters');
                if (!res.ok || !same(origin) || !Array.isArray(data?.starters) || !data.starters.length) return;
                const container = document.getElementById('conversationStarters'), buttons = document.getElementById('starterButtons');
                if (!container || !buttons) return;
                buttons.replaceChildren(...data.starters.slice(0, STARTERS_SHOWN).map(text => {
                    const button = document.createElement('button');
                    button.className = 'starter-btn';
                    button.setAttribute('data-click', 'useStarter');
                    button.setAttribute('data-pass', 'element');
                    button.textContent = String(text);
                    return button;
                }));
                container.style.display = 'block';
            } catch (error) {
                if (active()) warn('Failed to load conversation starters:', error);
            }
        }

        /** A starter goes into an empty composer and is sent a moment later, unless the chat changed meanwhile. */
        function useStarter(button) {
            const text = button.textContent;
            const input = document.getElementById('messageInput'), origin = context();
            if (!insert(text)) return;
            later(() => {
                if (!busy() && input?.value === text && same(origin)) document.getElementById('messageForm')?.requestSubmit();
            }, STARTER_SEND_DELAY);
        }

        function scheduleHint() {
            later(() => {
                const hint = document.getElementById('featureHint');
                if (!hint || hint.style.display !== 'none') return;
                const gone = dismissed();
                const index = HINTS.findIndex((_, i) => !gone.includes(i));
                if (index < 0) return;
                const text = document.getElementById('hintText');
                if (text) text.textContent = HINTS[index];
                hint.dataset.index = String(index);
                hint.style.display = 'flex';
            }, HINT_DELAY);
        }

        function dismissHint() {
            const hint = document.getElementById('featureHint');
            if (!hint || hint.style.display === 'none') return;
            const index = Number.parseInt(hint.dataset.index, 10);
            if (Number.isInteger(index) && index >= 0) {
                const gone = dismissed();
                if (!gone.includes(index)) gone.push(index);
                try { storage.setItem(HINTS_KEY, JSON.stringify(gone)); } catch (_) { /* storage blocked: shown again next time */ }
            }
            hint.style.display = 'none';
        }

        const openSetupCheck = () => assign('/setup?check=1');

        return Object.freeze({check, greet, starters, useStarter, scheduleHint, dismissHint, openSetupCheck});
    }

    return Object.freeze({create, HINTS});
});
