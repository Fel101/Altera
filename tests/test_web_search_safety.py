"""Synthetic search controls; no external provider is contacted."""
import json
import logging
from types import SimpleNamespace
from pathlib import Path

import yaml

from core.chat import ChatEngine
from core.web_search import SearchConfig, SearchResult, WebSearcher


def test_engines_keep_their_own_personality_and_search_instructions():
    def personality(name):
        return {'name': name, 'age': 30, 'traits': ['thoughtful'], 'interests': [], 'quirks': [],
                'speech_style': {'emojis': False, 'tone': 'warm', 'formality': 'casual', 'humor': 'gentle'}}
    first = ChatEngine({'web_search': {'enabled': False}}, personality('First'))
    second = ChatEngine({'web_search': {'enabled': True}}, personality('Second'))
    assert first.prompt_builder is not second.prompt_builder
    assert 'You are First' in first.prompt_builder._persona_section.build()
    assert 'You are Second' in second.prompt_builder._persona_section.build()
    assert first.prompt_builder._tools_section.build() == ''
    assert 'web search tool' in second.prompt_builder._tools_section.build()


def test_disabled_search_is_not_advertised_probed_or_executed(monkeypatch):
    engine = ChatEngine({'web_search': {'enabled': False}}, {'name': 'Synthetic', 'traits': []})
    calls = []
    def chat(messages, **kwargs):
        calls.append(kwargs)
        return 'Synthetic reply'
    monkeypatch.setattr(type(engine), 'llm_client', property(lambda _: SimpleNamespace(chat=chat)))
    monkeypatch.setattr(engine.web_searcher, 'search', lambda *a, **kw: calls.append('unexpected search'))
    assert engine._call_llm([], 1) == 'Synthetic reply'
    assert len(calls) == 1 and calls[0]['tools'] is None and calls[0]['tool_executor'] is None
    assert engine._supports_tools is None
    assert json.loads(engine._execute_tool_call('web_search', {'query': 'private synthetic words'})) == {
        'error': 'Web search is turned off.'}
    assert len(calls) == 1


def test_disabled_search_is_not_advertised_during_streaming(monkeypatch):
    engine = ChatEngine({'web_search': {'enabled': False}}, {'name': 'Synthetic', 'traits': []})
    calls, saved = [], []
    def chat(messages, **kwargs):
        calls.append(kwargs)
        return iter(['Synthetic reply'])
    monkeypatch.setattr(type(engine), 'llm_client', property(lambda _: SimpleNamespace(chat=chat)))
    monkeypatch.setattr(engine, '_start_photos', lambda *args: [])
    monkeypatch.setattr(engine, '_persist_reply', lambda session_id, text: saved.append(text))
    assert list(engine._call_llm_stream([], 1)) == ['Synthetic reply', None]
    assert saved == ['Synthetic reply'] and len(calls) == 1
    assert calls[0]['tools'] is None and calls[0]['tool_executor'] is None
    assert engine._supports_tools is None


def test_portable_starter_requires_explicit_search_and_provider_enablement():
    config = yaml.safe_load(Path('config.example.yaml').read_text('utf-8'))['web_search']
    assert config['enabled'] is False
    assert all(provider['enabled'] is False for provider in config['providers'].values())


def test_disabled_search_does_not_probe_even_after_enabled_verdict(monkeypatch):
    engine = ChatEngine({'web_search': {'enabled': True}}, {'name': 'Synthetic', 'traits': []})
    probes = []
    monkeypatch.setattr(engine, '_server_accepts_tools', lambda: probes.append(True) or True)
    assert engine._tools_for_this_backend() is True and probes == [True]
    engine.web_searcher.config.enabled = False
    assert engine._tools_for_this_backend() is False and probes == [True]
    engine.web_searcher.config.enabled = True
    assert engine._tools_for_this_backend() is True and probes == [True]


def test_weather_fallback_honors_duckduckgo_provider_setting(monkeypatch):
    config = SearchConfig(providers={'openweathermap': {'enabled': True, 'api_key': ''},
                                     'duckduckgo': {'enabled': False}})
    searcher = WebSearcher(config)
    calls = []
    monkeypatch.setattr(searcher, '_search_duckduckgo', lambda query, limit: calls.append((query, limit)) or [])
    assert searcher.search('weather in synthetic city', 'openweathermap') == []
    assert calls == []
    config.providers['duckduckgo']['enabled'] = True
    assert searcher.search('weather in synthetic city', 'openweathermap') == []
    assert calls == [('weather weather in synthetic city', 5)]


def test_search_logs_do_not_include_query_or_provider_exception(monkeypatch, caplog):
    secret = 'synthetic-query-and-api-key-do-not-log'
    searcher = WebSearcher(SearchConfig(providers={'duckduckgo': {'enabled': True}}))
    result = SearchResult('Synthetic title', 'https://example.invalid', 'Synthetic snippet', 'duckduckgo')
    monkeypatch.setattr(searcher, '_search_duckduckgo', lambda *args: [result])
    with caplog.at_level(logging.DEBUG, logger='core.web_search'):
        assert searcher.search(secret) == [result]
        assert searcher.search(secret) == [result]
        def fail(*args):
            raise RuntimeError('https://provider.invalid/?q=' + secret + '&key=' + secret)
        monkeypatch.setattr(searcher, '_search_duckduckgo', fail)
        assert searcher.search(secret + '-new') == []
    assert 'cache hit' in caplog.text and 'Search failed' in caplog.text
    assert secret not in caplog.text and 'provider.invalid' not in caplog.text


def test_unknown_or_invalid_provider_does_not_contact_network(monkeypatch):
    searcher = WebSearcher(SearchConfig(providers={'unknown': {'enabled': True}, 'wikipedia': 'invalid'}))
    calls = []
    def unexpected(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('Unexpected network')
    monkeypatch.setattr(searcher._session, 'get', unexpected)
    assert searcher.search('synthetic', 'unknown') == []
    assert searcher.search('synthetic', ['wikipedia']) == []
    assert searcher.search('synthetic', 'wikipedia') == []
    assert calls == []
