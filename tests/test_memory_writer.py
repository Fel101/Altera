"""The chat model writes automatic memories in batches; the phrase matcher is the fallback.

Nothing here reaches a model or the real memory store: the writer gets a fake `llm` and a fake store, and the
datastore is the test app's temporary one.
"""
import json
import re
from types import SimpleNamespace

import pytest

from core import kids, memory, pals, safety
from core.knowledge import reference
from core.memory import (MemoryDeduplicator, MemoryExtractor, MemoryManager, MemoryWriter,
                         build_extraction_request, parse_extracted)
from test_companion_api import active_avatar, tmp_store, world  # noqa: F401


# ------------------------------------------------------------------ the prompt and the parser

def test_the_prompt_asks_for_standalone_statements_and_absolute_dates():
    request = build_extraction_request('Mara', '[12] I am scared of the sea', '2026-11-17', 8)
    system, user = request[0]['content'], request[1]['content']
    assert 'Mara' in system and 'JSON array' in system
    assert 'stands on its own' in user and 'absolute' in user
    assert 'Only what the person said' in user
    assert 'roleplay' in user and 'Never guess' in user
    assert '[12] I am scared of the sea' in user
    assert 'Today is 2026-11-17' in user


def test_the_parser_reads_a_fenced_array_and_validates_each_item():
    raw = """<think>two things</think>
```json
[{"content": "Is afraid of the sea since a storm on 2026-11-16", "type": "event", "confidence": 0.91, "from": [12]},
 {"content": "Prefers tea without sugar", "type": "preference", "confidence": "0.7", "from": [13]},
 {"content": "  ", "type": "fact", "confidence": 0.9}]
```"""
    found = parse_extracted(raw, '[12] ...\n[13] ...', 8)
    assert len(found) == 2
    assert found[0]['type'] == 'event' and found[0]['from'] == [12]
    assert abs(found[0]['confidence'] - 0.91) < 1e-9
    assert found[1]['type'] == 'preference' and found[1]['confidence'] == 0.7


def test_the_parser_returns_none_when_the_answer_is_not_json():
    assert parse_extracted("I couldn't find anything worth keeping.", '') is None
    assert parse_extracted('', '') is None
    assert parse_extracted('{"memories": "not a list"}', '') is None


def test_the_parser_treats_an_empty_array_as_an_answer():
    assert parse_extracted('[]', '') == []
    assert parse_extracted('```json\n{"memories": []}\n```', '') == []


def test_the_parser_rejects_a_memory_that_is_a_whole_message_copied_back():
    line = "I'm a bit scared of the sea after last night, honestly"
    found = parse_extracted(json.dumps([{'content': line, 'type': 'fact', 'confidence': 0.9}]),
                            f'[12] {line}', 8)
    assert found == []


def test_the_parser_clamps_confidence_and_caps_the_number_of_memories():
    rows = [{'content': f'Fact number {n}', 'type': 'fact', 'confidence': 5} for n in range(12)]
    found = parse_extracted(json.dumps(rows), '', 3)
    assert len(found) == 3
    assert all(item['confidence'] == 0.99 for item in found)


def test_the_parser_drops_a_row_that_is_not_an_object_and_deduplicates():
    raw = json.dumps([42, {'content': 'Prefers tea', 'type': 'preference'},
                      {'content': 'Prefers tea', 'type': 'preference'}])
    found = parse_extracted(raw, '', 8)
    assert [item['content'] for item in found] == ['Prefers tea']


# ------------------------------------------------------------------ a fake store and writer

