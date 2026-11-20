// About, and the π in its corner. Adult mode is off by default and out of sight: nothing in Settings, the chat or Home
// mentions it while it is off. The π opens a box that plays a short "you got in" screen, then shows adult mode's settings
// (companion.js fills them in; turning it on still needs the 18+ confirmation, and the parent PIN when one is set). The
// kids view has no π. Hiding it is for tidiness, not security: the server's own checks are what keep it off.
(function (root, factory) {
    'use strict';
    const api = factory(root);
    if (typeof module === 'object' && module.exports) module.exports = api;
    else Object.assign(root, api.globals);
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
    'use strict';
    const LINES = [
        '> altera --whoami',
        '  a grown-up, probably',
        '> unlock hidden_settings',
        '  checking for kids nearby ...... none found',
        '  skipping the boring bits ........... done',
        '> ACCESS GRANTED',
    ];
    const SCREEN = LINES.join('\n');
    let timer = null;

    const byId = id => root.document.getElementById(id);
    function show(id, shown) {
        const node = byId(id);
        if (node) node.style.display = shown ? 'flex' : 'none';
    }
    function closeSettings() { byId('settingsPanel')?.classList.remove('open'); }
    function kidsView() { return Boolean(root.document.body?.classList.contains('kids-view')); }
    function reducedMotion() {
        try { return Boolean(root.matchMedia?.('(prefers-reduced-motion: reduce)').matches); } catch (_) { return false; }
    }

    function openAbout() {
        closeSettings();
        const pi = byId('aboutPi');
        if (pi) pi.hidden = kidsView();
        show('aboutOverlay', true);
        byId('aboutClose')?.focus?.();
    }

    function closeAbout() { show('aboutOverlay', false); }

    function finishVaultIntro() {
        if (timer !== null) root.clearTimeout(timer);
        timer = null;
        const screen = byId('vaultScreen');
        if (screen) screen.textContent = SCREEN;
        const body = byId('vaultBody');
        if (body) body.hidden = false;
    }

    /** The box behind the π. `quick` skips the intro (the mood badge and picture notices open it directly). */
    function openVault(options = {}) {
        if (kidsView()) return;
        closeAbout();
        closeSettings();
        if (typeof root.refreshAdult === 'function') root.refreshAdult();
        show('vaultOverlay', true);
        if (options.quick || reducedMotion()) return finishVaultIntro();
        const screen = byId('vaultScreen');
        byId('vaultBody').hidden = true;
        screen.textContent = '';
        let shown = 0;
        const step = () => {
            shown += 1;
            screen.textContent = SCREEN.slice(0, shown);
            if (shown >= SCREEN.length) return finishVaultIntro();
            timer = root.setTimeout(step, SCREEN[shown - 1] === '\n' ? 260 : 16);
        };
        if (timer !== null) root.clearTimeout(timer);
        step();
    }

    function closeVault() {
        if (timer !== null) root.clearTimeout(timer);
        timer = null;
        show('vaultOverlay', false);
    }

    if (root.document?.addEventListener) {
        root.document.addEventListener('keydown', event => {
            if (event.key !== 'Escape') return;
            closeVault();
            closeAbout();
        });
    }

    return {LINES, globals: {openAbout, closeAbout, openVault, closeVault, finishVaultIntro}};
});
