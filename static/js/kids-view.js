// Altera — the kids view. Only kids pals, and a PIN to leave.
//
// The server refuses everything else for a browser in this view (core/routes/lock.py); this file only hides the
// controls that would lead there and asks for the PIN when a grown-up wants out.
(() => {
    if (!document.body.classList.contains('kids-view')) return;
    const byId = id => document.getElementById(id);
    const heading = byId('sidebarPeopleTitle');
    if (heading) heading.textContent = 'Kids pals';
    const badge = byId('kidsViewBadge'), leave = byId('leaveKidsBtn');
    if (badge) badge.hidden = false;
    if (leave) leave.hidden = false;

    const dialog = byId('leaveKidsDialog'), form = byId('leaveKidsForm');
    const input = byId('leaveKidsPin'), status = byId('leaveKidsStatus'), go = byId('leaveKidsGo');
    if (!dialog || !form) return;
    let busy = false;

    function say(text) { if (status) status.textContent = text || ''; }

    function open() {
        if (input) input.value = '';
        say('');
        if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
        if (input) input.focus();
    }

    function close() {
        if (typeof dialog.close === 'function') dialog.close(); else dialog.removeAttribute('open');
        if (leave) leave.focus();
    }

    if (leave) leave.addEventListener('click', event => { event.preventDefault(); open(); });
    const cancel = byId('leaveKidsCancel');
    if (cancel) cancel.addEventListener('click', close);

    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (busy) return;
        busy = true;
        if (go) go.disabled = true;
        say('Checking the PIN…');
        try {
            const response = await fetch('/api/lock/leave', {
                method: 'POST', cache: 'no-store', credentials: 'same-origin',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({pin: input ? input.value : ''}),
            });
            let data = null;
            try { data = await response.json(); } catch (_) { /* an error page without JSON */ }
            if (!response.ok) {
                if (response.status === 423 || (data && data.code === 'locked')) { window.location.assign('/lock'); return; }
                throw new Error((data && data.error) || `Altera refused the request (${response.status}).`);
            }
            window.location.assign('/home');
        } catch (error) {
            say(error.message || 'Could not leave kids mode. Try again.');
            if (input) { input.focus(); input.select(); }
        } finally {
            busy = false;
            if (go) go.disabled = false;
        }
    });
})();