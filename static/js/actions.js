// Page actions without inline event handlers, so the pages can have a Content-Security-Policy that refuses inline script
// (docs/ARCHITECTURE.md, "Content-Security-Policy"). An element names the function it calls instead of carrying code:
//
//   <button data-click="toggleSettings">            toggleSettings()
//   <button data-click="setTheme" data-args='["dark"]'>      setTheme('dark')
//   <input data-change="toggleVoiceLayer" data-pass="checked">   toggleVoiceLayer(this.checked)
//   <form data-submit="sendMessage" data-pass="event">           sendMessage(event)
//
// data-args is a JSON array; data-pass adds one more argument after it: element, checked, value or event. The name is a
// global function or a dotted path to one (BackupUI.open); nothing is evaluated. As with inline handlers, every element
// from the target up to the page that names an action for the event runs it, innermost first, until one stops the event.
(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else api.install(root, root.document);
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';
    const ATTRIBUTES = Object.freeze({click: 'data-click', change: 'data-change', submit: 'data-submit'});
    const NAME = /^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*$/;

    function resolve(root, name) {
        if (typeof name !== 'string' || !NAME.test(name)) return null;
        let owner, value = root;
        for (const part of name.split('.')) {
            if (value === null || value === undefined || part === '__proto__' || part === 'constructor' || part === 'prototype') return null;
            owner = value;
            value = value[part];
        }
        return typeof value === 'function' ? {fn: value, self: owner === root ? undefined : owner} : null;
    }

    function argumentsFor(element, event) {
        let args = [];
        const raw = element.getAttribute('data-args');
        if (raw) {
            try {
                const parsed = JSON.parse(raw);
                args = Array.isArray(parsed) ? parsed : [parsed];
            } catch (_) {
                throw new Error(`data-args on an element is not JSON: ${raw}`);
            }
        }
        const pass = element.getAttribute('data-pass');
        if (pass === 'element') args.push(element);
        else if (pass === 'checked') args.push(element.checked);
        else if (pass === 'value') args.push(element.value);
        else if (pass === 'event') args.push(event);
        return args;
    }

    function dispatch(root, event) {
        const attribute = ATTRIBUTES[event.type];
        if (!attribute) return;
        let element = event.target && event.target.nodeType === 1 ? event.target : event.target?.parentElement;
        while (element && !event.cancelBubble) {
            if (typeof element.getAttribute === 'function' && element.hasAttribute?.(attribute)) {
                const name = element.getAttribute(attribute), found = resolve(root, name);
                if (!found) {
                    root.console?.error?.(`No page action named ${name}.`);
                } else {
                    try {
                        const result = found.fn.apply(found.self, argumentsFor(element, event));
                        if (result && typeof result.catch === 'function') result.catch(error => root.console?.error?.(error));
                    } catch (error) {
                        root.console?.error?.(error);
                    }
                }
            }
            element = element.parentElement;
        }
    }

    function install(root, document) {
        for (const type of Object.keys(ATTRIBUTES)) document.addEventListener(type, event => dispatch(root, event));
    }

    return Object.freeze({install, dispatch, resolve, ATTRIBUTES});
});
