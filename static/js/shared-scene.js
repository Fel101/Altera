// One-shot shared picture request. Original files remain on the user's device.
window.AlteraSharedScene = (() => {
    'use strict';
    const MAX_BYTES = 8 * 1024 * 1024, MAX_PIXELS = 4 * 1024 * 1024;
    let owner, context = null, pending = null, review = null, busy = false, stopped = false, controller = null, reviewController = null;
    const el = id => document.getElementById('sharedScene' + id);
    const current = () => ({session_id: currentSessionId, avatar_id: String(Companion.active?.id || ''),
        dataset_epoch: AlteraAPI.epoch, selection: Companion.selection});
    const same = (a, b) => Boolean(a && b && ['session_id', 'avatar_id', 'dataset_epoch', 'selection'].every(key => a[key] === b[key]));
    const dirty = () => Boolean(pending || el('Text')?.value.trim() || el('File')?.files?.length);
    const status = text => { if (owner.active && el('Status')) el('Status').textContent = text; };
    function sync() {
        if (!owner.active) return;
        for (const name of ['File', 'Text', 'Self', 'Providers']) el(name).disabled = busy || Boolean(pending);
        el('Providers').disabled ||= !review;
        el('Submit').disabled = busy || (!pending && !review);
        el('Submit').textContent = pending ? 'Retry same request' : 'Create imagined picture';
        el('Discard').disabled = busy; el('Close').disabled = busy;
    }
    async function loadReview(origin) {
        reviewController?.abort();
        const request = new AbortController(); reviewController = request;
        review = null; el('Providers').checked = false;
        el('Recipients').textContent = 'Checking configured recipients…'; sync();
        const timeout = setTimeout(() => request.abort(), 15000);
        try {
            const response = await fetch('/api/shared-scenes/context', {method:'POST', headers:{'Content-Type':'application/json'},
                body:JSON.stringify({session_id:origin.session_id, avatar_id:origin.avatar_id, dataset_epoch:origin.dataset_epoch}),
                signal:request.signal});
            const value = await response.json(); owner.assert();
            if (reviewController !== request || !same(origin, context) || !same(origin, current())) return;
            if (!response.ok) throw new Error(value?.error || 'Could not review the picture providers.');
            if (!value || typeof value.vision_endpoint !== 'string' || typeof value.image_endpoint !== 'string'
                    || typeof value.vision_model !== 'string' || !/^[a-f0-9]{64}$/.test(value.provider_fingerprint)) {
                throw new Error('Could not verify the picture providers.');
            }
            review = value;
            el('Recipients').textContent = `Vision: ${value.vision_endpoint} · Model: ${value.vision_model} · Image: ${value.image_endpoint}`;
        } catch (error) {
            if (owner.active && reviewController === request) el('Recipients').textContent = error.message || 'Could not review the picture providers.';
        } finally {
            clearTimeout(timeout); if (reviewController === request) reviewController = null; sync();
        }
    }
    function open() {
        if (!owner.active) return;
        const now = current();
        if (!Number.isSafeInteger(now.session_id) || now.session_id <= 0 || !now.avatar_id || !now.dataset_epoch) {
            showReplyNotice('Open a companion conversation before requesting a shared picture.'); return;
        }
        if (context && !same(context, now) && dirty()) { showReplyNotice('Review or discard the original shared-picture draft first.'); return; }
        const changed = !same(context, now);
        context = now;
        el('Context').textContent = `For ${Companion.active?.name || 'this companion'} · Conversation ${now.session_id}`;
        if (!el('Dialog').open) el('Dialog').showModal();
        if (!pending && (changed || !review)) void owner.run(() => loadReview(now)).catch(() => {});
        sync();
    }
    function close() {
        if (!owner.active || busy) return;
        if (el('Dialog').open) el('Dialog').close();
    }
    function discard() {
        if (!owner.active || busy) return;
        if (dirty() && !window.confirm(pending
            ? 'Discard this draft? The earlier request may still finish; check the original conversation before trying again.'
            : 'Discard this shared-picture draft? Your original file will not be changed.')) return;
        pending = null; context = current(); el('Form').reset(); status('Draft discarded. Your original file was not changed.');
        void owner.run(() => loadReview(context)).catch(() => {}); sync();
    }
    function prepareNavigation() {
        if (!owner.active) return false;
        if (!dirty() && !busy) return true;
        if (!el('Dialog').open) el('Dialog').showModal();
        status('Finish or discard this draft before switching companions.');
        return false;
    }
    async function pngData(file) {
        if (!file || file.size <= 0 || file.size > MAX_BYTES || !['image/png', 'image/jpeg'].includes(file.type)) {
            throw new Error('Choose a PNG or JPEG no larger than 8 MiB.');
        }
        const bitmap = await createImageBitmap(file);
        try {
            owner.assert();
            if (!bitmap.width || !bitmap.height || bitmap.width > 4096 || bitmap.height > 4096
                    || bitmap.width * bitmap.height > MAX_PIXELS) throw new Error('Use an image of at most 4 megapixels and 4096 pixels per side.');
            const canvas = document.createElement('canvas'); canvas.width = bitmap.width; canvas.height = bitmap.height;
            const context2d = canvas.getContext('2d'); if (!context2d) throw new Error('This browser cannot prepare the image.');
            context2d.drawImage(bitmap, 0, 0);
            const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
            owner.assert();
            if (!blob || blob.size > MAX_BYTES) throw new Error('The prepared image is larger than 8 MiB. Choose a smaller photo.');
            const value = await new Promise((resolve, reject) => {
                const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('The photo could not be read.'));
                reader.readAsDataURL(blob);
            });
            owner.assert();
            if (typeof value !== 'string' || !value.startsWith('data:image/png;base64,')) throw new Error('The photo could not be prepared.');
            return value.slice('data:image/png;base64,'.length);
        } finally { bitmap.close(); }
    }
    async function submit(event) {
        event.preventDefault();
        if (!owner.active || busy || !same(context, current())) { status('The conversation changed. Review this draft before continuing.'); return; }
        busy = true; sync();
        try {
            if (!pending) {
                if (!review || !el('Self').checked || !el('Providers').checked || !el('Text').value.trim()) throw new Error('Review the recipients, complete both confirmations and describe the scene.');
                const origin = context, scene = el('Text').value.trim(), file = el('File').files[0];
                status('Preparing a copy on this device…');
                const reference_png = await pngData(file);
                if (!same(origin, current())) throw new Error('The conversation changed. Review this draft before continuing.');
                pending = {session_id: origin.session_id, avatar_id: origin.avatar_id, dataset_epoch: origin.dataset_epoch,
                    request_id: 'shared_' + crypto.randomUUID().replace(/-/g, ''), scene, reference_png,
                    self_reference: true, adult_confirmation: true, provider_confirmation: true,
                    provider_fingerprint: review.provider_fingerprint};
            }
            const submitted = pending;
            controller = new AbortController();
            const timeout = setTimeout(() => controller?.abort(), 30000);
            let response, value;
            try {
                status('Submitting the reviewed image; its providers may retain it…');
                response = await fetch('/api/shared-scenes', {method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify(submitted), signal:controller.signal});
                value = await response.json();
            } finally { clearTimeout(timeout); controller = null; }
            owner.assert();
            if (!response.ok) throw new Error(value?.error || 'The request could not be confirmed.');
            if (!value || !/^p-[0-9a-f]{10}$/.test(value.id) || !Number.isSafeInteger(value.message_id)
                    || value.avatar_id !== submitted.avatar_id || value.session_id !== submitted.session_id) {
                throw new Error('The request response could not be verified.');
            }
            if (!same(context, current())) throw new Error('The conversation changed. Check the original conversation for the picture.');
            const message = appendMessage('companion', '');
            const card = document.createElement('div'); card.className = 'photo-card'; card.dataset.photoId = value.id;
            message.querySelector('.message-content').append(card); hydratePhotos(message);
            pending = context = review = null; el('Form').reset(); el('Dialog').close();
            showReplyNotice('Imagined picture requested. It will appear in this conversation.');
        } catch (error) {
            if (owner.active) status(pending
                ? `${error.message || 'The result is uncertain.'} Retry the same request or check the original conversation before discarding.`
                : (error.message || 'The image could not be prepared.'));
        } finally { busy = false; sync(); }
    }
    owner = AlteraPage.component('shared-scene', {
        stop() { stopped = true; controller?.abort(); reviewController?.abort(); return true; },
        clear() { pending = context = review = null; busy = false; el('Form')?.reset(); if (el('Dialog')?.open) el('Dialog').close(); return true; }
    });
    AlteraPage.ready(() => {
        el('Open').addEventListener('click', open);
        el('Close').addEventListener('click', close);
        el('Discard').addEventListener('click', discard);
        el('Form').addEventListener('submit', event => { event.preventDefault(); void owner.run(() => submit(event)).catch(() => {}); });
        el('Dialog').addEventListener('cancel', event => { event.preventDefault(); close(); });
    });
    window.addEventListener('beforeunload', event => { if (!stopped && dirty()) { event.preventDefault(); event.returnValue = ''; } });
    return Object.freeze({open, prepareNavigation});
})();
