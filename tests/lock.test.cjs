// The lock screen and the kids view: a PIN to open the app, Kids to stay with kids pals, and a PIN to leave.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const lockSource = fs.readFileSync('static/js/lock.js', 'utf8');
const kidsSource = fs.readFileSync('static/js/kids-view.js', 'utf8');
const lockTemplate = fs.readFileSync('templates/lock.html', 'utf8');
const indexTemplate = fs.readFileSync('templates/index.html', 'utf8');
const homeTemplate = fs.readFileSync('templates/home.html', 'utf8');
const homeSource = fs.readFileSync('static/js/home.js', 'utf8');
const kidsCss = fs.readFileSync('static/css/kids-view.css', 'utf8');

class Element {
    constructor(tag = 'div', attrs = {}) {
        this.tag = tag; this.children = []; this.listeners = {}; this.attributes = {...attrs}; this.dataset = {};
        this.style = {}; this.textContent = ''; this.value = ''; this.className = attrs.class || '';
        this.hidden = false; this.disabled = false; this.open = false; this.focused = false; this.clicks = 0;
        this.files = [];
        this.classList = {
            toggle: (name, on) => { const names = new Set(this.className.split(' ').filter(Boolean)); on ? names.add(name) : names.delete(name); this.className = [...names].join(' '); },
            contains: name => this.className.split(' ').includes(name),
            add: (...names) => { this.className = [...new Set([...this.className.split(' '), ...names])].join(' '); },
        };
    }
    set innerHTML(_) { throw new Error('Render text, never HTML'); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    querySelector() { return null; }
    focus() { this.focused = true; }
    select() {}
    contains() { return false; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    click() { this.clicks += 1; (this.listeners.click || []).forEach(callback => callback({target: this, preventDefault() {}})); }
    async fire(name, event = {}) { for (const callback of this.listeners[name] || []) await callback({target: this, preventDefault() {}, ...event}); }
}

function documentFor(template, bodyClass = '') {
    const nodes = new Map();
    for (const match of template.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
        const node = new Element(match[1]);
        node.id = match[2];
        node.className = (match[0].match(/class="([^"]*)"/) || [, ''])[1];
        node.hidden = /(^|\s)hidden(\s|=|>)/.test(match[0]);
        nodes.set(node.id, node);
    }
    const body = new Element('body');
    body.className = bodyClass;
    body.classList.add(...bodyClass.split(' ').filter(Boolean));
    const document = {
        body,
        getElementById: id => nodes.get(id) || null,
        createElement: tag => new Element(tag),
        querySelectorAll: () => [],
        addEventListener() {},
    };
    return {document, nodes};
}

function load(source, {document, fetchImpl, visited}) {
    const context = {
        document, console, JSON, Error, Promise,
        window: {location: {assign: url => visited.push(url)}},
        fetch: fetchImpl,
    };
    context.window = context.window;
    vm.runInNewContext(source, context, {filename: 'lock-test.js'});
    return context;
}

const jsonResponse = (status, data) => ({ok: status >= 200 && status < 300, status, json: async () => data});

// ---- the lock screen

test('the lock screen focuses the PIN field and has two ways in', () => {
    const {document, nodes} = documentFor(lockTemplate);
    const visited = [];
    load(lockSource, {document, fetchImpl: async () => jsonResponse(200, {}), visited});
    assert.equal(nodes.get('pinInput').focused, true, 'the PIN is ready to type');
    assert.ok(nodes.get('kidsButton'), 'Kids is offered beside the PIN');
    assert.doesNotMatch(lockTemplate, /href="\/home"/, 'the lock screen is not a link back into the app');
    assert.doesNotMatch(lockTemplate, /href="\/memory"/);
});

test('a wrong PIN is reported and stays on the lock screen', async () => {
    const {document, nodes} = documentFor(lockTemplate);
    const visited = [];
    load(lockSource, {document, fetchImpl: async () => jsonResponse(403, {error: 'That PIN is not right.', code: 'bad_pin'}), visited});
    nodes.get('pinInput').value = '0000';
    await nodes.get('unlockForm').fire('submit');
    assert.match(nodes.get('lockStatus').textContent, /not right/);
    assert.deepEqual(visited, []);
});

test('the right PIN unlocks the whole app', async () => {
    const {document, nodes} = documentFor(lockTemplate);
    const visited = [];
    load(lockSource, {document, fetchImpl: async () => jsonResponse(200, {ok: true}), visited});
    nodes.get('pinInput').value = '1234';
    await nodes.get('unlockForm').fire('submit');
    assert.deepEqual(visited, ['/home']);
});

test('Kids opens the kids view, not the whole app', async () => {
    const {document, nodes} = documentFor(lockTemplate);
    const visited = [];
    let body = null;
    load(lockSource, {document, fetchImpl: async (_url, init) => { body = JSON.parse(init.body); return jsonResponse(200, {ok: true}); }, visited});
    await nodes.get('kidsButton').fire('click');
    assert.deepEqual(body, {});
    assert.deepEqual(visited, ['/']);
});

// ---- the kids view

test('the kids view names the list, shows the badge and offers a way out', () => {
    const {document, nodes} = documentFor(indexTemplate, 'kids-view');
    const visited = [];
    load(kidsSource, {document, fetchImpl: async () => jsonResponse(200, {}), visited});
    assert.equal(nodes.get('sidebarPeopleTitle').textContent, 'Kids pals');
    assert.equal(nodes.get('kidsViewBadge').hidden, false);
    assert.equal(nodes.get('leaveKidsBtn').hidden, false);
    assert.ok(nodes.get('leaveKidsDialog'), 'leaving asks for the PIN');
});

test('outside the kids view the script does nothing', () => {
    const {document, nodes} = documentFor(indexTemplate, '');
    const visited = [];
    load(kidsSource, {document, fetchImpl: async () => jsonResponse(200, {}), visited});
    assert.equal(nodes.get('kidsViewBadge').hidden, true, 'the badge stays hidden on the ordinary chat');
    assert.equal(nodes.get('leaveKidsBtn').hidden, true);
});

test('leaving the kids view needs the PIN and only then opens the app', async () => {
    const {document, nodes} = documentFor(indexTemplate, 'kids-view');
    const visited = [];
    load(kidsSource, {document, fetchImpl: async () => jsonResponse(403, {error: 'That PIN is not right.', code: 'bad_pin'}), visited});
    await nodes.get('leaveKidsBtn').fire('click');
    assert.equal(nodes.get('leaveKidsDialog').open, true, 'the dialog opens and the PIN is focused');
    assert.equal(nodes.get('leaveKidsPin').focused, true);
    nodes.get('leaveKidsPin').value = '0000';
    await nodes.get('leaveKidsForm').fire('submit');
    assert.match(nodes.get('leaveKidsStatus').textContent, /not right/);
    assert.deepEqual(visited, []);

    const second = documentFor(indexTemplate, 'kids-view');
    const okVisited = [];
    load(kidsSource, {document: second.document, fetchImpl: async () => jsonResponse(200, {ok: true}), visited: okVisited});
    second.nodes.get('leaveKidsPin').value = '1234';
    await second.nodes.get('leaveKidsForm').fire('submit');
    assert.deepEqual(okVisited, ['/home']);
});

// ---- the page and the settings that go with it

test('the chat page has a kids-view mode and the styles that go with it', () => {
    assert.match(indexTemplate, /\{% if kids_view %\}kids-view\{% endif %\}/, 'the body carries the mode');
    assert.match(indexTemplate, /id="leaveKidsBtn"/);
    assert.match(indexTemplate, /id="leaveKidsDialog"/);
    assert.match(indexTemplate, /js\/kids-view\.js/);
    assert.match(indexTemplate, /css\/kids-view\.css/);
    assert.match(kidsCss, /body\.kids-view \.settings-btn/);
    assert.match(kidsCss, /body\.kids-view \.bottom-nav/);
    assert.match(kidsCss, /body\.kids-view \.sidebar-nav/);
});

test('the kids dialog sets a PIN with kids mode, and can change it later', () => {
    for (const id of ['kidsPin', 'kidsPinRepeat', 'kidsChangeFields', 'kidsCurrentPin', 'kidsNewPin', 'kidsChangePin']) {
        assert.ok(homeTemplate.includes(`id="${id}"`), id);
    }
    assert.match(homeSource, /\/api\/lock\/pin/);
    assert.match(homeSource, /validPin/);
    assert.match(homeSource, /pin_set/);
});

test('a pal’s kids switch carries the PIN with the change', () => {
    assert.match(homeTemplate, /id="palSettingsKidsPin"/);
    assert.match(homeSource, /body\.pin = \$\('palSettingsKidsPin'\)/);
});