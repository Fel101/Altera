const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {create} = require('../static/js/reply-settings.js');

const template = fs.readFileSync('templates/index.html', 'utf8');
const IDS = ['replySettingsForm', 'replyTemperature', 'replyLength', 'replyMore', 'replyTopP', 'replyMinP', 'replyRepetition',
    'replyPresence', 'replyFrequency', 'replyNotSent', 'replySave', 'replyStatus'];
const SETTINGS = {temperature: 0.85, max_tokens: 1024, sampling: {}, provider: 'lmstudio', not_sent: [], limits: {}};

function fixture({answer = SETTINGS, status = 200} = {}) {
    const nodes = Object.fromEntries(IDS.map(id => [id, {id, value: '', textContent: '', hidden: false, disabled: false,
        open: false, listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; }}]));
    const calls = [];
    const request = async (url, init = {}) => {
        calls.push({url, method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : undefined});
        const sent = init.body ? JSON.parse(init.body) : {};
        if (init.method === 'PUT' && status < 400) {
            const sampling = {};
            for (const [key, value] of Object.entries(sent)) if (!['temperature', 'max_tokens'].includes(key) && value !== null) sampling[key] = value;
            answer = {...answer, temperature: sent.temperature, max_tokens: sent.max_tokens, sampling};
        }
        return {ok: init.method !== 'PUT' || status < 400, status: init.method === 'PUT' ? status : 200,
            json: async () => (init.method === 'PUT' && status >= 400 ? {error: 'Top P must be from 0.01 to 1.'} : answer)};
    };
    const controller = create({document: {getElementById: id => nodes[id]}, request});
    return {nodes, calls, controller};
}

test('the Settings panel has every element the script reads, and loads the script', () => {
    for (const id of IDS) assert.match(template, new RegExp(`id="${id}"`), id);
    assert.match(template, /js\/reply-settings\.js/);
});

test('it shows the app-wide temperature and reply length, with every optional setting empty and closed', async () => {
    const {nodes, controller} = fixture();
    await controller.start();
    assert.equal(nodes.replyTemperature.value, 0.85);
    assert.equal(nodes.replyLength.value, 1024);
    assert.equal(nodes.replyTopP.value, '');
    assert.equal(nodes.replyMore.open, false);
    assert.equal(nodes.replyNotSent.hidden, true);
});

test('a set optional setting keeps "More" open, and a provider that refuses some says so', async () => {
    const {nodes, controller} = fixture({answer: {...SETTINGS, provider: 'openai', sampling: {min_p: 0.05},
        not_sent: ['min_p', 'repetition_penalty']}});
    await controller.start();
    assert.equal(nodes.replyMinP.value, 0.05);
    assert.equal(nodes.replyMore.open, true);
    assert.equal(nodes.replyNotSent.hidden, false);
    assert.equal(nodes.replyNotSent.textContent, 'Min P and Repetition penalty are not sent to this provider: its API refuses them.');
});

test('saving sends numbers, and an empty optional setting as null so it is not sent at all', async () => {
    const {nodes, calls, controller} = fixture();
    await controller.start();
    nodes.replyTemperature.value = '1.2';
    nodes.replyTopP.value = '0.9';
    await nodes.replySettingsForm.listeners.submit({preventDefault() {}});
    const put = calls.find(call => call.method === 'PUT');
    assert.deepEqual(put.body, {temperature: 1.2, max_tokens: 1024, top_p: 0.9, min_p: null, repetition_penalty: null,
        presence_penalty: null, frequency_penalty: null});
    assert.equal(nodes.replyStatus.textContent, 'Saved. The next reply uses these settings.');
    assert.equal(nodes.replySave.disabled, false);
});

test('a blank temperature or a word is caught before anything is sent', async () => {
    const {nodes, calls, controller} = fixture();
    await controller.start();
    nodes.replyTemperature.value = '';
    await controller.save();
    assert.equal(nodes.replyStatus.textContent, 'Give a temperature.');
    nodes.replyTemperature.value = '0.7';
    nodes.replyPresence.value = 'lots';
    await controller.save();
    assert.equal(nodes.replyStatus.textContent, 'Presence penalty must be a number.');
    assert.ok(!calls.some(call => call.method === 'PUT'));
});

test('the server saying no is shown as it said it, and nothing on the form is lost', async () => {
    const {nodes, controller} = fixture({status: 400});
    await controller.start();
    nodes.replyTopP.value = '5';
    await controller.save();
    assert.equal(nodes.replyStatus.textContent, 'Top P must be from 0.01 to 1.');
    assert.equal(nodes.replyTopP.value, '5');
    assert.equal(nodes.replySave.disabled, false);
});
