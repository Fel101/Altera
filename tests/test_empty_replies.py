"""A completion without visible text must not become a saved empty reply."""
from types import SimpleNamespace

import pytest

from core.llm import LLMClient, StreamComplete
from core.turns import EMPTY_REPLY
from test_polish_reliability import decoded

pytest_plugins = ('test_companion_api', 'test_polish_reliability')


@pytest.mark.parametrize('content, expected', [(None, ''), ('Visible answer', 'Visible answer')])
def test_nonstream_model_does_not_substitute_reasoning_for_answer(content, expected):
    message = SimpleNamespace(content=content, reasoning_content='Private reasoning', tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: response)))
    assert LLMClient(model='fake')._chat_once_or_with_tools(sdk, []) == expected


def test_stream_model_does_not_expose_reasoning_delta():
    delta = SimpleNamespace(content=None, reasoning_content='Private reasoning', tool_calls=None)
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **_kwargs: iter([SimpleNamespace(choices=[SimpleNamespace(delta=delta)])]))))
    result = list(LLMClient(model='fake')._chat_stream(sdk, []))
    assert len(result) == 1 and isinstance(result[0], StreamComplete) and result[0] == ''


@pytest.mark.parametrize('chunks', [[], [' ', '\n\t'], ['[[photo: an unfinished scene']])
def test_empty_stream_never_saves_an_assistant_message(reliable, engine, world, chunks):
    engine.llm.chunks = chunks
    result = decoded(reliable.post('/api/chat/stream', json={'message': 'Hello', 'request_id': 'empty-stream-request-123'}))
    assert result[-1]['status'] == 'failed' and result[-1]['message_id'] is None
    assert [message.role for message in world.store.get_messages(result[-1]['session_id'])] == ['user']
    assert not any(event.get('status') == 'saved' for event in result)


def test_empty_regeneration_does_not_replace_the_existing_answer(reliable, engine, world):
    first = decoded(reliable.post('/api/chat/stream', json={'message': 'Hello', 'request_id': 'original-request-123'}))[-1]
    engine.llm.chunks = []
    result = decoded(reliable.post('/api/chat/regenerate', json={
        'session_id': first['session_id'], 'message_id': first['message_id'], 'request_id': 'empty-replacement-123'}))
    assert result[-1]['status'] == 'failed'
    assert world.store.get_message(first['message_id']).content == 'Hello!'


def test_empty_nonstream_completion_keeps_only_the_user_message(engine, world):
    session = world.store.create_session('synthetic')
    engine.llm.reply = '  \n'
    with pytest.raises(RuntimeError, match='empty reply, twice'):
        engine.send_message('Hello', session, stream=False)
    assert [message.role for message in world.store.get_messages(session)] == ['user']


def test_picture_only_reply_remains_a_valid_saved_message(reliable, engine, world, monkeypatch):
    engine.llm.chunks = ['[[photo: synthetic scene]]']
    monkeypatch.setattr(engine, '_start_photos', lambda *_args: [{'id': 'p-0123456789'}])
    result = decoded(reliable.post('/api/chat/stream', json={'message': 'Hello', 'request_id': 'picture-only-request-123'}))
    assert result[-1]['status'] == 'saved'
    assert world.store.get_message(result[-1]['message_id']).content.strip() == '[[photo:p-0123456789]]'


# ---------------------------------------------------------------- asked once more (a reasoning model now and then says nothing)

def answers(engine, *replies):
    """The model answers each call with the next of `replies` (a list of stream chunks, or a string when not streaming)."""
    left = list(replies)

    def chat(messages, stream=False, tools=None, tool_executor=None):
        engine.llm.calls.append(messages)
        reply = left.pop(0)
        return iter(reply) if stream else reply
    engine.llm.chat = chat


def test_an_empty_stream_is_asked_for_once_more_and_the_second_answer_is_saved(reliable, engine, world):
    answers(engine, [], ['Here ', 'I am.'])
    result = decoded(reliable.post('/api/chat/stream', json={'message': 'Hello', 'request_id': 'empty-then-fine-123'}))
    assert result[-1]['status'] == 'saved' and len(engine.llm.calls) == 2
    assert ''.join(event.get('response') or '' for event in result if event.get('status') != 'saved') == 'Here I am.'
    assert [m.content for m in world.store.get_messages(result[-1]['session_id'])] == ['Hello', 'Here I am.']


def test_empty_twice_fails_saying_so_and_is_not_asked_a_third_time(reliable, engine, world):
    answers(engine, [' '], [])
    result = decoded(reliable.post('/api/chat/stream', json={'message': 'Hello', 'request_id': 'empty-twice-request-123'}))
    assert result[-1]['status'] == 'failed' and len(engine.llm.calls) == 2
    assert reliable.get('/api/chat/requests/empty-twice-request-123').json['error'] == EMPTY_REPLY
    assert [message.role for message in world.store.get_messages(result[-1]['session_id'])] == ['user']


def test_another_failure_keeps_the_general_message(reliable, engine):
    def broken(messages, stream=False, tools=None, tool_executor=None):
        raise RuntimeError('synthetic outage')
    engine.llm.chat = broken
    decoded(reliable.post('/api/chat/stream', json={'message': 'Hello', 'request_id': 'broken-request-1234'}))
    assert reliable.get('/api/chat/requests/broken-request-1234').json['error'] == (
        'The reply could not be completed. Your message is saved.')


def test_a_text_reply_is_never_asked_for_twice(reliable, engine):
    answers(engine, ['Hi!'], ['never asked'])
    decoded(reliable.post('/api/chat/stream', json={'message': 'Hello', 'request_id': 'one-call-request-123'}))
    assert len(engine.llm.calls) == 1


def test_an_empty_nonstream_completion_is_asked_for_once_more(engine, world):
    session = world.store.create_session('synthetic')
    answers(engine, '', 'Second time lucky.')
    assert engine.send_message('Hello', session, stream=False) == 'Second time lucky.'
    assert [m.content for m in world.store.get_messages(session)] == ['Hello', 'Second time lucky.']
