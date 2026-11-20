window.BackupUI = (() => {
    const el = id => document.getElementById(id);
    const keys = ['altera_theme', 'altera_accent', 'altera_dismissed_hints', 'altera_room'];
    const secrets = ['backupPassword', 'backupPasswordConfirm', 'restoreSecret', 'recoveryPassword', 'recoveryPasswordConfirm', 'backupRecoveryKeyValue'];
    const creationOptions = ['backupEncrypted', 'backupPassword', 'backupPasswordConfirm', 'backupRecoveryKey', 'backupRecoveryKeyValue'];
    const restoreOptions = ['backupFile', 'restoreUnlockMethod', 'restoreSecret', 'restoreConnections', 'recoveryEncrypted', 'recoveryPassword', 'recoveryPasswordConfirm'];
    let preview = null, busy = false, action = '', recoveryRequired = false, selected = null;
    let serial = 0, revision = 0, controller = null, opener = null, recoveryKeyArchive = '';
    let serverCompatible = false;
    const downloads = new Set();
    const tracked = action => (...args) => owner.run(() => action(...args)).catch(error => { if (owner.active) throw error; });
    const restartMessage = 'Restart Altera to use this backup interface. Saved backups can still be downloaded.';
    function clean(text) {
        let value = String(text || '');
        for (const id of secrets) { const secret = el(id).value; if (secret) value = value.split(secret).join('[redacted]'); }
        return value.slice(0, 2000);
    }
    function status(text) { el('backupStatus').textContent = clean(text); }
    function mutation() { return action === 'create' || action === 'apply'; }
    function sync() {
        for (const id of creationOptions) el(id).disabled = busy || recoveryRequired || !serverCompatible;
        for (const id of restoreOptions) el(id).disabled = busy || !serverCompatible;
        el('backupCreate').disabled = busy || recoveryRequired || !serverCompatible;
        el('backupPreview').disabled = busy || !serverCompatible;
        el('backupApply').disabled = busy || !serverCompatible || !preview || !el('restoreAck').checked;
        el('backupClose').disabled = busy && mutation(); el('restoreAck').disabled = busy;
        el('restoreConnections').disabled ||= recoveryRequired;
        el('backupRecoveryKey').disabled ||= !el('backupEncrypted').checked;
        el('backupPasswordFields').hidden = !el('backupEncrypted').checked;
        el('backupPlaintextWarning').hidden = el('backupEncrypted').checked;
        el('restoreSecretFields').hidden = el('restoreUnlockMethod').value === 'none';
        if (preview?.encrypted) el('recoveryEncrypted').checked = true;
        el('recoveryEncrypted').disabled ||= Boolean(preview?.encrypted) || recoveryRequired;
        el('recoverySettings').hidden = recoveryRequired;
        el('restoreAckText').textContent = recoveryRequired ? 'I understand that existing conversations, memories, pictures and notes will be replaced with the saved recovery backup. No additional recovery copy will be created.' : 'I understand that existing conversations, memories, pictures and notes will be replaced. A recovery backup will be saved first.';
        el('recoveryPassword').disabled ||= recoveryRequired; el('recoveryPasswordConfirm').disabled ||= recoveryRequired;
        el('recoveryPasswordFields').hidden = !el('recoveryEncrypted').checked;
        el('recoveryPlaintextWarning').hidden = el('recoveryEncrypted').checked;
        el('backupRecoveryKeyDownload').disabled = busy || !el('backupRecoveryKeyValue').value;
        for (const row of el('backupList').children) for (const button of row.children) {
            if (button.tagName === 'BUTTON') button.disabled = busy || !serverCompatible || (recoveryRequired && button.dataset.backupId !== el('backupList').dataset.recoveryId);
        }
    }
    function invalidate() {
        preview = null; el('restorePreview').replaceChildren(); el('restorePreview').hidden = true;
        el('backupApply').hidden = true; el('restoreAckLabel').hidden = true; el('restoreAck').checked = false; sync();
    }
    function changed() {
        if (busy && mutation()) return;
        revision++; if (action === 'preview') controller?.abort(); invalidate();
        status('Restore inputs changed. Preview the selected backup again.');
    }
    function begin(kind) {
        if (busy || !owner.active) return null;
        busy = true; action = kind; controller = new AbortController();
        const job = {id: ++serial, revision, signal: controller.signal}; sync(); return job;
    }
    function current(job) { return job && owner.active && el('backupDialog').open && serial === job.id && revision === job.revision; }
    function finish(job) { if (job && serial === job.id) { busy = false; action = ''; controller = null; sync(); } }
    function fail(message, focus) { const error = new Error(message); error.focusId = focus; throw error; }
    function password(id, confirmation, label) {
        const value = el(id).value;
        if (Array.from(value).length < 12 || new TextEncoder().encode(value).length > 1024) fail(`${label} needs at least 12 characters and at most 1,024 UTF-8 bytes. Spaces are preserved.`, id);
        if (value !== el(confirmation).value) fail(`${label} confirmation does not match.`, confirmation);
        return value;
    }
    function credential() {
        const method = el('restoreUnlockMethod').value, value = el('restoreSecret').value;
        if (method === 'none') return {};
        if (!['password', 'recovery_key'].includes(method)) fail('Choose an unlock method.', 'restoreUnlockMethod');
        if (!value || new TextEncoder().encode(value).length > 1024) fail('Enter the backup password or recovery key (at most 1,024 UTF-8 bytes).', 'restoreSecret');
        return {[method]: value};
    }
    function showError(error) {
        status(error.name === 'AbortError' ? 'The preview was canceled. Current data has not been changed.' : error.message);
        if (error.focusId) el(error.focusId).focus();
    }
    async function request(url, init = {}) {
        const response = await fetch(url, {...init, cache: 'no-store'});
        let data;
        try { data = await response.json(); } catch { throw new Error('Altera did not return a readable backup response.'); }
        if (!response.ok) throw new Error(data.error || `Backup request failed (HTTP ${response.status}).`);
        return data;
    }
    function tabValue(key, empty) {
        try {
            const raw = AlteraStorage.session.getItem(key);
            return raw === null ? empty : JSON.parse(raw);
        } catch (_) {
            throw new Error('Saved tab drafts could not be read. Your backup or restore was not started; keep this tab open and review its drafts.');
        }
    }
    function appendTabDraft(drafts, item, allowMissingMessage = false) {
        if (!item || typeof item !== 'object' || Array.isArray(item) ||
                (typeof item.message !== 'string' && !(allowMissingMessage && item.message === undefined))) {
            throw new Error('Saved tab drafts could not be read. Your backup or restore was not started; keep this tab open and review its drafts.');
        }
        if (typeof item.message === 'string') drafts.push({session_id: item.resolved_session_id || item.session_id || null, message: item.message});
    }
    function tabDrafts() {
        const drafts = [];
        const pending = tabValue('altera_pending_turns', {});
        if (!pending || typeof pending !== 'object' || Array.isArray(pending)) throw new Error('Saved tab drafts could not be read. Your backup or restore was not started; keep this tab open and review its drafts.');
        Object.values(pending).forEach(item => appendTabDraft(drafts, item, true));
        const savedComposer = tabValue('altera_composer_draft', null);
        if (savedComposer !== null) appendTabDraft(drafts, savedComposer);
        const restored = tabValue('altera_restored_drafts', []);
        if (!Array.isArray(restored)) throw new Error('Saved tab drafts could not be read. Your backup or restore was not started; keep this tab open and review its drafts.');
        restored.forEach(item => appendTabDraft(drafts, item));
        const composer = el('messageInput');
        if (composer?.value.trim()) drafts.push({session_id: typeof currentSessionId !== 'undefined' ? currentSessionId : null, message: composer.value});
        return drafts;
    }
    async function browserState() {
        const epoch = typeof AlteraAPI !== 'undefined' ? AlteraAPI.epoch : null;
        const preferences = {};
        for (const key of keys) { const value = AlteraStorage.local.getItem(key); if (value !== null) preferences[key] = value; }
        preferences.altera_theme ||= 'dark'; preferences.altera_accent ||= '#b59cff'; preferences.altera_dismissed_hints ||= '[]';
        if (typeof getQueuedMessages !== 'function') throw new Error('Saved drafts are unavailable. Your backup or restore was not started; reload Altera and try again.');
        const before = tabDrafts();
        const drafts = (await getQueuedMessages(true, {requireReadable: true})).map(d => ({session_id: d.session_id, message: d.message}));
        const after = tabDrafts(), remaining = new Map();
        for (const item of after) {
            const key = JSON.stringify([item.session_id, item.message]);
            remaining.set(key, (remaining.get(key) || 0) + 1);
        }
        for (const item of before) {
            const key = JSON.stringify([item.session_id, item.message]), count = remaining.get(key) || 0;
            if (count) remaining.set(key, count - 1);
            else drafts.push(item);
        }
        drafts.push(...after);
        if ((typeof AlteraAPI !== 'undefined' ? AlteraAPI.epoch : null) !== epoch) {
            throw new Error('Altera’s data changed while collecting drafts. Your backup or restore was not started; reload and review them.');
        }
        return {preferences, drafts, dataset_epoch: epoch};
    }
    async function list(job) {
        const data = await request('/api/backups', {signal: job.signal});
        if (!current(job)) return;
        serverCompatible = data.backup_api_version === 2;
        recoveryRequired = Boolean(data.recovery_required);
        if (recoveryRequired) el('restoreConnections').checked = false;
        const box = el('backupList'); box.replaceChildren(); box.dataset.recoveryId = data.recovery_backup || '';
        if (!data.backups.length) box.textContent = 'No backups yet.';
        for (const item of data.backups) {
            const row = document.createElement('div'); row.className = 'backup-row';
            const label = document.createElement('p');
            label.textContent = `${item.id === data.recovery_backup ? 'Recovery backup · ' : ''}${new Date(item.created_at).toLocaleString()} · ${item.encrypted ? 'Encrypted · contents hidden until unlock' : `Plaintext ZIP · ${item.counts?.messages || 0} messages`} · ${(item.bytes / 1024 / 1024).toFixed(1)} MB`;
            const download = document.createElement('a'); download.textContent = 'Download';
            download.href = item.download_url;
            const restore = document.createElement('button'); restore.type = 'button'; restore.textContent = 'Preview restore'; restore.dataset.backupId = item.id;
            restore.addEventListener('click', () => {
                if (busy || !serverCompatible) return;
                selected = {id: item.id, encrypted: Boolean(item.encrypted)}; el('backupFile').value = '';
                if (!item.encrypted) { el('restoreUnlockMethod').value = 'none'; el('restoreSecret').value = ''; }
                else if (el('restoreUnlockMethod').value === 'none') el('restoreUnlockMethod').value = 'password';
                changed(); return tracked(makePreview)();
            });
            row.append(label, download, restore); box.append(row);
        }
        if (!serverCompatible) status(restartMessage);
        else if (recoveryRequired) status('Recovery is required. Unlock and restore the marked recovery backup before resuming conversations.');
        sync();
    }
    async function create() {
        if (busy) return;
        if (!serverCompatible || recoveryRequired) { status(restartMessage); return; }
        let payload, focus, sent = false;
        try {
            payload = {format: el('backupEncrypted').checked ? 'encrypted' : 'zip'};
            if (payload.format === 'encrypted') {
                payload.password = password('backupPassword', 'backupPasswordConfirm', 'Backup password');
                payload.include_recovery_key = el('backupRecoveryKey').checked;
            }
        } catch (error) { showError(error); return; }
        const job = begin('create'); if (!job) return; status('Creating a consistent backup…');
        try {
            payload.browser_state = await browserState(); if (!current(job)) return;
            sent = true;
            const data = await request('/api/backups/v2', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload), signal: job.signal});
            if (!current(job)) return;
            if (data.encrypted !== (payload.format === 'encrypted')) throw new Error('The server did not confirm the requested backup format. Review saved backups before retrying.');
            el('backupRecoveryKeyValue').value = data.recovery_key || '';
            recoveryKeyArchive = data.recovery_key && typeof data.id === 'string' ? data.id.slice(0, 128).replace(/[^A-Za-z0-9._-]/g, '_') : '';
            el('backupRecoveryKeyPanel').hidden = !data.recovery_key;
            await list(job); if (!current(job)) return;
            el('backupPassword').value = ''; el('backupPasswordConfirm').value = '';
            status(data.encrypted ? 'Encrypted backup saved. Keep its password or recovery key separately from the downloaded backup.' : 'Plaintext ZIP saved. Anyone with the file can read its contents.');
            focus = data.recovery_key ? 'backupRecoveryKeyDownload' : 'backupCreate';
        } catch (error) {
            if (current(job)) {
                if (sent) status(`${error.message || 'The backup response was interrupted.'} A backup may have been saved; review saved backups before retrying.`);
                else showError(error);
            }
        }
        finally { payload = null; finish(job); if (focus && current(job)) el(focus).focus(); }
    }
    async function makePreview() {
        if (busy) return;
        if (!serverCompatible) { status(restartMessage); return; }
        invalidate(); let init, failureFocus;
        try {
            const unlock = credential(), connections = el('restoreConnections').checked;
            if (selected?.encrypted && !Object.keys(unlock).length) fail('Unlock this encrypted backup with its password or recovery key.', 'restoreUnlockMethod');
            if (selected) init = {headers: {'Content-Type': 'application/json'}, body: JSON.stringify({backup_id: selected.id, restore_connections: connections, ...unlock})};
            else {
                const file = el('backupFile').files[0]; if (!file) fail('Choose a backup file or a saved backup first.', 'backupFile');
                const body = new FormData(); body.append('file', file); body.append('restore_connections', String(connections));
                for (const [key, value] of Object.entries(unlock)) body.append(key, value);
                init = {body};
            }
        } catch (error) { showError(error); return; }
        const job = begin('preview'); if (!job) return; status('Unlocking and checking the backup…');
        try {
            const data = await request('/api/backups/v2/restore/preview', {method: 'POST', ...init, signal: job.signal});
            if (!current(job)) return;
            if (typeof data.token !== 'string' || !data.token || typeof data.encrypted !== 'boolean' || !data.counts || typeof data.counts !== 'object') throw new Error('Altera returned an incomplete restore preview. Preview the backup again before applying.');
            preview = {token: data.token, encrypted: data.encrypted === true, counts: data.counts, notes: data.notes, connections: data.connections};
            if (preview.encrypted) el('recoveryEncrypted').checked = true;
            const box = el('restorePreview'); box.replaceChildren();
            const title = document.createElement('strong'); title.textContent = preview.encrypted ? 'Encrypted backup validated' : 'Plaintext ZIP validated';
            const description = document.createElement('p'), c = preview.counts;
            const count = (value, noun, plural = `${noun}s`) => `${value || 0} ${value === 1 ? noun : plural}`;
            description.textContent = `${count(c.messages, 'message')} in ${count(c.sessions, 'conversation')}; ${count(c.memories, 'memory', 'memories')}; ${count(c.companions, 'companion')}; ${count(c.pictures, 'picture')}; ${count(c.note_files, 'note file')}; ${count(c.unsent_drafts, 'unsent draft')} to review.`;
            const notes = document.createElement('ul');
            for (const note of preview.notes || []) { const li = document.createElement('li'); li.textContent = clean(note); notes.append(li); }
            const connection = document.createElement('li');
            if (preview.connections) { const p = preview.connections; connection.textContent = clean(`Service settings will be restored: ${p.provider}, model ${p.model}, endpoint ${p.model_host || 'default'}; speech ${p.voice_engine}; cloud speech fallback ${p.cloud_voice_fallback ? 'on' : 'off'}.`); }
            else connection.textContent = 'This PC’s current model and voice connections will be kept.';
            notes.append(connection); box.append(title, description, notes); box.hidden = false;
            el('restoreAckLabel').hidden = false; el('backupApply').hidden = false;
            status(recoveryRequired ? 'Review the saved recovery backup, then confirm repair.' : 'Review the replacement and choose a recovery-backup password before confirming.');
        } catch (error) { if (current(job)) { invalidate(); showError(error); failureFocus = el('restoreUnlockMethod').value === 'none' ? 'backupFile' : 'restoreSecret'; } }
        finally { init = null; finish(job); if (failureFocus && current(job)) el(failureFocus).focus(); }
    }
    async function apply() {
        if (!serverCompatible) { status(restartMessage); return; }
        if (busy || !preview || !el('restoreAck').checked) return;
        let payload;
        try {
            payload = {token: preview.token, confirm: 'restore', ...credential()};
            if (!recoveryRequired) {
                payload.recovery_format = el('recoveryEncrypted').checked ? 'encrypted' : 'zip';
                if (preview.encrypted && payload.recovery_format !== 'encrypted') fail('An encrypted source requires an encrypted recovery backup.', 'recoveryEncrypted');
                if (payload.recovery_format === 'encrypted') payload.recovery_password = password('recoveryPassword', 'recoveryPasswordConfirm', 'Recovery-backup password');
            }
        } catch (error) { showError(error); return; }
        const job = begin('apply'); if (!job) return; status(recoveryRequired ? 'Restoring the saved recovery backup…' : 'Saving a recovery backup, then restoring…');
        try {
            payload.browser_state = await browserState(); if (!current(job)) return;
            const result = await request('/api/backups/v2/restore/apply', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload), signal: job.signal});
            if (!current(job)) return;
            for (const id of secrets) el(id).value = '';
            try {
                AlteraStorage.local.setItem('altera_mood', 'off');
                if (result.browser_state) {
                    for (const [key, value] of Object.entries(result.browser_state.preferences)) if (keys.includes(key)) AlteraStorage.local.setItem(key, value);
                    if (typeof restoreBrowserDrafts === 'function') await restoreBrowserDrafts(result.browser_state.drafts);
                    else if (result.browser_state.drafts.length) {
                        const existing = tabValue('altera_restored_drafts', []);
                        if (!Array.isArray(existing)) throw new Error('Saved tab drafts could not be read.');
                        AlteraStorage.session.setItem('altera_restored_drafts', JSON.stringify([...existing, ...result.browser_state.drafts]));
                    }
                }
                AlteraStorage.session.setItem('altera_restore_receipt', JSON.stringify({recovery_backup: result.recovery_backup.id}));
            } catch { status('Server data was restored. Browser drafts could not be restored; they remain in the backup. Reload Altera.'); return; }
            window.location.assign('/');
        } catch (error) { if (current(job)) showError(error); }
        finally { payload = null; finish(job); }
    }
    function reset() {
        controller?.abort(); controller = null; serial++; revision++; busy = false; action = ''; selected = null;
        for (const id of secrets) el(id).value = '';
        recoveryKeyArchive = '';
        serverCompatible = false;
        el('backupEncrypted').checked = true; el('backupRecoveryKey').checked = false; el('backupRecoveryKeyPanel').hidden = true;
        el('recoveryEncrypted').checked = true; el('restoreUnlockMethod').value = 'password'; el('restoreConnections').checked = false; el('backupFile').value = '';
        invalidate(); status('');
    }
    function closed() { reset(); el('backupList').replaceChildren(); if (owner.active) opener?.focus(); opener = null; }
    async function open() {
        if (busy && mutation()) return;
        const dialog = el('backupDialog'); if (!dialog.open) opener = document.activeElement;
        reset(); if (!dialog.open) dialog.showModal();
        const job = begin('list'); if (!job) return; status('Loading backups…');
        try { await list(job); if (current(job) && serverCompatible && !recoveryRequired) status(''); }
        catch (error) { if (current(job)) showError(error); }
        finally { finish(job); }
    }
    function downloadKey() {
        const key = el('backupRecoveryKeyValue').value;
        if (busy || !key || !el('backupDialog').open) return;
        const url = URL.createObjectURL(new Blob([`Altera backup recovery key\nBackup: ${recoveryKeyArchive || 'see saved backup'}\nKeep this separately from the encrypted backup.\n\n${key}\n`], {type: 'text/plain;charset=utf-8'}));
        const link = document.createElement('a'); link.href = url; link.download = `altera-recovery-${recoveryKeyArchive || 'backup'}.txt`;
        document.body.append(link); link.click(); link.remove();
        const download = {url, timer: null}; downloads.add(download);
        download.timer = setTimeout(() => { URL.revokeObjectURL(url); downloads.delete(download); }, 0);
    }
    function init() {
        if (!el('backupDialog')) return;
        el('backupCreate').addEventListener('click', tracked(create)); el('backupPreview').addEventListener('click', tracked(makePreview)); el('backupApply').addEventListener('click', tracked(apply));
        el('backupRecoveryKeyDownload').addEventListener('click', tracked(downloadKey));
        el('backupClose').addEventListener('click', () => { if (!(busy && mutation())) el('backupDialog').close(); });
        el('backupDialog').addEventListener('close', closed);
        el('backupDialog').addEventListener('cancel', event => { if (busy && mutation()) event.preventDefault(); else closed(); });
        el('backupFile').addEventListener('change', () => { selected = null; changed(); }); el('restoreConnections').addEventListener('change', changed);
        el('restoreUnlockMethod').addEventListener('change', () => { el('restoreSecret').value = ''; changed(); }); el('restoreSecret').addEventListener('input', changed);
        el('backupEncrypted').addEventListener('change', () => { el('backupPassword').value = ''; el('backupPasswordConfirm').value = ''; el('backupRecoveryKey').checked = false; sync(); });
        for (const id of ['recoveryEncrypted', 'recoveryPassword', 'recoveryPasswordConfirm']) el(id).addEventListener('input', () => { el('restoreAck').checked = false; sync(); });
        el('recoveryEncrypted').addEventListener('change', () => { el('restoreAck').checked = false; sync(); }); el('restoreAck').addEventListener('change', sync);
        sync();
    }
    const owner = AlteraPage.component('backups', {
        stop() {
            if (!el('backupDialog')) return true;
            for (const row of el('backupList').children) for (const node of row.children) {
                if (node.tagName === 'A') node.removeAttribute('href');
            }
            reset(); el('backupDialog').close();
            for (const download of downloads) { clearTimeout(download.timer); URL.revokeObjectURL(download.url); } downloads.clear();
            return true;
        },
        clear() {
            preview = selected = opener = null; recoveryKeyArchive = ''; recoveryRequired = false;
            el('backupList')?.replaceChildren(); el('restorePreview')?.replaceChildren();
            if (el('backupStatus')) el('backupStatus').textContent = '';
            return true;
        }
    });
    AlteraPage.ready(init);
    return {open: tracked(open), browserState: tracked(browserState)};
})();
