const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {create, ACCENTS} = require('../static/js/appearance.js');

function node(tag = 'div', className = '') {
    const attributes = {}, classes = new Set(className.split(' ').filter(Boolean)), properties = {};
    return {tag, children: [], dataset: {}, style: {background: '', setProperty(name, value) { properties[name] = value; }, properties},
        hidden: false, title: '', textContent: '', type: '',
        get className() { return [...classes].join(' '); }, set className(value) { classes.clear(); value.split(' ').filter(Boolean).forEach(c => classes.add(c)); },
        classList: {add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
            toggle(c, on = !classes.has(c)) { on ? classes.add(c) : classes.delete(c); return on; }},
        setAttribute(name, value) { attributes[name] = String(value); }, getAttribute: name => attributes[name] ?? null,
        removeAttribute(name) { delete attributes[name]; }, replaceChildren(...children) { this.children = children; }};
}

function page(saved = {}) {
    const html = node('html'), themes = ['dark', 'light'].map(mode => node('button', `theme-option ${mode}-mode`));
    const nodes = {accentColors: node(), discreetBtn: node('button'), moodBadge: node('span')};
    const values = new Map(Object.entries(saved));
    const document = {documentElement: html, createElement: tag => node(tag), getElementById: id => nodes[id] || null,
        querySelectorAll(selector) {
            if (selector === '.theme-option') return themes;
            if (selector === '.accent-option') return nodes.accentColors.children;
            return [];
        },
        querySelector: selector => themes.find(t => selector === `.theme-option.${t.className.split(' ')[1]}`) || null};
    const storage = {getItem: key => (values.has(key) ? values.get(key) : null), setItem: (key, value) => values.set(key, value)};
    return {appearance: create({document, storage}), html, themes, nodes, values};
}

test('the chat page loads appearance.js before app.js, and app.js keeps the names the buttons call', () => {
    const index = fs.readFileSync('templates/index.html', 'utf8'), app = fs.readFileSync('static/js/app.js', 'utf8');
    assert.ok(index.indexOf('js/appearance.js') > 0 && index.indexOf('js/appearance.js') < index.indexOf('js/app.js'));
    for (const name of ['setTheme', 'setAccentColor', 'setCompanionMood', 'toggleDiscreet', 'currentMood']) assert.match(app, new RegExp(`function ${name}\\(`));
});

test('restoring brings back the saved theme, accent and discreet mode, and draws the accent buttons', () => {
    const f = page({altera_theme: 'light', altera_accent: '#34d399', altera_discreet: '1'});
    f.appearance.restore();
    assert.equal(f.html.getAttribute('data-theme'), 'light');
    assert.equal(f.themes[1].getAttribute('aria-pressed'), 'true');
    assert.equal(f.html.style.properties['--user-accent'], '#34d399');
    assert.equal(f.html.style.properties['--user-accent-glow'], 'rgba(52, 211, 153, 0.28)');
    const buttons = f.nodes.accentColors.children;
    assert.equal(buttons.length, ACCENTS.length);
    const green = buttons.find(b => b.getAttribute('data-args') === '["#34d399"]');
    assert.equal(green.getAttribute('aria-pressed'), 'true');
    assert.equal(green.getAttribute('data-click'), 'setAccentColor');
    assert.equal(f.html.classList.contains('discreet'), true);
    assert.equal(f.nodes.discreetBtn.getAttribute('aria-pressed'), 'true');
});

test('a saved accent that is not a colour is ignored rather than written into the page', () => {
    const f = page({altera_accent: 'red; background: url(x)'});
    f.appearance.restore();
    assert.equal(f.html.style.properties['--user-accent'], undefined);
    assert.equal(f.nodes.accentColors.children[0].getAttribute('aria-pressed'), 'true');   // the default is shown
    f.appearance.setAccent('nope', null);
    assert.equal(f.values.get('altera_accent'), 'red; background: url(x)');                   // nor saved
});

test('dark is the page without a data-theme; the choice is saved and its button pressed', () => {
    const f = page();
    f.appearance.setTheme('light');
    assert.equal(f.html.getAttribute('data-theme'), 'light');
    f.appearance.setTheme('dark');
    assert.equal(f.html.getAttribute('data-theme'), null);
    assert.equal(f.values.get('altera_theme'), 'dark');
    assert.deepEqual(f.themes.map(t => t.getAttribute('aria-pressed')), ['true', 'false']);
});

test('discreet mode hides the adult-mode colours without forgetting the level, and closing the page resets both', () => {
    const f = page();
    f.appearance.setMood('romantic', 'Romantic');
    assert.equal(f.html.dataset.mood, 'romantic');
    assert.equal(f.nodes.moodBadge.hidden, false);
    assert.equal(f.nodes.moodBadge.textContent, 'Romantic');
    f.appearance.toggleDiscreet();
    assert.equal(f.html.dataset.mood, 'off');
    assert.equal(f.appearance.mood(), 'romantic');                                        // living.js still knows
    assert.equal(f.values.get('altera_discreet'), '1');
    f.appearance.toggleDiscreet(false);
    assert.equal(f.html.dataset.mood, 'romantic');
    f.appearance.reset();
    assert.equal(f.appearance.mood(), 'off');
    assert.equal(f.html.dataset.mood, 'off');
});
