// Ordinary browser storage and readiness for Altera's page components.
(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else {
        root.AlteraPage = api.createAlteraPage({window: root, document: root.document});
        root.AlteraStorage = root.AlteraPage.storage;
        root.adoptLegacyDatabase = api.adoptLegacyDatabase;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';
    // Altera was called Her, and its browser settings were saved as her_<name>. Move each one to altera_<name> once,
    // unless the new key already exists. Only these names are touched; nothing else in storage is read.
    const LEGACY_KEYS = ['theme', 'accent', 'mood', 'discreet', 'room', 'dismissed_hints', 'backchannel',
        'composer_draft', 'restored_drafts', 'pending_turns', 'restore_receipt'];
    function migrateLegacyKeys(window) {
        for (const scope of ['localStorage', 'sessionStorage']) {
            try {
                const store = window[scope];
                for (const name of LEGACY_KEYS) {
                    const old = store.getItem('her_' + name);
                    if (old === null) continue;
                    if (store.getItem('altera_' + name) === null) store.setItem('altera_' + name, old);
                    store.removeItem('her_' + name);
                }
            } catch (_) { /* Storage can be switched off; the defaults apply then. */ }
        }
    }

    // The same for a browser database with an old her- name: copy whatever is left in it into the new database `db`,
    // then delete it. Rows are added, never overwritten; `renumber` drops an auto-increment key so the new database
    // numbers the row itself. If anything fails, the old database stays for the next try.
    const settle = r => new Promise((resolve, reject) => {
        r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error);
    });
    async function adoptLegacyDatabase(indexedDB, legacyName, db, storeName, {renumber = false} = {}) {
        if (typeof indexedDB?.databases !== 'function') return;
        if (!(await indexedDB.databases()).some(item => item.name === legacyName)) return;
        const opening = indexedDB.open(legacyName);
        opening.onupgradeneeded = () => opening.transaction.abort(); // Gone in the meantime: do not create it.
        const old = await settle(opening);
        try {
            if (old.objectStoreNames.contains(storeName)) {
                const rows = await settle(old.transaction(storeName, 'readonly').objectStore(storeName).getAll());
                await new Promise((resolve, reject) => {
                    const tx = db.transaction(storeName, 'readwrite'), store = tx.objectStore(storeName);
                    for (const row of rows || []) {
                        const copy = {...row};
                        if (renumber) delete copy[store.keyPath];
                        store.add(copy).onerror = event => event.preventDefault(); // Keep the row already there.
                    }
                    tx.oncomplete = resolve; tx.onerror = tx.onabort = () => reject(tx.error);
                });
            }
        } finally { old.close(); }
        indexedDB.deleteDatabase(legacyName);
    }

    function createAlteraPage({window, document}) {
        migrateLegacyKeys(window);
        const ready = new Promise(resolve => {
            if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', resolve, {once: true});
            else resolve();
        });
        const storage = Object.freeze(Object.fromEntries(['local', 'session'].map(scope => [scope, Object.freeze({
            getItem: name => window[scope + 'Storage'].getItem(name),
            setItem: (name, value) => window[scope + 'Storage'].setItem(name, value),
            removeItem: name => window[scope + 'Storage'].removeItem(name)
        })])));
        const component = Object.freeze({active: true, assert() {}, async run(action) { return action(); }});
        return Object.freeze({
            storage,
            ready(callback) {
                void ready.then(callback).catch(() => window.console?.error('Altera page initialization did not complete.'));
            },
            async flush() {},
            component: () => component
        });
    }
    return Object.freeze({createAlteraPage, adoptLegacyDatabase});
});
