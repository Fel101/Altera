"""Regression checks for repeated model deltas and explicit stream completion."""
from types import SimpleNamespace

import pytest

from core.llm import LLMClient, StreamComplete
from test_companion_api import engine, tmp_store, world  # noqa: F401
from test_polish_reliability import decoded, reliable  # noqa: F401


@pytest.mark.parametrize('pieces', [['ha', 'ha', '!'], ['🙂', '🙂', ' yes'], ['a', 'b', 'ab', 'c']])
def test_repeated_text_deltas_are_fully_streamed_and_saved(reliable, engine, world, pieces):
    expected = ''.join(pieces)
    engine.llm.chunks = [*pieces, StreamComplete(expected)]
    events = decoded(reliable.post('/api/chat/stream', json={
        'message': 'Hi', 'request_id': 'repeated-delta-review-123'}))
    assert events[-1]['status'] == 'saved'
    assert ''.join(event.get('response') or '' for event in events) == expected
    assert world.store.get_message(events[-1]['message_id']).content == expected


def test_real_stream_generator_marks_only_the_aggregate():
    pieces = ['ha', 'ha', '!']
    chunks = [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None))])
              for text in pieces]
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: iter(chunks))))
    result = list(LLMClient(model='fake')._chat_stream(sdk, [{'role': 'user', 'content': 'Hi'}]))
    assert result[:-1] == pieces
    assert all(type(delta) is str for delta in result[:-1])
    assert isinstance(result[-1], StreamComplete)
    assert result[-1] == 'haha!'
