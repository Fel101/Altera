const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function settings(payloads) {
    const source = fs.readFileSync('static/js/app.js', 'utf8');
    const start = source.indexOf("let currentProviderKey =");
    const end = source.indexOf('function onModelChange()', start);
    const elements = new Map(['providerSelect', 'modelSelect', 'modelStatus', 'refreshModelsBtn', 'providerStatus', 'customProviderSection'].map(id => [id, {
        style: {}, value: '', replaceChildren(...items) {this.options = items; this.value = (items.find(item => item.selected) || items[0])?.value || '';},
        set innerHTML(_) {throw new Error('Provider content must never be interpreted as HTML');},
    }]));
    const requested = [];
    const context = vm.createContext({
        console, document: {getElementById: id => elements.get(id)}, AbortController, providerLoadRun:0,modelLoadRun:0,providerRevision:0,
        Option: function(text, value, defaultSelected, selected) {Object.assign(this, {text, value, selected});},
        fetch: async url => {requested.push(url); return {ok: true, json: async () => payloads.shift()};},
    });
    context.window=context;require('./helpers/ordinary-page.cjs')(context);
    vm.runInContext(source.slice(start, end), context);
    vm.runInContext(source.slice(source.indexOf('const MainUI ='),source.indexOf('// Own public main-page actions')),context);
    return {context, elements, requested};
}

test('provider and model strings remain literal option text, including quotes and markup', async () => {
    const provider = '<img src=x onerror=attack()>', model = '"><option onclick="attack()"> & model';
    const state = settings([
        {providers: [{key: 'custom', name: provider, is_active: true}], active_provider: 'custom', active_model: model},
        {models: [{id: 'other-model'}, {id: model}]},
    ]);
    await state.context.loadProviderSettings();
    assert.equal(state.elements.get('providerSelect').options[0].text, provider);
    assert.equal(state.elements.get('modelSelect').options[1].text, model);
    assert.equal(state.elements.get('modelSelect').value, model);
    assert.equal(state.elements.get('modelStatus').textContent, '2 models available');
});

test('an empty model list replaces stale choices and restores the enabled control', async () => {
    const state = settings([{models: []}]);
    await state.context.refreshModels();
    const select = state.elements.get('modelSelect');
    assert.equal(select.options.length, 1);
    assert.equal(select.options[0].text, 'No models found');
    assert.equal(select.value, '');
    assert.equal(select.disabled, false);
});