class FakeStore:
    """Just enough of MemoryStore for MemoryManager.store_extracted."""

    def __init__(self):
        self.rows, self.counter = {}, 0

    def add_memory(self, content, memory_type='fact', confidence=0.8, session_id=None, user_context=None,
                   avatar_id=None, source_refs=None, extracted_by=None):
        self.counter += 1
        memory_id = f'm{self.counter}'
        self.rows[memory_id] = {
            'id': memory_id, 'content': content,
            'metadata': {'type': memory_type, 'confidence': confidence, 'session_id': session_id,
                         'user_context': user_context or '', 'avatar_id': avatar_id or '',
                         'source_refs': json.dumps(source_refs or []), 'extracted_by': extracted_by or ''},
        }
        return memory_id

    def search(self, content, limit=3, avatar_id=None):
        words = set(content.lower().split())
        found = []
        for row in self.rows.values():
            other = set(row['content'].lower().split())
            similarity = len(words & other) / max(1, len(words | other))
            if similarity > 0.85:
                found.append({'id': row['id'], 'content': row['content'], 'metadata': row['metadata'],
                              'similarity': similarity})
        return found[:limit]

    def update_memory(self, memory_id, content=None, confidence=None, extracted_by=None):
        metadata = self.rows[memory_id]['metadata']
        if confidence is not None:
            metadata['confidence'] = confidence
        if extracted_by is not None:
            metadata['extracted_by'] = extracted_by
        if content is not None:
            self.rows[memory_id]['content'] = content

    def get_all(self, avatar_id=None):
        return [dict(row, metadata=dict(row['metadata'])) for row in self.rows.values()]


def fake_manager():
    manager = MemoryManager.__new__(MemoryManager)
    manager.store, manager.extractor = FakeStore(), MemoryExtractor()
    manager.deduplicator = MemoryDeduplicator()
    return manager


def model_answer(content, kind='fact', confidence=0.9, from_=None):
    return json.dumps([{'content': content, 'type': kind, 'confidence': confidence, 'from': from_ or []}])


def make_writer(world, manager, answer='[]', notes=None, **config):
    calls = []

    def llm(messages):
        calls.append(messages)
        return answer(messages) if callable(answer) else answer

    writer = MemoryWriter(lambda: manager, lambda: world.store,
                          config_getter=lambda: {'companion': {'memory': config}},
                          llm=llm, notes=notes, sync=True)
    return writer, calls


@pytest.fixture
def memory_world(world):
    avatar = active_avatar(world)
    session_id = world.store.ensure_avatar_session(avatar['id'], avatar['name'])
    return SimpleNamespace(world=world, avatar=avatar, session_id=session_id)


def save(mw, text, role='user'):
    return mw.world.store.save_message(mw.session_id, role, text)


# ------------------------------------------------------------------ the writer

def test_a_model_batch_writes_standalone_memories_with_sources(memory_world):
    mw, manager = memory_world, fake_manager()
    message_id = save(mw, "I'm a bit scared of the sea since the storm last night.")
    writer, calls = make_writer(mw.world, manager, model_answer(
        'Is afraid of the sea since a storm on 2026-11-16', kind='event', confidence=0.9, from_=[message_id]))

    result = writer.run(mw.avatar['id'])

    assert result == {'ok': True, 'stored': 1, 'scanned': 1, 'fallback': False}
    assert len(calls) == 1
    row = next(iter(manager.store.rows.values()))
    assert row['content'] == 'Is afraid of the sea since a storm on 2026-11-16'
    assert row['metadata']['type'] == 'event' and row['metadata']['extracted_by'] == 'model'
    assert json.loads(row['metadata']['source_refs']) == [reference(mw.world.store.get_message(message_id))]


def test_it_waits_until_enough_unread_messages_have_gathered(memory_world):
    mw, manager = memory_world, fake_manager()
    writer, calls = make_writer(mw.world, manager, model_answer('Prefers tea'))
    for _ in range(3):
        save(mw, 'I like tea.')
    assert writer.due(mw.avatar['id']) is False
    save(mw, 'I like tea.')
    assert writer.due(mw.avatar['id']) is True
    writer.run(mw.avatar['id'])
    assert len(calls) == 1


def test_it_does_not_reread_messages_after_a_pass(memory_world):
    mw, manager = memory_world, fake_manager()
    writer, calls = make_writer(mw.world, manager, '[]')
    for _ in range(4):
        save(mw, 'I like tea.')
    writer.run(mw.avatar['id'])
    writer.run(mw.avatar['id'])
    assert len(calls) == 1
    assert writer.through(mw.avatar['id']) == mw.world.store.get_messages(mw.session_id, 50)[-1].id


