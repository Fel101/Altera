// Altera — Offline Message Queue (Phase 7: PWA Support)
// Uses IndexedDB for reliable message queuing when offline

const OFFLINE_DB_NAME = 'altera-offline-queue';
const LEGACY_OFFLINE_DB_NAME = 'her-offline-queue'; // Altera's old name; browser-state.js moves what is left in it.
// Older workers opened version 2 for a separate GET queue. Only this page owns the message queue now.
const OFFLINE_DB_VERSION = 3;
const STORE_NAME = 'pending-messages';

let dbPromise = null;
let offlinePollTimer;

function getDB() {
    if (!dbPromise) {
        dbPromise = new Promise((resolve, reject) => {
            const request = indexedDB.open(OFFLINE_DB_NAME, OFFLINE_DB_VERSION);
            
            request.onupgradeneeded = (event) => {
                const db = event.target.result;
                if (!db.objectStoreNames.contains(STORE_NAME)) {
                    const store = db.createObjectStore(STORE_NAME, { keyPath: 'id', autoIncrement: true });
                    store.createIndex('timestamp', 'timestamp', { unique: false });
                    store.createIndex('status', 'status', { unique: false });
                }
            };
            
            request.onsuccess = async () => {
                const db = request.result;
                if (typeof adoptLegacyDatabase === 'function') {
                    try { await adoptLegacyDatabase(indexedDB, LEGACY_OFFLINE_DB_NAME, db, STORE_NAME, {renumber: true}); }
                    catch (_) { /* The old queue stays and is tried again next time. */ }
                }
                resolve(db);
            };
            request.onerror = () => reject(request.error);
        });
    }
    return dbPromise;
}

// ============================================================
// Queue Management
// ============================================================

async function queueMessage(sessionId, message) {
    try {
        const item = {
            session_id: sessionId,
            request_id: crypto.randomUUID(),
            dataset_epoch: typeof AlteraAPI !== 'undefined' ? AlteraAPI.epoch : null,
            message: message,
            timestamp: Date.now(),
            status: 'pending',
            retry_count: 0
        };
        const db = await getDB();
        await new Promise((resolve, reject) => {
            const tx = db.transaction(STORE_NAME, 'readwrite');
            tx.objectStore(STORE_NAME).add(item);
            tx.oncomplete = resolve;
            tx.onerror = tx.onabort = () => reject(tx.error || new Error('The draft was not saved.'));
        });
        
        updateOfflineIndicator();
        return true;
    } catch (err) {
        console.error('[Offline] Failed to queue message:', err);
        return false;
    }
}

async function getQueuedMessages(includeReview = false, {requireReadable = false} = {}) {
    let reading = false;
    try {
        const db = await getDB();
        const tx = db.transaction(STORE_NAME, 'readonly');
        const store = tx.objectStore(STORE_NAME);
        const index = store.index('status');
        
        reading = true;
        return await new Promise((resolve, reject) => {
            const request = includeReview ? store.getAll() : index.getAll('pending');
            let result;
            request.onsuccess = () => { result = (request.result || []).filter(item => item.status !== 'sent'); };
            request.onerror = () => reject(request.error || new Error('Saved drafts could not be read.'));
            tx.oncomplete = () => result === undefined ? reject(new Error('Saved draft read was not completed.')) : resolve(result);
            tx.onerror = tx.onabort = () => reject(tx.error || new Error('Saved draft read was not completed.'));
        });
    } catch (err) {
        console.error('[Offline] Failed to get queued messages:', err);
        if (requireReadable) throw new Error('Saved drafts could not be read. Your backup or restore was not started; try again after browser storage is available.');
        if (reading) throw err;
        return [];
    }
}

async function clearQueuedMessages() {
    try {
        const db = await getDB();
        await new Promise((resolve, reject) => {
            const tx = db.transaction(STORE_NAME, 'readwrite');
            tx.objectStore(STORE_NAME).clear();
            tx.oncomplete = resolve;
            tx.onerror = tx.onabort = () => reject(tx.error || new Error('Saved drafts could not be cleared.'));
        });
        
        updateOfflineIndicator();
    } catch (err) {
        console.error('[Offline] Failed to clear queue:', err);
        throw new Error('Saved drafts could not be cleared. Their remaining copies have been kept.');
    }
}

async function saveQueueItem(item) {
    const db = await getDB();
    await new Promise((resolve, reject) => {
        const tx = db.transaction(STORE_NAME, 'readwrite');
        tx.objectStore(STORE_NAME).put(item);
        tx.oncomplete = resolve;
        tx.onerror = tx.onabort = () => reject(tx.error);
    });
}

async function restoreBrowserDrafts(drafts) {
    const db = await getDB();
    await new Promise((resolve, reject) => {
        const tx = db.transaction(STORE_NAME, 'readwrite');
        for (const draft of drafts) tx.objectStore(STORE_NAME).add({session_id: draft.session_id || null,
            message: draft.message, timestamp: Date.now(), request_id: crypto.randomUUID(), dataset_epoch: null,
            status: 'needs_review', retry_count: 0});
        tx.oncomplete = resolve;
        tx.onerror = tx.onabort = () => reject(tx.error);
    });
    updateOfflineIndicator();
}

