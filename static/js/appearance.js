// How the chat page looks: Settings' theme and accent colour, the colours of adult mode's level, and discreet mode.
// The page's first script (templates/index.html) applies the saved theme, level and discreet mode before anything is
// drawn; this keeps them in step afterwards. app.js wires it to the page, under the names the buttons and companion.js
// call: setTheme, setAccentColor, setCompanionMood and toggleDiscreet.
(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.Appearance = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';
    // The first is the Evening default; adult mode's levels bring their own colours (style.css).
    const ACCENTS = Object.freeze([
        {name: 'Lavender', value: '#b59cff'}, {name: 'Blue', value: '#6ea8ff'}, {name: 'Green', value: '#34d399'},
        {name: 'Pink', value: '#f28bab'}, {name: 'Amber', value: '#f7b267'}, {name: 'Teal', value: '#2dd4bf'},
    ]);
    const COLOUR = /^#[0-9a-f]{6}$/i;
    const DISCREET_ON = 'Discreet mode is on: click to show everything again';
    const DISCREET_OFF = 'Discreet mode: blur the portrait and the chat (Esc)';

    function create({document, storage}) {
        let mood = 'off', discreet = false;
        const html = () => document.documentElement;
        const read = key => { try { return storage.getItem(key); } catch (_) { return null; } };
        const save = (key, value) => { try { storage.setItem(key, value); } catch (_) { /* storage blocked: this page only */ } };

        function press(selector, chosen) {
            for (const node of document.querySelectorAll(selector)) {
                const on = node === chosen;
                node.classList.toggle('active', on);
                node.setAttribute('aria-pressed', on ? 'true' : 'false');
            }
        }

        function setTheme(theme) {
            if (theme === 'dark') html().removeAttribute('data-theme');
            else html().setAttribute('data-theme', theme);
            save('altera_theme', theme);
            press('.theme-option', document.querySelector(`.theme-option.${theme}-mode`));
        }

        // The picked accent is --user-accent*: the Evening colours use it, adult mode's levels bring their own.
        function applyAccent(color) {
            if (!COLOUR.test(color || '')) return false;
            const [r, g, b] = [1, 3, 5].map(i => parseInt(color.slice(i, i + 2), 16));
            const style = html().style;
            style.setProperty('--user-accent', color);
            style.setProperty('--user-accent-hover', `rgb(${Math.min(r + 28, 255)}, ${Math.min(g + 28, 255)}, ${Math.min(b + 28, 255)})`);
            style.setProperty('--user-accent-glow', `rgba(${r}, ${g}, ${b}, 0.28)`);
            return true;
        }

        function setAccent(color, button) {
            if (!applyAccent(color)) return;
            save('altera_accent', color);
            press('.accent-option', button || null);
        }

        function renderAccents(container, shown) {
            container.replaceChildren(...ACCENTS.map(accent => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'accent-option';
                button.style.background = accent.value;
                button.setAttribute('data-click', 'setAccentColor');
                button.setAttribute('data-args', JSON.stringify([accent.value]));
                button.setAttribute('data-pass', 'element');
                button.setAttribute('aria-label', `${accent.name} accent`);
                button.setAttribute('title', accent.name);
                const on = accent.value === shown;
                button.classList.toggle('active', on);
                button.setAttribute('aria-pressed', on ? 'true' : 'false');
                return button;
            }));
        }

        function apply() {
            html().dataset.mood = discreet ? 'off' : mood;
            html().classList.toggle('discreet', discreet);
            const button = document.getElementById('discreetBtn');
            if (button) {
                button.setAttribute('aria-pressed', discreet ? 'true' : 'false');
                button.title = discreet ? DISCREET_ON : DISCREET_OFF;
            }
        }

        /** companion.js calls this whenever adult mode is loaded or changed. `label` is the level's name for the badge. */
        function setMood(level, label) {
            mood = level || 'off';
            save('altera_mood', mood);
            const badge = document.getElementById('moodBadge');
            if (badge) {
                badge.hidden = mood === 'off';
                badge.textContent = label || mood;
            }
            apply();
        }

        function toggleDiscreet(force) {
            discreet = typeof force === 'boolean' ? force : !discreet;
            save('altera_discreet', discreet ? '1' : '0');
            apply();
        }

        /** Once the page is ready: the saved theme, accent and discreet mode, and the accent buttons. */
        function restore() {
            const theme = read('altera_theme') || 'dark';
            html().setAttribute('data-theme', theme);
            press('.theme-option', document.querySelector(`.theme-option.${theme}-mode`));
            const accent = read('altera_accent');
            const applied = accent && applyAccent(accent);
            const container = document.getElementById('accentColors');
            if (container) renderAccents(container, applied ? accent : ACCENTS[0].value);
            discreet = read('altera_discreet') === '1';
            apply();
        }

        /** The page closed: nothing about the last person's level stays on it. */
        function reset() {
            mood = 'off';
            discreet = false;
            html().dataset.mood = 'off';
            html().classList.remove('discreet');
        }

        return Object.freeze({restore, setTheme, applyAccent, setAccent, setMood, toggleDiscreet, reset,
            mood: () => mood, discreet: () => discreet});
    }

    return Object.freeze({create, ACCENTS});
});
