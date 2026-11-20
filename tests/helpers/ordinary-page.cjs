// Component fixtures get the same page API the browser does: native storage,
// DOM-ready callbacks and components that stay active. Real startup is tested separately.
module.exports = function ordinaryPage(context, {flush} = {}) {
    const components = new Map();
    const component = Object.freeze({active: true, assert() {}, async run(action) { return action(); }});
    context.AlteraStorage = Object.freeze({
        get local() { return context.localStorage; },
        get session() { return context.sessionStorage; }
    });
    context.AlteraPage = Object.freeze({
        ready(callback) { context.document?.addEventListener('DOMContentLoaded', callback); },
        component(name) { components.set(name, component); return component; },
        flush: flush || (async () => {})
    });
    return {components};
};