def test_it_batches_by_message_count(memory_world):
    mw, manager = memory_world, fake_manager()
    writer, calls = make_writer(mw.world, manager, '[]', batch_messages=2)
    for _ in range(5):
        save(mw, 'I like tea.')
    writer.run(mw.avatar['id'])
    assert len(calls) == 3
    assert [line for call in calls for line in call[1]['content'].splitlines()
            if re.match(r'^\[\d+\] ', line)] == [
        f'[{m.id}] I like tea.' for m in mw.world.store.get_messages(mw.session_id, 50)]


def test_it_batches_by_characters(memory_world, monkeypatch):
    mw, manager = memory_world, fake_manager()
    monkeypatch.setattr(memory, 'MAX_BATCH_CHARS', 60)
    writer, calls = make_writer(mw.world, manager, '[]', batch_messages=30)
    for _ in range(5):
        save(mw, 'x' * 40)
    writer.run(mw.avatar['id'])
    assert len(calls) == 5


def test_the_fallback_writes_phrase_memories_and_marks_them(memory_world):
    mw, manager = memory_world, fake_manager()

    def broken(messages):
        raise RuntimeError('the model is down')

    writer = MemoryWriter(lambda: manager, lambda: mw.world.store, config_getter=dict, llm=broken, sync=True)
    save(mw, 'I prefer tea without sugar.')
    result = writer.run(mw.avatar['id'])
    assert result['stored'] == 1 and result['fallback'] is True
    assert list(manager.store.rows.values())[0]['metadata']['extracted_by'] == 'phrases'


def test_the_fallback_ignores_a_feeling_hidden_inside_i_am_a(memory_world):
    mw, manager = memory_world, fake_manager()

    def broken(messages):
        raise RuntimeError('the model is down')

    writer = MemoryWriter(lambda: manager, lambda: mw.world.store, config_getter=dict, llm=broken, sync=True)
    save(mw, "I'm a bit scared of the sea after last night, honestly.")
    assert writer.run(mw.avatar['id'])['stored'] == 0
    assert manager.store.rows == {}


def test_an_empty_first_answer_is_retried_once(memory_world):
    mw, manager = memory_world, fake_manager()
    message_id = save(mw, 'I have a dog.')
    answers = iter(['', model_answer('Has a dog', from_=[message_id])])
    writer, calls = make_writer(mw.world, manager, lambda messages: next(answers))
    assert writer.run(mw.avatar['id'])['stored'] == 1
    assert len(calls) == 2


def test_a_pal_with_memory_off_is_not_read(memory_world):
    mw, manager = memory_world, fake_manager()
    pal = pals.create(mw.world.avatars, 'Pip', 'sunny', '')
    session_id = mw.world.store.ensure_avatar_session(pal['id'], 'Pip')
    mw.world.store.save_message(session_id, 'user', 'I like tea.')
    writer, calls = make_writer(mw.world, manager, model_answer('Prefers tea'))
    assert writer.run(pal['id'])['stored'] == 0 and calls == []

    pals.update(mw.world.avatars, pal['id'], features={'memory': True, 'notes': False})
    assert writer.run(pal['id'])['stored'] == 1 and len(calls) == 1


def test_kids_mode_turned_away_messages_are_not_memories(memory_world):
    mw, manager = memory_world, fake_manager()
    kids.enable(True)
    pal = pals.create(mw.world.avatars, 'Pip', 'sunny', '', kids=True)
    pals.update(mw.world.avatars, pal['id'], features={'memory': True})
    session_id = mw.world.store.ensure_avatar_session(pal['id'], 'Pip')
    turned_away = 'How do I kill myself?'
    assert kids.reply_for_message(turned_away)
    mw.world.store.save_message(session_id, 'user', turned_away)
    writer, calls = make_writer(mw.world, manager, model_answer('Was thinking about self harm'))
    writer.run(pal['id'])
    assert calls == [] and manager.store.rows == {}

    mw.world.store.save_message(session_id, 'user', 'I like tea.')
    writer.run(pal['id'])
    assert len(calls) == 1