async function reviewQueuedMessages() {
    document.getElementById('draftReview')?.remove();
    const dialog = document.createElement('dialog');
    dialog.id = 'draftReview'; dialog.className = 'draft-review';
    dialog.setAttribute('aria-label', 'Review unsent drafts');
    const title = document.createElement('h2'); title.textContent = 'Review unsent drafts';
    const hint = document.createElement('p'); hint.textContent = 'These messages will stay unsent until you choose a draft and press Send yourself.';
    const close = document.createElement('button'); close.type = 'button'; close.textContent = 'Close';
    close.addEventListener('click', () => dialog.close());
    const feedback = document.createElement('p'); feedback.setAttribute('role', 'status');
    dialog.append(title, hint, close, feedback);
    for (const item of await getQueuedMessages(true)) {
        const row = document.createElement('article');
        const label = document.createElement('p'); label.textContent = item.session_id ? `Conversation ${item.session_id}` : 'Unsent message';
        const input = document.createElement('textarea'); input.value = item.message; input.setAttribute('aria-label', 'Unsent draft');
        const use = document.createElement('button'); use.type = 'button';
        use.textContent = item.session_id && Number(item.session_id) !== Number(currentSessionId) ? 'Use in this conversation' : 'Use as draft';
        use.addEventListener('click', async () => {
            const composer = document.getElementById('messageInput');
            if (composer.value.trim()) { feedback.textContent = 'Send or clear the text already in the composer first.'; return; }
            const destination = currentSessionId, content = input.value;
            composer.value = content;
            try {
                await deleteQueuedMessage(item.id);
            } catch (_) { feedback.textContent = 'The composer copy could not be confirmed. The saved draft has been kept for retry.'; return; }
            dialog.close(); autoResize(composer); composer.focus();
        });
        row.append(label, input, use); dialog.append(row);
    }
    document.body.append(dialog); dialog.showModal();
}

async function deleteQueuedMessage(id) {
    try {
        const db = await getDB();
        await new Promise((resolve, reject) => {
            const tx = db.transaction(STORE_NAME, 'readwrite');
            tx.objectStore(STORE_NAME).delete(id);
            tx.oncomplete = resolve;
            tx.onerror = tx.onabort = () => reject(tx.error || new Error('The saved draft could not be removed.'));
        });
        
        updateOfflineIndicator();
    } catch (err) {
        console.error('[Offline] Failed to delete queued message:', err);
        throw new Error('The saved draft could not be removed. Its copy has been kept.');
    }
}

// ============================================================
// Retry Logic — send all pending messages when online
// ============================================================

let retryTimeout = null;
let queueRetryInProgress = false;

async function retryQueuedMessages() {
    if (queueRetryInProgress) return;
    queueRetryInProgress = true;
    let success = 0, failed = 0;
    try {
        for (const item of await getQueuedMessages()) {
            try {
                // Persist the key BEFORE sending, including for queues created by an older app.
                item.request_id ||= crypto.randomUUID();
                await saveQueueItem(item);
                if (!item.dataset_epoch || (typeof AlteraAPI !== 'undefined' && item.dataset_epoch !== AlteraAPI.epoch)) {
                    item.status = 'needs_review';
                    await saveQueueItem(item);
                    failed++;
                    continue;
                }
                const response = await fetch('/api/chat', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ session_id: item.session_id, message: item.message, request_id: item.request_id, dataset_epoch: item.dataset_epoch })
                });
                const result = await response.json();
                if (response.ok && result.status === 'saved') {
                    await deleteQueuedMessage(item.id);
                    success++;
                    if (typeof currentSessionId !== 'undefined' && currentSessionId === item.session_id && !isStreaming) {
                        await loadMessages(currentSessionId);
                    }
                } else {
                    // Keep a failed message visible. A failed turn needs an explicit new attempt, not endless resending.
                    if (result.code === 'dataset_changed' || response.status === 410 || result.status === 'failed') {
                        item.status = 'needs_review';
                        await saveQueueItem(item);
                    }
                    failed++;
                }
            } catch (err) {
                console.warn('[Offline] Could not retrieve the queued reply:', err);
                failed++;
            }
        }
    } catch (_) {
        failed++;
    } finally {
        queueRetryInProgress = false;
        updateOfflineIndicator();
    }
    if (success) showNotification(`Sent ${success} queued message(s)`, 'success');
    return { success, failed };
}

// Schedule periodic retry when online
function scheduleRetry() {
    if (retryTimeout) clearTimeout(retryTimeout);
    retryTimeout = setTimeout(async () => {
        const isOnline = navigator.onLine;
        if (isOnline) {
            await retryQueuedMessages();
        }
        // Keep checking every 30 seconds when online
        scheduleRetry();
    }, 30000);
}

