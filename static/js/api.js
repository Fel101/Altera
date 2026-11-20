// Bind same-origin mutations to the dataset displayed when this page opened.
(() => {
    window.AlteraAPI = Object.freeze({epoch: window.__DATA_EPOCH__ || null});
    const nativeFetch = window.fetch.bind(window);
    window.fetch = (input, init = {}) => {
        const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, window.location.href);
        const method = (init.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
        if (AlteraAPI.epoch && url.origin === window.location.origin && url.pathname.startsWith('/api/')
                && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
            const headers = new Headers(init.headers || (typeof input === 'object' && input.headers) || {});
            headers.set('X-Altera-Dataset', AlteraAPI.epoch);
            init = {...init, headers};
        }
        return nativeFetch(input, init);
    };
})();
