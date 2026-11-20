"""Diagnostics report actual usage, survive replay, and never infer missing cost."""
import json
from types import SimpleNamespace as NS

import pytest
from flask import Flask

from core.datastore import get_datastore
from core.diagnostics import snapshot, summarize
from core.llm import LLMClient
from core.turns import CURRENT_TURN, TurnService


class Stream:
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False
    def __iter__(self):
        return iter(self.chunks)
    def close(self):
        self.closed = True


def chunk(text=None, usage=None, tools=None):
    return NS(usage=usage, choices=[NS(delta=NS(content=text, tool_calls=tools))] if text or tools else [])


@pytest.fixture
def measured(altera_config, monkeypatch):
    store = get_datastore()
    sid = store.create_session('Measured')
    service, app = TurnService(store), Flask(__name__)
    app.secret_key = 'test-only'
    calls, streams = [], []
    responses = [Stream([chunk('Hello '), chunk('there'), chunk(usage=NS(prompt_tokens=17, completion_tokens=3, total_tokens=20, cost=.002))])]
    def create(**kwargs):
        calls.append(kwargs)
        response = responses.pop(0)
        streams.append(response)
        return response
    monkeypatch.setattr('openai.OpenAI', lambda **kwargs: NS(chat=NS(completions=NS(create=create)), close=lambda: None))
    client = LLMClient('https://openrouter.ai/api/v1', 'fixture-secret', 'fixture-model', provider_key='openrouter')
    def produce(session_id):
        text = ''
        for piece in client.chat([{'role': 'user', 'content': 'Private words must not be in diagnostics'}], stream=True):
            if piece != text:
                text += piece
                yield {'response': piece}
        CURRENT_TURN.get().update(assistant_message_id=store.save_message(session_id, 'assistant', text))
    def run(key='measured-reply-001'):
        service.submit(key, sid, 'Private input', False, produce, app)
        list(service.events(key))
        return service.get(key)
    return NS(store=store, sid=sid, service=service, client=client, calls=calls, streams=streams, responses=responses, run=run)


def test_stream_final_empty_choices_usage_is_durable_and_replay_does_not_double_count(measured):
    row = measured.run()
    assert row['status'] == 'saved' and row['first_text_ms'] is not None and row['elapsed_ms'] >= row['first_text_ms']
    data = snapshot(measured.store, measured.sid)
    metrics = data['turns'][0]['metrics']
    assert metrics['total_tokens']['value'] == 20 and metrics['reported_cost_credits']['value'] == .002
    assert metrics['call_count'] == 1 and metrics['input_tokens_estimate'] > 0
    assert all(stream.closed for stream in measured.streams)
    assert 'Private' not in json.dumps(data) and 'fixture-secret' not in json.dumps(data)
    measured.run()
    assert len(measured.calls) == 1 and snapshot(measured.store, measured.sid)['turns'][0]['metrics'] == metrics


def test_interrupted_or_unreported_usage_stays_unknown_not_zero(measured):
    def interrupted():
        yield chunk('Partial')
        raise InterruptedError('Stopped before final usage')
    measured.responses[:] = [Stream(interrupted())]
    row = measured.run()
    assert row['status'] == 'interrupted'
    metrics = snapshot(measured.store, measured.sid)['turns'][0]['metrics']
    assert metrics['total_tokens']['value'] is None and metrics['reported_cost_credits']['value'] is None
    assert metrics['visible_output_tokens_estimate'] > 0


def test_multiple_tool_rounds_accumulate_only_reported_usage(measured):
    tool = NS(index=0, id='call_one', function=NS(name='lookup', arguments='{}'))
    measured.responses[:] = [Stream([chunk(tools=[tool]), chunk(usage=NS(prompt_tokens=10, completion_tokens=1, total_tokens=11))]), Stream([chunk('Answer')])]
    def create(**kwargs):
        return measured.responses.pop(0)
    fake = NS(chat=NS(completions=NS(create=create)))
    turn = NS(metrics={})
    token = CURRENT_TURN.set(turn)
    try:
        # Direct stream keeps cancellation on the real turn out of this focused tool-round test.
        turn.check_cancelled = lambda: None
        list(measured.client._chat_stream(fake, [{'role': 'user', 'content': 'question'}], tools=[{}], tool_executor=lambda *a: 'found'))
    finally:
        CURRENT_TURN.reset(token)
    result = summarize(turn.metrics)
    assert result['call_count'] == 2
    assert result['total_tokens'] == {'value': 11, 'reported_calls': 1, 'total_calls': 2}
    assert result['reported_cost_credits']['value'] is None


def test_local_request_keeps_compatibility_and_openai_requests_usage(measured):
    from core.diagnostics import begin_call, record_usage
    measured.client.base_url = 'http://127.0.0.1:1234/v1'
    measured.run()
    assert 'stream_options' not in measured.calls[0]
    measured.client.base_url = 'https://api.openai.com/v1'
    measured.responses[:] = [Stream([chunk('Hi')])]
    measured.run('measured-reply-002')
    assert measured.calls[1]['stream_options'] == {'include_usage': True}
    turn = NS(metrics={})
    token = CURRENT_TURN.set(turn)
    try:
        record = begin_call(measured.client, [])
        record_usage(record, {'cost': 99, 'prompt_tokens': True, 'completion_tokens': -2})
    finally:
        CURRENT_TURN.reset(token)
    assert 'reported_cost_credits' not in record and 'prompt_tokens' not in record and 'completion_tokens' not in record


def test_diagnostics_scope_and_malformed_restored_metrics(measured):
    from test_blueprints import create_app
    measured.run()
    other = measured.store.create_session('Another companion')
    assert snapshot(measured.store, other)['turns'] == []
    with measured.store._connect() as conn:
        conn.execute('UPDATE chat_turns SET diagnostics=?', (json.dumps({'calls': ['invalid', {'cost': 'secret', 'total_tokens': 'oops'}], 'secret': 'private'}),))
    client = create_app('system').test_client()
    assert client.get('/api/diagnostics').status_code == 400
    assert client.get('/api/diagnostics?session_id=99999').status_code == 404
    response = client.get(f'/api/diagnostics?session_id={measured.sid}')
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert 'private' not in response.text and response.json['turns'][0]['metrics']['total_tokens']['value'] is None
