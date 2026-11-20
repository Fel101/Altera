// Altera — the lock screen. Two ways in: the parent PIN (the whole app) or Kids (only kids pals).
(() => {
    const byId = id => document.getElementById(id);
    const form = byId('unlockForm'), input = byId('pinInput'), status = byId('lockStatus');
    const unlock = byId('unlockButton'), kids = byId('kidsButton');
    let busy = false;

    function say(text) { if (status) status.textContent = text || ''; }

    async function post(url, body) {
        const response = await fetch(url, {
            method: 'POST', cache: 'no-store', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body || {}),
        });
        let data = null;
        try { data = await response.json(); } catch (_) { /* an error page without JSON */ }
        if (!response.ok) {
            const error = new Error((data && data.error) || `Altera refused the request (${response.status}).`);
            error.status = response.status;
            error.code = data && data.code;
            throw error;
        }
        return data || {};
    }

    async function enter(button, url, body, destination) {
        if (busy) return;
        busy = true;
        if (button) button.disabled = true;
        say(url === '/api/lock/unlock' ? 'Unlocking…' : 'Opening kids mode…');
        try {
            await post(url, body);
            window.location.assign(destination);
        } catch (error) {
            say(error.message || 'Could not open Altera. Try again.');
            if (input) { input.focus(); input.select(); }
        } finally {
            busy = false;
            if (button) button.disabled = false;
        }
    }

    if (form) form.addEventListener('submit', event => {
        event.preventDefault();
        return enter(unlock, '/api/lock/unlock', {pin: input ? input.value : ''}, '/home');
    });
    if (kids) kids.addEventListener('click', () => enter(kids, '/api/lock/kids', {}, '/'));

    // The PIN field is the first thing to reach for; focus it, and keep focus in the card on a small screen.
    if (input) input.focus();
})();