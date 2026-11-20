const test = require('node:test');
const assert = require('node:assert/strict');
const mainPage = require('./helpers/main-page.cjs');

test('missing local recognition assets are visible without disabling barge-in', () => {
    const page = mainPage();
    page.run("voiceEnabled = true; updateVoiceStatus({tts_enabled: true, voice_state: 'ready', voice_name: 'Synthetic', stt_model_state: 'unavailable'})");
    assert.match(page.nodes.get('voiceStatus').textContent, /recognition model is missing or incomplete/);
    assert.equal(page.nodes.get('micBtn').disabled, false);
    page.run("updateVoiceStatus({tts_enabled: true, voice_state: 'ready', voice_name: 'Synthetic', stt_model_state: 'cached'})");
    assert.doesNotMatch(page.nodes.get('voiceStatus').textContent, /missing or incomplete/);
});