// ============================================================
// Online/Offline Detection
// ============================================================

let isOnline = navigator.onLine;
let lastOnlineState = null;

function updateOnlineStatus() {
    const wasOnline = isOnline;
    isOnline = navigator.onLine;
    
    if (wasOnline !== isOnline) {
        lastOnlineState = isOnline;
        console.log(`[Offline] Network ${isOnline ? 'restored' : 'lost'}`);
        
        updateOfflineIndicator();
        
        // If we came back online, retry queued messages immediately
        if (isOnline) {
            hideOfflineBanner();
            retryQueuedMessages();
            scheduleRetry();
        } else {
            // Show offline banner when going offline
            showOfflineBanner();
        }
    }
}

window.addEventListener('online', updateOnlineStatus);
window.addEventListener('offline', updateOnlineStatus);

// ============================================================
// UI Updates
// ============================================================

function updateOfflineIndicator() {
    const indicator = document.getElementById('offlineIndicator');
    if (!indicator) return;
    
    getQueuedMessages(true).then(queue => {
        if (queue.length > 0) {
            indicator.style.display = 'flex';
            const label = indicator.querySelector('#queueCountText') || indicator.querySelector('.queue-count');
            const review = queue.filter(item => item.status !== 'pending').length;
            if (label) label.textContent = `${queue.length - review} queued${review ? ` · ${review} to review` : ''}`;
        } else {
            indicator.style.display = 'none';
        }
    }).catch(() => {
        indicator.style.display = 'flex';
        const label = indicator.querySelector('#queueCountText') || indicator.querySelector('.queue-count');
        if (label) label.textContent = 'Unlock to review saved drafts';
    });
}

function showOfflineBanner() {
    let banner = document.getElementById('offlineBanner');
    
    if (!banner) {
        banner = document.createElement('div');
        banner.id = 'offlineBanner';
        banner.className = 'offline-banner';
        banner.setAttribute('role', 'status');
        banner.innerHTML = `
            <div class="offline-banner-content">
                <span>Connection lost</span>
                <button class="offline-retry-btn" data-click="checkConnection">Retry</button>
            </div>
        `;
        
        const chatColumn = document.querySelector('.chat-column');
        if (chatColumn) {
            chatColumn.insertBefore(banner, document.getElementById('messagesContainer'));
        }
    }
    
    banner.style.display = 'block';
}

function hideOfflineBanner() {
    const banner = document.getElementById('offlineBanner');
    if (banner) {
        banner.style.display = 'none';
    }
}

// ============================================================
// PWA Install Prompt
// ============================================================

let deferredInstallPrompt = null;
const installButton = document.getElementById('installBtn');

async function checkInstallAvailability() {
    // Check if already installed (standalone display mode)
    if (window.matchMedia('(display-mode: standalone)').matches ||
        window.navigator.standalone === true) {
        console.log('[PWA] Already installed as app');
        return;
    }
    
    // Listen for beforeinstallprompt event
    window.addEventListener('beforeinstallprompt', (e) => {
        e.preventDefault(); // Prevent default install prompt
        deferredInstallPrompt = e;
        console.log('[PWA] Install prompt available');
        
        // Show install button if it exists
        if (installButton) {
            installButton.style.display = 'inline-flex';
        }
    });
}

async function handleInstallClick() {
    if (!deferredInstallPrompt) {
        console.log('[PWA] No install prompt available');
        return;
    }
    
    // Show the native install prompt
    deferredInstallPrompt.prompt();
    
    // Wait for user response
    const { outcome } = await deferredInstallPrompt.userChoice;
    console.log(`[PWA] Install ${outcome === 'accepted' ? 'accepted' : 'dismissed'}`);
    
    deferredInstallPrompt = null;
    
    if (installButton) {
        installButton.style.display = 'none';
    }
}

if (installButton) {
    installButton.addEventListener('click', handleInstallClick);
}

// ============================================================
// Notification Helper
// ============================================================

function showNotification(message, type = 'info') {
    if (!('Notification' in window)) return;
    
    // Check permission
    if (Notification.permission === 'granted') {
        new Notification('Altera', { body: message, icon: '/static/icons/icon-96.png' });
    } else if (Notification.permission !== 'denied') {
        Notification.requestPermission().then(permission => {
            if (permission === 'granted') {
                new Notification('Altera', { body: message, icon: '/static/icons/icon-96.png' });
            }
        });
    }
}

// ============================================================
// Initialization
// ============================================================

AlteraPage.ready(() => {
    updateOfflineIndicator();
    if (!window.__ALTERA_RECOVERY__) {
        retryQueuedMessages();
        scheduleRetry();
    }
    // Check install availability
    checkInstallAvailability();
    
    // Update offline status periodically (in case of network changes)
    offlinePollTimer = setInterval(() => {
        updateOnlineStatus();
    }, 5000);
});

