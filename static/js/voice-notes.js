// Durable, explicitly reviewed voice notes are separate from the immediate chat mic.
window.VoiceNotes = (() => {
    const DB_NAME = 'altera-voice-notes', STORE = 'notes', MAX_NOTES = 10;
    const LEGACY_DB_NAME = 'her-voice-notes'; // Altera's old name; browser-state.js moves what is left in it.
    const MAX_BYTES = 100 * 1024 * 1024, MAX_NOTE_BYTES = 20 * 1024 * 1024;
    const notes = new Map(), retaining = new Set(), contexts = new Map(), controllers = new Set(), requestTimers = new Map();
    let dbPromise = null, selectedId = null, opened = false, busy = false, capture = null;
    let pollTimer = null, pollCount = 0, generation = 0, objectUrl = null, initialized = false, suppliedContext = null;
    let settings = {}, suspended = false;
    const stateNames = {upload: 'Ready for transcription', transcribing: 'Transcribing', review: 'Review transcript', ready: 'Reviewed and ready', sending: 'Reply in progress', sent: 'Reply saved', failed: 'Needs attention', stale: 'Context changed'};
    const el = id => document.getElementById(id);
    const selected = () => notes.get(selectedId);
    const id = () => 'vn_' + crypto.randomUUID().replace(/-/g, '');
    function status(text) { if (el('voiceNotesStatus')) el('voiceNotesStatus').textContent = text; }
    function baseContext(value) {
        const sessionId = value?.sessionId ?? value?.session_id;
        const companionId = value?.companionId ?? value?.avatar_id ?? '';
        const datasetEpoch = value?.datasetEpoch ?? value?.dataset_epoch;
        if (!Number.isSafeInteger(sessionId) || sessionId <= 0 || typeof datasetEpoch !== 'string' || !datasetEpoch) {
            throw new Error('Open a conversation in the current dataset before recording a voice note.');
        }
        return {session_id: sessionId, avatar_id: companionId == null ? '' : String(companionId), dataset_epoch: datasetEpoch,
            companion_name: value?.companionName || value?.companion_name || (companionId ? 'Companion' : 'Default companion')};
    }
    function current() {
        const page = {
            sessionId: typeof currentSessionId === 'undefined' ? null : currentSessionId,
            companionId: typeof Companion === 'undefined' ? '' : Companion.active?.id,
            datasetEpoch: typeof AlteraAPI === 'undefined' ? null : AlteraAPI.epoch,
        };
        const value = settings.getContext ? settings.getContext() : typeof currentSessionId !== 'undefined' ? page : suppliedContext || page;
        return baseContext(value);
    }
    const sameBase = (a, b) => a.session_id === b.session_id && a.avatar_id === b.avatar_id && a.dataset_epoch === b.dataset_epoch;
    const sameContext = (a, b) => sameBase(a, b) && a.source_revision === b.source_revision && a.recall_after === b.recall_after;
    const contextKey = value => JSON.stringify([value.session_id, value.avatar_id, value.dataset_epoch]);
    function getDB() {
        if (!dbPromise) dbPromise = new Promise((resolve, reject) => {
            const request = indexedDB.open(DB_NAME, 1);
            request.onupgradeneeded = () => {
                if (!request.result.objectStoreNames.contains(STORE)) request.result.createObjectStore(STORE, {keyPath: 'note_id'});
            };
            request.onsuccess = async () => {
                const db = request.result;
                if (typeof adoptLegacyDatabase === 'function') {
                    try { await adoptLegacyDatabase(indexedDB, LEGACY_DB_NAME, db, STORE); }
                    catch (_) { /* The old notes stay and are tried again next time. */ }
                }
                resolve(db);
            };
            request.onerror = request.onblocked = () => reject(new Error('Voice-note storage could not be opened in this browser.'));
        }).catch(error => { dbPromise = null; throw error; });
        return dbPromise;
    }
    async function localNotes() {
        const db = await getDB();
        return new Promise((resolve, reject) => {
            const request = db.transaction(STORE, 'readonly').objectStore(STORE).getAll();
            request.onsuccess = () => resolve(request.result || []);
            request.onerror = () => reject(request.error || new Error('Voice notes could not be read.'));
        });
    }
    async function persist(note) {
        if (!note.localOrigin) return;
        const db = await getDB();
        await new Promise((resolve, reject) => {
            const tx = db.transaction(STORE, 'readwrite'), store = tx.objectStore(STORE), request = store.getAll();
            let failure = null;
            request.onsuccess = () => {
                const others = (request.result || []).filter(item => item.note_id !== note.note_id);
                const bytes = others.reduce((sum, item) => sum + (item.blob?.size || 0), 0) + (note.blob?.size || 0);
                if (others.length >= MAX_NOTES || bytes > MAX_BYTES) {
                    failure = new Error('This browser can keep at most 10 voice notes and 100 MiB. Delete or download older notes first.');
                    tx.abort(); return;
                }
                store.put({...note, storageError: false, localSaved: true});
            };
            tx.oncomplete = resolve;
            tx.onerror = tx.onabort = () => reject(failure || tx.error || new Error('Browser storage is full or unavailable.'));
        });
        note.storageError = false; note.localSaved = true;
    }
    async function removeLocal(noteId) {
        const db = await getDB();
        await new Promise((resolve, reject) => {
            const tx = db.transaction(STORE, 'readwrite');
            tx.objectStore(STORE).delete(noteId);
            tx.oncomplete = resolve;
            tx.onerror = tx.onabort = () => reject(tx.error || new Error('The browser copy could not be deleted.'));
        });
    }
    async function request(url, options = {}) {
        const controller = new AbortController(); controllers.add(controller);
        const timeout = setTimeout(() => controller.abort(), 25000);
        requestTimers.set(controller, timeout);
        try {
            const response = await fetch(url, {...options, signal: controller.signal, cache: 'no-store'});
            const data = await response.json();
            if (!response.ok) { const error = new Error(data.error || `Voice notes: HTTP ${response.status}`); error.status = response.status; throw error; }
            return data;
        } finally { clearTimeout(timeout); requestTimers.delete(controller); controllers.delete(controller); }
    }
    const json = (method, data) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
    async function freshContext(base = current()) {
        const response = await request(`/api/voice-notes/context?session_id=${base.session_id}`);
        const data = response.context || response;
        const context = {...baseContext(data), companion_name: base.companion_name, source_revision: data.source_revision, recall_after: data.recall_after, approved: true};
        if (!sameBase(base, context) || !Number.isSafeInteger(context.source_revision) || context.source_revision < 0 ||
                !Number.isSafeInteger(context.recall_after) || context.recall_after < 0 || !sameBase(current(), base)) {
            throw new Error('The displayed conversation or its saved context changed. Review it before recording.');
        }
        contexts.set(contextKey(base), context);
        return context;
    }
    async function requireContext(note) {
        if (!note.context?.approved || !sameBase(current(), note.context)) {
            throw new Error('This note belongs to a different or unapproved context. Review it and explicitly copy it to the current context, or record again.');
        }
        const fresh = await freshContext();
        if (!sameContext(fresh, note.context)) throw new Error('Remember/Correct/Forget or conversation changes invalidated this recording context. Record again or explicitly copy it as a new note.');
        return fresh;
    }
    function serverNote(data) { return data.note || data; }
    function mergeServer(note, remote) {
        if (remote.note_id !== note.note_id || remote.session_id !== note.context.session_id ||
                String(remote.avatar_id || '') !== note.context.avatar_id || remote.dataset_epoch !== note.context.dataset_epoch) {
            throw new Error('Altera returned a voice note from another context. The original recording has been kept.');
        }
        note.server = remote;
        return note;
    }
    function fromServer(remote) {
        return {note_id: remote.note_id, created_at: remote.created_at, localOrigin: false, server: remote,
            context: {session_id: remote.session_id, avatar_id: String(remote.avatar_id || ''), dataset_epoch: remote.dataset_epoch,
                source_revision: remote.source_revision, recall_after: remote.recall_after,
                approved: Number.isSafeInteger(remote.source_revision) && Number.isSafeInteger(remote.recall_after)}};
    }
    function stopPoll() { if (pollTimer !== null) clearTimeout(pollTimer); pollTimer = null; }
    function schedulePoll() {
        stopPoll();
        const note = selected();
        if (!opened || suspended || document.hidden || capture || !note || !['transcribing', 'sending'].includes(note.server?.status)) return;
        if (pollCount >= 30) { status('Still working. Use Refresh saved notes to check again; nothing will be sent automatically.'); return; }
        const noteId = note.note_id, version = generation;
        pollTimer = setTimeout(async () => {
            pollTimer = null;
            if (!opened || generation !== version || selectedId !== noteId) return;
            if (busy) { schedulePoll(); return; }
            pollCount++;
            try { await refreshSelected(); }
            catch (error) { status(`Could not refresh this note: ${error.message}`); }
            schedulePoll();
        }, Math.min(5000, 500 * 2 ** Math.min(pollCount, 4)));
    }
    function renderList() {
        const list = el('voiceNotesList'); if (!list) return;
        list.replaceChildren();
        for (const note of [...notes.values()].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))) {
            const button = document.createElement('button'); button.type = 'button';
            button.textContent = `Conversation ${note.context.session_id} · ${stateNames[note.server?.status] || (note.context.approved ? 'Saved in this browser' : 'Context needs review')}${note.storageError ? ' · NOT SAVED' : ''}`;
            button.setAttribute('aria-pressed', String(note.note_id === selectedId));
            button.addEventListener('click', () => { if (!busy && !capture) select(note.note_id); }); list.append(button);
        }
        if (!notes.size) list.textContent = 'No saved voice notes yet.';
    }
    function releaseUrl() { if (objectUrl) URL.revokeObjectURL(objectUrl); objectUrl = null; }
    function renderSelected() {
        const note = selected(); el('voiceNotesDetail').hidden = !note;
        releaseUrl();
        const audio = el('voiceNotesInputAudio'); audio.pause(); audio.removeAttribute('src');
        const download = el('voiceNotesDownload'); download.hidden = true; download.removeAttribute('href');
        el('voiceNotesReplyAudio').replaceChildren();
        if (!note) { sync(); return; }
        const contextReview = note.context.approved ? '' : ' · Context needs review. Use Copy recording to current context before uploading.';
        el('voiceNotesContext').textContent = `${note.context.companion_name || (note.context.avatar_id ? 'Companion' : 'Default companion')} · Conversation ${note.context.session_id} · ${stateNames[note.server?.status] || 'Local recording'}${note.server?.error ? ` · ${note.server.error}` : ''}${note.server?.audio_warning ? ` · ${note.server.audio_warning}` : ''}${contextReview}`;
        if (note.blob) {
            objectUrl = URL.createObjectURL(note.blob); audio.src = objectUrl; download.href = objectUrl; download.hidden = false;
            const type = String(note.blob.type || '').toLowerCase();
            const extension = type.includes('mp4') ? '.m4a' : type.includes('ogg') ? '.ogg' : type.includes('webm') ? '.webm' :
                type.includes('wav') ? '.wav' : type === 'audio/mpeg' || type === 'audio/mp3' ? '.mp3' : '.bin';
            download.download = note.note_id + extension;
        } else if (note.server?.upload_complete && !note.server?.audio_warning) {
            const url = `/api/voice-notes/${note.note_id}/audio`;
            if (url) audio.src = url;
        }
        el('voiceNotesTranscript').value = note.local_edit ?? note.server?.transcript ?? '';
        for (const clip of note.server?.reply_audio || []) {
            if (!/^reply-[0-9a-f]{64}\.(wav|mp3)$/.test(clip.filename || '')) continue;
            const player = document.createElement('audio'); player.controls = true; player.preload = 'none';
            const url = `/api/voice-notes/${note.note_id}/replies/${clip.filename}`;
            if (url) { player.setAttribute('aria-label', 'Saved reply audio'); player.src = url; el('voiceNotesReplyAudio').append(player); }
        }
        if (note.server?.reply_audio_warning) {
            const text = document.createElement('p'); text.textContent = note.server.reply_audio_warning; el('voiceNotesReplyAudio').append(text);
        }
        sync();
    }
    function sync() {
        if (!el('voiceNotesDialog')) return;
        const note = selected(), remote = note?.server;
        const edit = el('voiceNotesTranscript'), unchanged = Boolean(remote && edit.value === remote.transcript);
        for (const name of ['Record', 'Refresh', 'Upload', 'Transcribe', 'Save', 'Review', 'Send', 'Rebind', 'Delete']) el('voiceNotes' + name).disabled = busy || Boolean(capture);
        el('voiceNotesStop').disabled = !capture?.recorder || capture.stopping;
        el('voiceNotesUpload').disabled ||= !note?.blob || !note.context?.approved || Boolean(remote?.upload_complete);
        el('voiceNotesTranscribe').disabled ||= !remote?.upload_complete || Boolean(remote?.audio_warning) || !['upload', 'failed'].includes(remote.status) || remote.error_stage === 'send';
        edit.disabled = busy || Boolean(capture) || !remote || !['review', 'ready'].includes(remote.status);
        el('voiceNotesSave').disabled ||= !remote || !['review', 'ready'].includes(remote.status) || unchanged || !edit.value.trim();
        el('voiceNotesReview').disabled ||= !remote || !['review', 'ready'].includes(remote.status) || !unchanged || !remote.transcript?.trim();
        const canRetrieve = remote && (['ready', 'sending', 'sent'].includes(remote.status) || (remote.status === 'failed' && remote.error_stage === 'send'));
        el('voiceNotesSend').disabled ||= !canRetrieve || !unchanged || remote.reviewed_revision !== remote.revision;
        el('voiceNotesSend').textContent = remote && (['sending', 'sent'].includes(remote.status) || remote.error_stage === 'send') ? 'Retrieve existing reply' : 'Send reviewed note';
        el('voiceNotesRebind').disabled ||= !note?.blob;
        el('voiceNotesDelete').disabled ||= !note || ['sending', 'transcribing'].includes(remote?.status);
    }
    function select(noteId) { selectedId = noteId; pollCount = 0; renderList(); renderSelected(); schedulePoll(); }
    async function operation(action) {
        if (busy || capture) return;
        busy = true; sync();
        try { return await action(); }
        catch (error) { status(error.name === 'AbortError' ? 'Operation cancelled. Any saved recording remains available; retry explicitly.' : error.message); }
        finally { busy = false; sync(); schedulePoll(); }
    }
    async function refreshSelected() {
        const note = selected(); if (!note?.server) return;
        const remote = serverNote(await request(`/api/voice-notes/${note.note_id}?session_id=${note.context.session_id}`));
        mergeServer(note, remote); await persist(note); renderSelected(); renderList();
    }
    async function refresh() {
        return operation(async () => {
            stopPoll();
            let storageFailure = null;
            try { for (const note of await localNotes()) if (!notes.has(note.note_id)) notes.set(note.note_id, note); }
            catch (error) { storageFailure = error; }
            // Local originals remain reviewable when the server is offline.
            if (!notes.has(selectedId)) selectedId = notes.keys().next().value || null;
            renderList(); renderSelected();
            const base = current();
            if (navigator.onLine !== false) {
                const response = await request(`/api/voice-notes?session_id=${base.session_id}`);
                for (const remote of (response.notes || response || []).slice(0, 100)) {
                    const note = notes.get(remote.note_id);
                    if (note) mergeServer(note, remote); else notes.set(remote.note_id, fromServer(remote));
                }
            }
            if (!notes.has(selectedId)) selectedId = notes.keys().next().value || null;
            pollCount = 0; renderList(); renderSelected();
            status(storageFailure ? `${storageFailure.message} New recordings cannot be promised saved.` :
                navigator.onLine === false ? 'Offline. Browser recordings remain here. Upload and Send require your action after reconnecting.' : 'Choose a note, or record one. Upload, transcription, review and Send are separate actions.');
        });
    }
    function supportedMime() {
        if (typeof MediaRecorder === 'undefined') throw new Error('Recording is unavailable in this browser.');
        for (const type of ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']) {
            if (MediaRecorder.isTypeSupported?.(type)) return type;
        }
        throw new Error('This browser cannot record a supported audio format.');
    }
    async function digest(blob) {
        const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256', await blob.arrayBuffer()));
        return [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
    }
    async function record() {
        if (typeof AlteraBackchannel !== 'undefined') AlteraBackchannel.stop();
        return operation(async () => {
            const base = current(), version = generation;
            const saved = await localNotes();
            if (saved.length >= MAX_NOTES || saved.reduce((sum, item) => sum + (item.blob?.size || 0), 0) >= MAX_BYTES) {
                throw new Error('Browser voice-note storage is full. Delete or download an older note before recording.');
            }
            let context;
            try { context = await freshContext(base); }
            catch (error) {
                if (!sameBase(current(), base)) throw error;
                context = contexts.get(contextKey(base)) || {...base, source_revision: null, recall_after: null, approved: false};
            }
            if (!opened || generation !== version || !sameBase(current(), base)) return;
            const mime = supportedMime();
            const stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true}});
            if (!opened || generation !== version || !sameBase(current(), base)) { stream.getTracks().forEach(track => track.stop()); return; }
            const recording = {context: {...context}, note_id: id(), stream, chunks: [], bytes: 0, stopping: false, started: Date.now()};
            try { recording.recorder = new MediaRecorder(stream, {mimeType: mime}); }
            catch (error) { stream.getTracks().forEach(track => track.stop()); throw error; }
            recording.done = new Promise(resolve => { recording.resolve = resolve; });
            recording.recorder.ondataavailable = event => {
                if (event.data.size) { recording.chunks.push(event.data); recording.bytes += event.data.size; }
                if (recording.bytes >= MAX_NOTE_BYTES - 1024 * 1024 && !recording.stopping) stop();
            };
            recording.recorder.onerror = () => {
                status('The microphone stopped unexpectedly. Saving the audio captured so far…');
                recording.stream.getTracks().forEach(track => track.stop());
                if (recording.recorder.state !== 'inactive') stop();
            };
            recording.recorder.onstop = async () => {
                clearTimeout(recording.timer); recording.stream.getTracks().forEach(track => track.stop());
                const blob = new Blob(recording.chunks, {type: recording.recorder.mimeType || mime});
                const note = {note_id: recording.note_id, context: recording.context, blob, localOrigin: true, created_at: new Date().toISOString()};
                notes.set(note.note_id, note); selectedId = note.note_id;
                try {
                    if (!blob.size || blob.size > MAX_NOTE_BYTES) throw new Error('The recording is empty or larger than 20 MiB. Download it before closing; it has not been saved.');
                    await persist(note);
                    status(note.context.approved ? 'Recording saved in this browser. Upload it when you are ready; it will not send automatically.' :
                        'Recording saved in this browser. Its context was unavailable. Explicitly copy it to an approved current context before upload.');
                } catch (error) { note.storageError = true; status(`Recording NOT SAVED: ${error.message} Keep this page open and download the original recording.`); }
                finally { if (capture === recording) capture = null; renderList(); renderSelected(); recording.resolve(note); }
            };
            capture = recording;
            try { recording.recorder.start(1000); }
            catch (error) { capture = null; stream.getTracks().forEach(track => track.stop()); throw error; }
            recording.timer = setTimeout(() => { status('Five-minute limit reached. Saving the recording…'); stop(); }, 300000);
            status(context.approved ? 'Recording a voice note. Press Stop and save recording when finished.' : 'Recording offline without approved context. Context review will be required before upload.'); sync();
        });
    }
    function stop() {
        const recording = capture;
        if (!recording) return Promise.resolve(null);
        if (!recording.stopping) {
            recording.stopping = true; clearTimeout(recording.timer);
            if (recording.recorder.state !== 'inactive') recording.recorder.stop();
            sync();
        }
        return recording.done;
    }
    async function upload() {
        return operation(async () => {
            const note = selected(); if (!note?.blob) throw new Error('Choose a browser recording first.');
            await requireContext(note);
            const hash = await digest(note.blob);
            const form = new FormData();
            for (const [key, value] of Object.entries({note_id: note.note_id, session_id: note.context.session_id,
                dataset_epoch: note.context.dataset_epoch, avatar_id: note.context.avatar_id,
                source_revision: note.context.source_revision, recall_after: note.context.recall_after})) form.append(key, String(value));
            form.append('audio', note.blob, note.note_id + '.audio');
            const response = await request('/api/voice-notes', {method: 'POST', body: form});
            const remote = serverNote(response);
            if (!remote.upload_complete || remote.audio_sha256 !== hash || remote.audio_bytes !== note.blob.size) {
                throw new Error('The upload acknowledgment did not match your recording. The original is kept; retry with the same note ID.');
            }
            mergeServer(note, remote); await persist(note); renderSelected(); renderList();
            status('Altera confirmed the recording hash and size. The browser original is also kept. Choose Transcribe locally; nothing has been sent.');
        });
    }
    async function mutate(action, extra = {}) {
        const note = selected(); if (!note?.server) throw new Error('Upload a recording first.');
        await requireContext(note);
        const remote = serverNote(await request(`/api/voice-notes/${note.note_id}${action ? '/' + action : ''}`,
            json(action ? 'POST' : 'PUT', {...extra, dataset_epoch: note.context.dataset_epoch})));
        mergeServer(note, remote); if (!action) delete note.local_edit;
        await persist(note); renderSelected(); renderList(); pollCount = 0;
        return note;
    }
    const transcribe = () => operation(async () => { await mutate('transcribe'); status('Local transcription requested. Review its text when ready; Send remains a separate action.'); });
    const save = () => operation(async () => {
        const note = selected(), text = el('voiceNotesTranscript').value;
        if (!note?.server || !text.trim()) throw new Error('A nonempty transcript is required.');
        note.local_edit = text;
        if (navigator.onLine === false) {
            note.localOrigin = true; await persist(note);
            status('Transcript edit saved in this browser. Reconnect and save it to Altera before confirming review.'); return;
        }
        await mutate('', {transcript: text, revision: note.server.revision}); status('Transcript edit saved by Altera. Confirm this new revision after reviewing it.');
    });
    const review = () => operation(async () => {
        const note = selected();
        if (!note?.server || el('voiceNotesTranscript').value !== note.server.transcript) throw new Error('Save your transcript edits to Altera before confirming review.');
        await mutate('review', {revision: note.server.revision}); status('This transcript revision is reviewed. Send reviewed note is still required.');
    });
    const send = () => operation(async () => {
        const note = selected(), remote = note?.server;
        const canRetrieve = remote && (['ready', 'sending', 'sent'].includes(remote.status) || (remote.status === 'failed' && remote.error_stage === 'send'));
        if (!canRetrieve || remote.reviewed_revision !== remote.revision || el('voiceNotesTranscript').value !== remote.transcript) {
            throw new Error('Save and confirm the current transcript revision before sending.');
        }
        if (typeof settings.send !== 'function') throw new Error('Chat sending is unavailable. Your reviewed note remains saved.');
        await requireContext(note);
        await settings.send({voice_note_id: note.note_id, reviewed_revision: remote.reviewed_revision,
            request_id: remote.request_id || 'voice_' + note.note_id, session_id: note.context.session_id,
            dataset_epoch: note.context.dataset_epoch, message: remote.transcript});
        await refreshSelected(); status('Reply retrieved. Any saved reply audio is available below; it will not autoplay.');
    });
    const approveContext = () => operation(async () => {
        const original = selected(); if (!original?.blob) throw new Error('A browser recording is required to make a new context copy.');
        const context = await freshContext();
        if (!window.confirm(`Create a NEW voice note from this recording for Conversation ${context.session_id}? Review its transcript again before sending. The original note stays unchanged.`)) return;
        const copy = {note_id: id(), context: {...context}, blob: original.blob, localOrigin: true, created_at: new Date().toISOString()};
        await persist(copy); notes.set(copy.note_id, copy); select(copy.note_id);
        status('A new recording copy is saved with the explicitly approved context and a new note ID. Upload, transcribe and review it before Send.');
    });
    const remove = () => operation(async () => {
        const note = selected(); if (!note || !window.confirm('Delete this voice note, including its recording and transcript? Saved chat messages remain.')) return;
        if (note.server) {
            const here = current();
            if (!sameBase(here, note.context)) throw new Error('Open this note’s original conversation and dataset before deleting its server copy.');
            await request(`/api/voice-notes/${note.note_id}`, json('DELETE', {confirm: 'delete-voice-note', dataset_epoch: note.context.dataset_epoch}));
        }
        if (note.localOrigin && note.localSaved) await removeLocal(note.note_id);
        notes.delete(note.note_id); select(notes.keys().next().value || null); status('Voice note deleted.');
    });
    async function close() {
        generation++; stopPoll(); abortRequests();
        if (capture) {
            const note = await stop();
            if (note?.storageError) { status('The recording is not saved. Download the original before closing, or delete it explicitly.'); return false; }
        }
        if (retaining.size) { status('An interrupted original is still saving. Browser closure remains pending.'); return false; }
        for (const note of notes.values()) if (note.storageError && !note.localSaved && note.localOrigin
            && note.blob?.size > 0 && note.blob.size <= MAX_NOTE_BYTES) {
            try { await persist(note); } catch (_) { /* Preserve the same original for review/retry. */ }
        }
        if ([...notes.values()].some(note => note.storageError && !note.localSaved && !note.server?.upload_complete)) {
            status('An original recording is NOT SAVED. Upload it successfully or delete it explicitly before closing; you can download it first.'); return false;
        }
        try {
            for (const note of notes.values()) if (note.local_edit !== undefined) { note.localOrigin = true; await persist(note); }
        } catch (error) { status(`Transcript edits could not be saved: ${error.message} Copy the edited text before closing.`); return false; }
        if (busy) { status('A voice-note operation is still finishing. Retry close when it settles.'); return false; }
        opened = false; releaseUrl();
        el('voiceNotesInputAudio')?.pause();
        for (const player of el('voiceNotesReplyAudio')?.querySelectorAll('audio') || []) player.pause();
        el('voiceNotesDialog')?.close();
        return true;
    }
    async function retainRecording(blob, value) {
        // A recording that failed to transcribe is kept locally and never transcribed
        // or uploaded again by itself. Context must be reviewed again before any send.
        let base;
        try { base = baseContext(value); }
        catch (_) { base = {session_id: null, avatar_id: '', dataset_epoch: '', companion_name: 'Unassigned recording'}; }
        const note = {note_id: id(), context: {...base, source_revision: null, recall_after: null, approved: false},
            blob, localOrigin: true, created_at: new Date().toISOString()};
        notes.set(note.note_id, note); selectedId = note.note_id; retaining.add(note);
        try {
            if (!blob?.size || blob.size > MAX_NOTE_BYTES) throw new Error('The original is empty or exceeds 20 MiB.');
            await persist(note); return true;
        } catch (_) { note.storageError = true; status('Interrupted recording NOT SAVED. Download its original or retry saving before closing.'); return false; }
        finally { retaining.delete(note); }
    }
    function abortRequests() {
        for (const controller of controllers) { controller.abort(); clearTimeout(requestTimers.get(controller)); }
        controllers.clear(); requestTimers.clear();
    }
    function unsavedOriginal() {
        return Boolean(capture) || [...notes.values()].some(note => note.blob && !note.server?.upload_complete &&
            (!note.localSaved || note.storageError));
    }
    function suspend(unloading = false) {
        // Invalidate a pending permission request even if visibility returns
        // before the browser finally resolves getUserMedia.
        generation++; suspended = true; stopPoll();
        if (unloading) abortRequests();
        const recording = capture;
        if (!recording) return;
        status(unloading ? 'Leaving this page. The microphone is stopped; the browser may close before the recording finishes saving.' :
            'Recording stopped because this page is hidden. Saving it in this browser; nothing will be uploaded or sent.');
        // pagehide is best effort: unloading can terminate the final media
        // event or IndexedDB transaction. Only onstop's completed transaction
        // may say that a recording was saved.
        stop().catch(error => status(`Recording NOT SAVED: ${error.message} Keep this page open and download the original.`));
        recording.stream.getTracks().forEach(track => track.stop());
    }
    function init() {
        if (initialized || !el('voiceNotesDialog')) return;
        initialized = true;
        const actions = {Record: record, Stop: stop, Refresh: refresh, Upload: upload, Transcribe: transcribe,
            Save: save, Review: review, Send: send, Rebind: approveContext, Delete: remove, Close: close};
        for (const [name, action] of Object.entries(actions)) el('voiceNotes' + name).addEventListener('click', action);
        el('voiceNotesTranscript').addEventListener('input', () => { const note = selected(); if (note) note.local_edit = el('voiceNotesTranscript').value; sync(); });
        el('voiceNotesDialog').addEventListener('cancel', event => { event.preventDefault(); close(); });
        el('voiceNotesDialog').addEventListener('close', () => { if (opened) close(); });
    }
    async function open(context) {
        if (typeof AlteraBackchannel !== 'undefined') AlteraBackchannel.stop();
        if (context) suppliedContext = context;
        init(); if (!el('voiceNotesDialog')) throw new Error('The voice-note dialog is unavailable.');
        opened = true; generation++;
        if (!el('voiceNotesDialog').open) el('voiceNotesDialog').showModal();
        await refresh();
    }
    AlteraPage.ready(init);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) suspend();
        else { suspended = false; schedulePoll(); }
    });
    window.addEventListener('pagehide', () => suspend(true));
    window.addEventListener('pageshow', () => { if (!document.hidden) { suspended = false; schedulePoll(); } });
    window.addEventListener('beforeunload', event => {
        if (unsavedOriginal()) { event.preventDefault(); event.returnValue = ''; }
    });
    return {configure: options => { settings = {...settings, ...options}; }, open, close, retainRecording, record, stop, upload, transcribe,
        save, review, send, approveContext, remove, refresh, select, localNotes,
        state: () => ({opened, busy, retaining: retaining.size, recording: Boolean(capture), selectedId, notes: [...notes.values()]})};
})();