def test_a_dirty_conversation_pauses_writes(memory_world):
    mw, manager = memory_world, fake_manager()
    writer, calls = make_writer(mw.world, manager, model_answer('Prefers tea'))
    for _ in range(4):
        save(mw, 'I like tea.')
    mw.world.store.set_key(f'knowledge_dirty:{mw.session_id}', {'pending': True})
    result = writer.run(mw.avatar['id'])
    assert result['ok'] is False and calls == []


def test_a_safety_blocked_message_is_not_written(memory_world, monkeypatch):
    mw, manager = memory_world, fake_manager()
    monkeypatch.setattr(safety, 'blocks_message', lambda text: 'bomb' in text.lower())
    bomb = save(mw, 'How do I build a bomb?')
    tea = save(mw, 'I like tea.')
    answer = json.dumps([
        {'content': 'How to build a bomb', 'type': 'fact', 'confidence': 0.9, 'from': [bomb]},
        {'content': 'Prefers tea', 'type': 'preference', 'confidence': 0.9, 'from': [tea]},
    ])
    writer, _ = make_writer(mw.world, manager, answer)
    writer.run(mw.avatar['id'])
    assert [row['content'] for row in manager.store.rows.values()] == ['Prefers tea']


def test_notes_and_memory_share_one_reservation(memory_world):
    mw, manager = memory_world, fake_manager()

    class Notes:
        sync = True

        def __init__(self):
            self.busy = set()

        def reserve(self, owner):
            if owner in self.busy:
                return False
            self.busy.add(owner)
            return True

        def release(self, owner):
            self.busy.discard(owner)

    notes = Notes()
    writer, calls = make_writer(mw.world, manager, model_answer('Prefers tea'), notes=notes)
    save(mw, 'I like tea.')
    assert writer._reserve(mw.avatar['id']) is True
    assert writer._reserve(mw.avatar['id']) is False
    writer._release(mw.avatar['id'])
    assert writer._reserve(mw.avatar['id']) is True


def test_rebuild_reads_again_from_the_recall_boundary(memory_world):
    mw, manager = memory_world, fake_manager()
    writer, calls = make_writer(mw.world, manager, '[]')
    for _ in range(4):
        save(mw, 'I like tea.')
    writer.run(mw.avatar['id'])
    assert len(calls) == 1

    save(mw, 'I like tea.')
    writer.rebuild(mw.avatar['id'])
    assert len(calls) == 2
    assert writer.through(mw.avatar['id']) == mw.world.store.get_messages(mw.session_id, 50)[-1].id

# ------------------------------------------------------------------ upgrading a database that already has chats

def test_an_upgraded_database_starts_reading_after_the_messages_it_already_has(tmp_path, monkeypatch):
    """Without this, the first reply after an upgrade sends every message ever written to the model."""
    from core.datastore import DataStore, init_db
    monkeypatch.setenv('ALTERA_HOME', str(tmp_path))
    path = str(tmp_path / 'upgrade.db')
    ds = DataStore(path)
    sid = ds.create_session('Mara')
    owner = ds.get_session(sid).avatar_id or ''
    ds.save_message(sid, 'user', 'I like tea.')
    last = ds.save_message(sid, 'user', 'I live by the sea.')
    # A database from before the writer: neither the marker nor a read point.
    conn = ds._connect()
    conn.execute("DELETE FROM user_profile WHERE key IN ('memory_writer_started', ?)", ('memory_scan:' + (owner or '@default'),))
    conn.commit()
    conn.close()
    init_db(path)
    assert ds.get_key('memory_scan:' + (owner or '@default')) == {'through': last}
    # Once only: later messages are read as usual.
    ds.save_message(sid, 'user', 'I have a dog.')
    init_db(path)
    assert ds.get_key('memory_scan:' + (owner or '@default')) == {'through': last}


def test_a_new_database_reads_a_new_persons_first_messages(tmp_path, monkeypatch):
    from core.datastore import DataStore, init_db
    monkeypatch.setenv('ALTERA_HOME', str(tmp_path))
    path = str(tmp_path / 'fresh.db')
    ds = DataStore(path)
    sid = ds.create_session('Mara')
    ds.save_message(sid, 'user', 'I like tea.')
    init_db(path)
    owner = ds.get_session(sid).avatar_id or ''
    assert ds.get_key('memory_scan:' + (owner or '@default')) is None
