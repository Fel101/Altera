// Decorative local scenery only: no model, location service or network requests.
(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else {
        const room = api.create({document: root.document, storage: root.AlteraStorage.local,
            now: () => new Date(), setTimer: root.setTimeout.bind(root), clearTimer: root.clearTimeout.bind(root),
            flush: () => root.AlteraPage.flush()});
        root.AlteraRoom = Object.freeze({refresh: room.refresh});
        root.AlteraPage.ready(room.start);
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';
    const modes = new Set(['off', 'day', 'night', 'auto']);
    const description = 'A decorative room around the portrait. Automatic seasons follow the northern calendar. No pictures are generated.';
    function create({document, storage, now, setTimer, clearTimer, flush}) {
        let closed = false, timer = null, select = null, status = null;
        function clearTimerIfNeeded() { if (timer !== null) clearTimer(timer); timer = null; }
        function clearScene() {
            const data = document.documentElement.dataset;
            delete data.room; delete data.roomLight; delete data.roomSeason;
        }
        function paint(mode) {
            clearTimerIfNeeded(); clearScene();
            if (closed || mode === 'off') return;
            const date = now(), hour = date.getHours(), month = date.getMonth();
            const data = document.documentElement.dataset;
            data.room = 'on';
            data.roomLight = mode === 'auto' ? (hour >= 7 && hour < 19 ? 'day' : 'night') : mode;
            data.roomSeason = mode === 'auto'
                ? (month === 11 || month < 2 ? 'winter' : month < 5 ? 'spring' : month < 8 ? 'summer' : 'autumn')
                : 'neutral';
            if (mode === 'auto' && !document.hidden) timer = setTimer(refresh, 60000);
        }
        function refresh() {
            if (closed) return;
            let mode = 'off';
            try { const saved = storage.getItem('altera_room'); if (modes.has(saved)) mode = saved; } catch (_) { /* Use the default. */ }
            if (select) select.value = mode;
            paint(mode);
        }
        async function change() {
            if (closed || !modes.has(select.value)) return;
            try {
                storage.setItem('altera_room', select.value);
                paint(select.value);
                await flush();
                if (!closed && status) status.textContent = description;
            } catch (_) {
                if (!closed && status) status.textContent = 'The room preference could not be saved. Keep this page open and retry.';
            }
        }
        function visibility() { if (!closed) refresh(); }
        function start() {
            if (closed) return;
            select = document.getElementById('roomMode'); status = document.getElementById('roomStatus');
            select?.addEventListener('change', change);
            document.addEventListener('visibilitychange', visibility);
            refresh();
        }
        function close() {
            closed = true; clearTimerIfNeeded(); clearScene();
            select?.removeEventListener('change', change);
            document.removeEventListener('visibilitychange', visibility);
            if (select) select.value = 'off';
            select = status = null;
            return true;
        }
        return Object.freeze({start, refresh, close});
    }
    return Object.freeze({create});
});
