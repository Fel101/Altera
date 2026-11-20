// The sidebar's Add someone menu, the labels on its rows, and the navigation that replaced Timeline with Home.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

const source = fs.readFileSync('static/js/companions.js', 'utf8');
const template = fs.readFileSync('templates/index.html', 'utf8');

// The menu functions are small and pure enough to lift out and run without the whole module's DOM.
const menuSource = source
    .slice(source.indexOf('// The sidebar\'s Add someone button'), source.indexOf('async function importSidebarCard'));

function load() {
    const nodes = {};
    const element = () => ({hidden: false, attributes: {}, style: {}, focused: false,
        setAttribute(name, value) { this.attributes[name] = String(value); },
        focus() { this.focused = true; },
        querySelector() { return null; }});
    nodes.addPersonMenu = element(); nodes.addPersonButton = element();
    nodes.addPersonMenu.hidden = true;
    const visited = [];
    const window = {location: {assign: url => visited.push(url)}};
    const byId = id => nodes[id] || null;
    const api = new Function('byId', 'closeSidebarOnMobile', 'openMeetModal', 'window',
        menuSource + '\nreturn {openAddPersonMenu, closeAddPersonMenu, addPersonFromMenu};')
        (byId, () => {}, () => {}, window);
    return {api, nodes, visited};
}

test('the add-person menu opens, sets aria-expanded, and closes again', () => {
    const {api, nodes} = load();
    api.openAddPersonMenu();
    assert.equal(nodes.addPersonMenu.hidden, false, 'the menu is open');
    assert.equal(nodes.addPersonButton.attributes['aria-expanded'], 'true');
    api.closeAddPersonMenu();
    assert.equal(nodes.addPersonMenu.hidden, true, 'the menu is closed');
    assert.equal(nodes.addPersonButton.attributes['aria-expanded'], 'false');
});

test('each add-person choice goes where Home would send it', () => {
    const {api, visited} = load();
    api.addPersonFromMenu('pal');
    assert.deepEqual(visited, ['/home?make=pal'], 'a pal opens the make-a-pal dialog on Home');
    api.addPersonFromMenu('companion');
    assert.deepEqual(visited, ['/home?make=pal', '/home?make=companion'], 'a companion opens the write dialog on Home');
    api.addPersonFromMenu('meet');
    assert.deepEqual(visited, ['/home?make=pal', '/home?make=companion'], 'Meet someone new opens here, not a page change');
});

test('the list is named for what it holds and its rows carry kind and kids labels', () => {
    assert.match(source, /Kids mode/, 'a kids pal is labelled in the sidebar');
    assert.match(source, /PERSON_KIND_LABELS/, 'a pal or character is named as one');
    assert.match(source, /c\.kids === true/, 'the kids badge comes from the record');
    assert.match(template, />People</, 'the list is named for what it holds, not "Your companions"');
});

test('the template offers more than Meet someone new and drops the legacy pages from the sidebar', () => {
    assert.doesNotMatch(template, /Your companions/, 'the heading names what the list holds');
    assert.match(template, /Add someone/, 'the add button offers more than one kind');
    assert.match(template, /Make a pal/, 'a pal is one of the choices');
    assert.match(template, /Import a character card/, 'a character card is one of the choices');
    assert.match(template, /Write a companion/, 'a companion written here is one of the choices');
    assert.match(template, /Meet someone new in NPC Forge/, 'meeting someone new is still offered');
    assert.doesNotMatch(template, /href="\/timeline" class="sidebar-link"/, 'Timeline is out of the sidebar');
    assert.doesNotMatch(template, /href="\/knowledge" class="sidebar-link"/, 'Knowledge is out of the sidebar');
});

test('the phone bar has Home where Timeline used to be', () => {
    assert.match(template, /href="\/home" class="bottom-nav-item"/, 'Home is in the phone bar');
    assert.doesNotMatch(template, /href="\/timeline" class="bottom-nav-item"/, 'Timeline is out of the phone bar');
});