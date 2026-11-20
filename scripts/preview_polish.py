"""Isolated UI preview: temporary data, fake replies, and no model/network clients.

Run from the repo with its Python environment. This never starts/restarts Altera's
production service. Stop this preview with Ctrl+C.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import yaml
    import dotenv
    import openai
    import requests
    from werkzeug.serving import make_server

    def no_network(*args, **kwargs):
        raise RuntimeError("Network/model calls are disabled in the isolated preview")

    openai.OpenAI = no_network
    requests.get = no_network
    dotenv.load_dotenv = lambda *a, **kw: False
    audit = ROOT / 'data' / 'polish-qa'
    audit.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='altera-polish-qa-') as folder:
        sandbox = Path(folder)
        os.environ['ALTERA_HOME'] = str(sandbox)
        os.environ['ALTERA_DISABLE_AUTOSTART'] = '1'
        os.chdir(sandbox)
        from core import config, datastore, profile, companion, chat, memory
        settings = {'app': {'host': '127.0.0.1', 'port': 0, 'debug': False},
                    'llm': {'provider': 'lmstudio', 'model': 'preview-fake', 'max_tokens': 1024},
                    'memory': {'top_k': 5}, 'voice': {'tts_enabled': False}, 'imagegen': {'enabled': False},
                    'tools': {'enabled': False}, 'phase4': {'proactive_enabled': False},
                    'companion': {'notes': {'context_tokens': 8192}}}
        config.CONFIG_PATH = sandbox / 'config.yaml'
        config.ROOT = sandbox
        companion.ROOT = sandbox
        config.LOCAL_CONFIG_PATH = sandbox / 'config.local.yaml'
        config.PERSONALITY_PATH = sandbox / 'personality.yaml'
        config.SECRET_KEY_PATH = sandbox / 'data' / 'secret_key'
        config.CONFIG_PATH.write_text(yaml.safe_dump(settings), encoding='utf-8')
        config.PERSONALITY_PATH.write_text((ROOT / 'personality.yaml').read_text('utf-8'), encoding='utf-8')
        config.reset_cache()
        profile.PROFILE_PATH = str(sandbox / 'user_profile.yaml')
        store = datastore.DataStore(str(sandbox / 'data' / 'conversations.db'))
        datastore._datastore_instance = store
        store.set_key('onboarding_complete', {'value': 'true'})
        sid = store.create_session('Isolated preview')
        store.save_message(sid, 'user', 'A sample message for testing the chat controls.')
        store.save_message(sid, 'assistant', 'This is an isolated preview. You can copy, edit, delete or regenerate these sample messages.')
        companion.set_services(companion.build_services(settings, root=sandbox))
        from copy import deepcopy
        class PreviewCollection:
            def __init__(self):
                self.rows = {}
            def add(self, ids, documents, metadatas):
                for item, content, metadata in zip(ids, documents, metadatas):
                    self.rows[item] = (content, deepcopy(metadata))
            def get(self, ids=None, where=None, **kwargs):
                selected = [i for i in (ids if ids is not None else self.rows) if i in self.rows and
                            (not where or all(self.rows[i][1].get(k) == v for k, v in where.items()))]
                return {'ids': selected, 'documents': [self.rows[i][0] for i in selected], 'metadatas': [deepcopy(self.rows[i][1]) for i in selected]}
            def update(self, ids, metadatas=None, documents=None):
                for n, item in enumerate(ids):
                    old, metadata = self.rows[item]
                    self.rows[item] = (documents[n] if documents else old, {**metadata, **(metadatas[n] if metadatas else {})})
            def delete(self, ids):
                for item in ids:
                    self.rows.pop(item, None)
            def query(self, query_texts, n_results, where=None):
                data = self.get(where=where)
                selected = data['ids'][:n_results]
                return {'ids': [selected], 'documents': [[self.rows[i][0] for i in selected]],
                        'metadatas': [[deepcopy(self.rows[i][1]) for i in selected]], 'distances': [[.1] * len(selected)]}
            def count(self):
                return len(self.rows)
        class PreviewVectors(memory.MemoryStore):
            def __init__(self):
                self._collection, self._client = PreviewCollection(), None
                self.persist_directory = str(sandbox / 'data/chroma_db')
            def snapshot(self):
                return {'version': 1, 'embedding_function': 'preview', 'records': [
                    {'id': i, 'content': text, 'metadata': deepcopy(metadata), 'embedding': [1., 0., 0.]}
                    for i, (text, metadata) in self.collection.rows.items()]}
            def restore_snapshot(self, snapshot):
                self.collection.rows = {r['id']: (r['content'], deepcopy(r['metadata'] or {})) for r in snapshot['records']}
        fake_memory = memory.MemoryManager()
        fake_memory.store = PreviewVectors()
        memory.memory_manager = fake_memory
        memory.get_memory_manager = lambda: fake_memory
        from core import notes
        from core.knowledge import reference
        for avatar_id, name, fact in [('mara-preview-123456', 'Mara', 'I prefer tea without sugar.'),
                                      ('ana-preview-123456', 'Ana', 'I enjoy painting landscapes.')]:
            folder = sandbox / 'data/avatars' / avatar_id
            folder.mkdir(parents=True)
            (folder / 'avatar.json').write_text(json.dumps({'id': avatar_id, 'name': name, 'card': {'name': name, 'age': 30}}), 'utf-8')
            own_sid = store.ensure_avatar_session(avatar_id, name)
            source = store.save_message(own_sid, 'user', fact)
            fake_memory.store.add_memory(fact, avatar_id=avatar_id, session_id=own_sid, user_context='auto_extracted',
                                         source_refs=[reference(store.get_message(source))])
            notes.NotesStore(companion.get_services().avatars).write(avatar_id, about_you='- ' + fact, our_story='- A sample conversation')
        fake_memory.store.add_memory('An older automatic preference for tea', avatar_id='mara-preview-123456', user_context='auto_extracted')
        fake_memory.store.add_memory('An older fact awaiting confirmation', avatar_id='mara-preview-123456')

        class FakeLLM:
            model, base_url, provider_key, max_tokens = 'preview-fake', 'http://127.0.0.1:1/v1', 'preview', 1024
            def chat(self, messages, stream=False, **kwargs):
                from core.diagnostics import begin_call
                begin_call(self, messages)
                reply = 'Here is a fresh preview reply. It arrives slowly so you can test Stop. No real model is being called.'
                if not stream:
                    return reply
                def chunks():
                    for word in reply.split(' '):
                        time.sleep(.35)
                        yield word + ' '
                return chunks()

        chat.ChatEngine.llm_client = property(lambda self: FakeLLM())
        chat.ChatEngine._extract_memories = lambda *a, **kw: None
        engine = chat.ChatEngine(settings, config.get_personality())
        engine._supports_tools = False
        engine._extract_memories = lambda *a, **kw: None
        chat._chat_engine_instance = engine
        import core.emotion
        core.emotion.analyze_and_store_emotion = lambda *a, **kw: None
        from core.voice_notes import get_voice_note_service
        voice_notes = get_voice_note_service(store)
        voice_notes.transcriber = lambda *a, **kw: {'text': 'A saved voice note from this isolated preview.', 'language': 'en'}
        import altera
        from flask import jsonify, session
        app = altera.app
        app.config['TEMPLATES_AUTO_RELOAD'] = True
        app.jinja_env.auto_reload = True
        # Presence uses the real ledger/routes and only this synthetic provider.
        from core.presence import get_presence_service
        presence = get_presence_service(store)
        presence.provider_getter = lambda: {'provider_key': 'preview', 'model': 'preview-fake',
                                            'base_url': 'http://127.0.0.1:1/v1', 'api_key': 'unused-preview'}
        presence.llm = lambda *a, **kw: 'A quiet hello from the isolated preview. How is your afternoon going?'
        presence._busy = lambda: False  # Deterministic fake-only preview; real lease behavior is tested separately.
        from core.reflection import get_reflection_service
        reflection = get_reflection_service(store)
        reflection.provider_getter = presence.provider_getter
        reflection._busy = lambda: False
        reflection.llm = lambda *a, **kw: json.dumps({
            'episodes': [{'title': 'A small tea ritual', 'summary': 'The user mentioned preferring tea without sugar.',
                          'uncertainty': 'One stated preference; no shared real-world event is established.', 'confidence': .8, 'source_ids': [0]}],
            'notebook': [{'note': 'about_you', 'text': 'The user said they prefer tea without sugar.',
                          'uncertainty': 'Preferences can change.', 'confidence': .8, 'source_ids': [0]}]})
        from core.episodes import get_episode_service
        episodes = get_episode_service(store)
        epoch = store.dataset_epoch()['value']
        mara = 'mara-preview-123456'
        mara_sid = store.get_avatar_session_id(mara)
        mara_source = store.get_messages(mara_sid, limit=1)[0].id
        for request_id, title, kind in [('preview_tea_moment', 'A little tea ritual', 'actual_exchange'),
                                         ('preview_imagined_moment', 'An imagined rainy cafe', 'imagined_scene'),
                                         ('preview_restored_moment', 'A retained first conversation', 'actual_exchange')]:
            item = episodes.capture(mara_sid, epoch, {'request_id': request_id, 'kind': kind, 'title': title,
                                    'summary': 'A deliberately curated sample for this isolated preview.',
                                    'uncertainty': 'Synthetic preview evidence.', 'sources': [{'message_id': mara_source}], 'in_story': True})['episode']
            if request_id == 'preview_restored_moment':
                item = episodes.change(item['id'], mara_sid, epoch, item['revision'], {'pinned': True, 'salience': 5})['episode']
                with store._connect() as conn:
                    conn.execute("UPDATE companion_episodes SET dataset_epoch='older-preview-epoch',status='stale',reason='Retained sample for explicit restore review.' WHERE id=?", (item['id'],))
                    conn.commit()
                conn.close()

        @app.after_request
        def finish_fake_presence(response):
            from flask import request
            if request.endpoint == 'presence.check_now' and response.status_code == 202:
                presence.run_one()
            if request.endpoint == 'reflection.run' and response.status_code == 202:
                reflection.run_one()
            return response

        @app.before_request
        def seed_session():
            session['session_id'] = sid

        app.view_functions['system.system_health'] = lambda: jsonify(llm={'status': 'ok', 'message': 'Isolated fake model'},
                                                             voice={'status': 'disabled', 'message': 'Preview'},
                                                             memory={'status': 'ok', 'message': 'Isolated test store'})
        app.view_functions['llm.list_models'] = lambda provider_key: jsonify(provider=provider_key, models=[{'id': 'preview-fake'}])

        @app.get('/qa/state')
        def state():
            return jsonify(messages=[m.__dict__ for m in store.get_messages(sid, limit=1000)],
                           sandbox=str(sandbox), session_id=sid)

        server = make_server('127.0.0.1', 0, app, threaded=True)
        url = f'http://127.0.0.1:{server.server_port}'
        (audit / 'preview.json').write_text(json.dumps({'url': url, 'sandbox': str(sandbox), 'pid': os.getpid()}, indent=2), encoding='utf-8')
        print(url, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            import logging
            logging.shutdown()
            import gc
            gc.collect()
            # Windows cannot remove the process's current directory when the temporary workspace exits.
            os.chdir(ROOT)


if __name__ == '__main__':
    main()
