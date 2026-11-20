const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const appSource = fs.readFileSync('static/js/app.js', 'utf8');
const source = appSource.slice(appSource.indexOf('async function loadMessages('), appSource.indexOf('\nfunction createMessageHTML('));
function fixture() {
    const requests = [], errors = [], container = {innerHTML: ''};
    const context = vm.createContext({currentSessionId: 1, messageLoadRun: 0, AbortController, AlteraAPI: {epoch: 'one'}, console: {error: (...args) => errors.push(args)},
        document: {getElementById: () => container}, endAutomaticRecordingForContextChange() {}, scrollToBottom() {},
        createMessageHTML: msg => msg.content,
        fetch: url => new Promise(resolve => requests.push({url, finish: rows => resolve({json: async () => rows})}))});
    require('./helpers/ordinary-page.cjs')(context);vm.runInContext(source, context);
    return {context, requests, errors, container, load: sid => context.loadMessages(sid)};
}

test('an accepted-message reload cannot overwrite a newly selected companion', async () => {
    const f = fixture(); const previous = f.load(1);
    f.context.currentSessionId = 2; const current = f.load(2);
    f.requests[1].finish([{content: 'Companion two'}]); await current;
    f.requests[0].finish([{content: 'Old companion one'}]); await previous;
    assert.equal(f.container.innerHTML, 'Companion two');
});

test('unavailable or malformed message responses preserve existing history and report a useful error',async()=>{
    for(const data of [null,{error:'Private service is unavailable'}]){
        const f=fixture();f.container.innerHTML='Existing saved history';
        f.context.fetch=async()=>({ok:false,json:async()=>data});await f.load(1);
        assert.equal(f.container.innerHTML,'Existing saved history');assert.equal(f.errors.length,1);
        assert.ok(!f.errors[0][1].message.includes('map'));
    }
});

test('a later same-session refresh wins even when the older response arrives last', async () => {
    const f = fixture(); const older = f.load(1), newer = f.load(1);
    f.requests[1].finish([{content: 'Newer saved history'}]); await newer;
    f.requests[0].finish([{content: 'Old history'}]); await older;
    assert.equal(f.container.innerHTML, 'Newer saved history');
});

test('a response captured before dataset replacement cannot render afterwards', async () => {
    const f = fixture(); const old = f.load(1); f.context.AlteraAPI.epoch = 'two';
    f.requests[0].finish([{content: 'Old dataset'}]); await old;
    assert.equal(f.container.innerHTML, '');
});
