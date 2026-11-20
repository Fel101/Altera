const test = require('node:test'), assert = require('node:assert/strict'), fixture = require('./helpers/main-page.cjs');

test('the companion avatar letter is the companion\'s own initial, never the old "H"', () => {
    const f = fixture();
    assert.equal(f.run('companionInitial()'), 'A');                   // no companion yet: A for Altera
    f.run("var Companion = {active: {name: '  juniper '}}");
    assert.equal(f.run('companionInitial()'), 'J');
    assert.match(f.run('msgAvatarHTML(false)'), /<div class="msg-avatar">J<\/div>/);
    assert.match(f.run('msgAvatarHTML(true)'), />Y<\/div>/);
    f.run("Companion.active.name = '<b>'");
    assert.equal(f.run('companionInitial()'), '&lt;');                // escaped, never markup
    f.run("Companion.active.name = 'émile'");
    assert.equal(f.run('companionInitial()'), 'É');
    assert.doesNotMatch(require('node:fs').readFileSync('static/js/app.js', 'utf8'), /msg-avatar[^>]*>H</);
});

test('the typing dots use the companion\'s avatar and stay below the message just sent', async () => {
    const f = fixture();
    const container = f.nodes.get('messagesContainer');
    f.run("var Companion = {active: {name: 'Juniper'}}");
    f.context.appendMessage = role => {
        const node = f.context.document.createElement('div');
        node.id = 'sent-' + role;
        container.appendChild(node);
    };
    f.context.runChatStream = async (payload, {onPrepared}) => { onPrepared(); };
    f.nodes.get('messageInput').value = 'The ridge walk, about 12 km.';
    await f.context.sendMessage({preventDefault() {}});
    const ids = container.children.map(node => node.id);
    assert.deepEqual(ids.slice(-2), ['sent-user', 'typingIndicator']);
    assert.match(container.children.at(-1).html, /<div class="msg-avatar">J<\/div>/);
});
