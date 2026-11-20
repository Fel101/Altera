"""Synthetic assets/fake CPU only; real Chroma tests use precomputed vectors."""
import json
from pathlib import Path
import socket
import stat
import threading
import traceback
from types import SimpleNamespace as NS

import numpy as np
import pytest

from core import local_embeddings as local


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import httpx
    import requests
    import tarfile
    import urllib.request
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
    def forbidden(*args, **kwargs):
        raise AssertionError('A model download, extraction, network call or default embedding was attempted')
    def forbidden_embedding(self, input):
        raise AssertionError('The upstream download-capable embedding function was called')
    monkeypatch.setattr(httpx, 'stream', forbidden)
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(tarfile, 'open', forbidden)
    monkeypatch.setattr(ONNXMiniLM_L6_V2, '_download', forbidden)
    monkeypatch.setattr(ONNXMiniLM_L6_V2, '_download_model_if_not_exists', forbidden)
    monkeypatch.setattr(ONNXMiniLM_L6_V2, '__call__', forbidden_embedding)
    monkeypatch.setattr(local.DefaultEmbeddingFunction, '__call__', forbidden_embedding)
    local.clear_embedding_cache()
    yield
    local.clear_embedding_cache()


@pytest.fixture
def assets(tmp_path):
    folder = tmp_path / 'existing-model'
    folder.mkdir()
    words = [f'synthetic-{index}' for index in range(30522)]
    for word, index in (('[PAD]', 0), ('[UNK]', 100), ('[CLS]', 101), ('[SEP]', 102), ('[MASK]', 103)):
        words[index] = word
    files = {
        'config.json': {'model_type': 'bert', 'hidden_size': 384, 'num_hidden_layers': 6, 'vocab_size': 30522},
        'tokenizer.json': {'model': {'type': 'WordPiece', 'vocab': {word: index for index, word in enumerate(words)}}},
        'tokenizer_config.json': {'do_lower_case': True},
        'special_tokens_map.json': {'pad_token': '[PAD]', 'unk_token': '[UNK]', 'cls_token': '[CLS]',
                                    'sep_token': '[SEP]', 'mask_token': '[MASK]'},
    }
    for name, value in files.items():
        (folder / name).write_text(json.dumps(value), 'utf-8')
    (folder / 'vocab.txt').write_text('\n'.join(words) + '\n', 'utf-8')
    (folder / 'model.onnx').write_bytes(b'SYNTHETIC MODEL: never used for real inference')
    return folder


class FakeEngine:
    instances = []
    def __init__(self, values):
        self.count = 0
        self.instances.append(self)
        assert set(values) == set(local.ASSETS)

    def embed(self, documents):
        self.count += 1
        return np.array([[1.] + [0.] * 383 for _ in documents], dtype=np.float32)


def function(assets, factory=FakeEngine):
    return local.CachedLocalDefaultEmbeddingFunction(assets, engine_factory=factory)


def test_metadata_and_default_configuration_are_lazy_and_do_not_register_a_replacement(monkeypatch):
    from chromadb.utils.embedding_functions import known_embedding_functions
    before = known_embedding_functions['default']
    def unavailable(*args):
        pytest.fail('Metadata construction must not resolve assets or settings')
    monkeypatch.setattr(local, '_source_path', unavailable)
    value = local.CachedLocalDefaultEmbeddingFunction()
    assert value.name() == 'default' and value.get_config() == {} and value.max_tokens() == 256
    assert not isinstance(value, local.DefaultEmbeddingFunction)
    assert not value.is_legacy()
    options = local.default_collection_options(embedding_function=value)
    assert options['embedding_function'] is value
    assert type(options['configuration']['embedding_function']) is local.DefaultEmbeddingFunction
    assert known_embedding_functions['default'] is before


@pytest.mark.parametrize('missing', local.ASSETS)
def test_missing_assets_fail_without_network_creation_extraction_or_engine(tmp_path, assets, monkeypatch, missing):
    (assets / missing).unlink()
    before = {path.name: path.read_bytes() for path in assets.iterdir()}
    def forbidden(*args, **kwargs):
        pytest.fail('Embedding must not create directories or initialize an engine with incomplete assets')
    value = function(assets, forbidden)
    monkeypatch.setattr(Path, 'mkdir', forbidden)
    monkeypatch.setattr(local.os, 'makedirs', forbidden)
    with pytest.raises(local.LocalEmbeddingError, match='memory.embedding_model_path'):
        value(['A synthetic query'])
    assert {path.name: path.read_bytes() for path in assets.iterdir()} == before


@pytest.mark.parametrize('corruption', ['json', 'shape', 'vocabulary', 'token-id', 'special-token', 'empty-model', 'directory'])
def test_corrupt_or_incompatible_assets_fail_before_engine_without_modifying_sources(assets, corruption):
    if corruption == 'json':
        (assets / 'tokenizer_config.json').write_text('{broken', 'utf-8')
    elif corruption == 'shape':
        (assets / 'config.json').write_text('{"model_type":"bert","hidden_size":768}', 'utf-8')
    elif corruption in {'vocabulary', 'token-id'}:
        value = json.loads((assets / 'tokenizer.json').read_text('utf-8'))
        value['model']['vocab']['[PAD]'] = 1 if corruption == 'token-id' else True
        (assets / 'tokenizer.json').write_text(json.dumps(value), 'utf-8')
    elif corruption == 'special-token':
        (assets / 'special_tokens_map.json').write_text('{}', 'utf-8')
    elif corruption == 'empty-model':
        (assets / 'model.onnx').write_bytes(b'')
    else:
        (assets / 'model.onnx').unlink()
        (assets / 'model.onnx').mkdir()
    def engine(values):
        pytest.fail('Invalid assets must not initialize the engine')
    with pytest.raises(local.LocalEmbeddingError):
        function(assets, engine)(['Synthetic input'])


def test_relative_explicit_model_path_uses_instance_root_and_default_remains_original_cache(assets, tmp_path, monkeypatch):
    from core import config
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(config, 'get_config', lambda: {'memory': {'embedding_model_path': assets.name}})
    value = local.CachedLocalDefaultEmbeddingFunction(engine_factory=FakeEngine)
    assert value(['Synthetic input'])[0].shape == (384,)
    monkeypatch.setattr(config, 'get_config', lambda: {'memory': {}})
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'synthetic-user'))
    assert local._source_path() == tmp_path / 'synthetic-user/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx'
    assert not (tmp_path / 'synthetic-user').exists()


def test_cached_engine_is_reused_and_asset_change_fails_closed(assets):
    value = function(assets)
    first = value(['First synthetic input'])
    engine = next(reversed(local._ENGINES.values()))
    second = value(['Second synthetic input'])
    assert engine.count == 2 and np.array_equal(first[0], second[0])
    (assets / 'vocab.txt').write_text('changed', 'utf-8')
    with pytest.raises(local.LocalEmbeddingError):
        value(['Third synthetic input'])
    assert engine.count == 2


def test_engine_cache_is_bounded_without_result_or_document_cache(tmp_path, assets):
    import shutil
    for index in range(4):
        root = tmp_path / f'model-{index}'
        shutil.copytree(assets, root)
        function(root)([f'Synthetic text {index}'])
    assert len(local._ENGINES) == local.MAX_ENGINES == 2
    assert all(set(vars(engine)) == {'count'} for engine in local._ENGINES.values())


def test_embedding_calls_are_serialized_and_initialize_one_engine(assets):
    first, release = threading.Event(), threading.Event()
    calls, errors = [], []
    class Blocking(FakeEngine):
        def embed(self, documents):
            calls.append(documents[0])
            first.set()
            assert release.wait(5)
            return super().embed(documents)
    value = function(assets, Blocking)
    def query(text):
        try:
            value([text])
        except Exception as error:
            errors.append(error)
    a, b = threading.Thread(target=query, args=('first',)), threading.Thread(target=query, args=('second',))
    a.start()
    assert first.wait(5)
    b.start()
    assert calls == ['first']
    release.set()
    a.join(5)
    b.join(5)
    assert not errors and calls == ['first', 'second'] and len(local._ENGINES) == 1


def test_engine_error_is_fixed_without_input_or_path_and_failed_engine_is_evicted(assets):
    secret = 'Synthetic private document contents'
    class Broken(FakeEngine):
        def embed(self, documents):
            raise RuntimeError(secret + str(assets))
    with pytest.raises(local.LocalEmbeddingError) as caught:
        function(assets, Broken)([secret])
    assert secret not in ''.join(traceback.format_exception(caught.value))
    assert str(assets) not in str(caught.value) and not local._ENGINES


@pytest.mark.parametrize('input', [[], ['x'] * 257, [None], 'not a list', ['\ud800'], ['x' * (local.MAX_INPUT_BYTES + 1)]])
def test_invalid_or_unbounded_input_fails_before_asset_lookup(monkeypatch, input):
    monkeypatch.setattr(local, '_source_path', lambda *args: pytest.fail('Invalid input must not inspect model assets'))
    with pytest.raises(local.LocalEmbeddingError):
        local.CachedLocalDefaultEmbeddingFunction()(input)


def test_cpu_engine_uses_memory_bytes_and_upstream_math_with_fake_runtime(assets, monkeypatch):
    import sys
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
    calls = []
    class Tokenizer:
        @staticmethod
        def from_str(value):
            assert isinstance(json.loads(value), dict)
            return Tokenizer()
        def enable_truncation(self, **kwargs):
            assert kwargs == {'max_length': 256}
        def enable_padding(self, **kwargs):
            assert kwargs == {'pad_id': 0, 'pad_token': '[PAD]', 'length': 256}
        def encode(self, value):
            return NS(ids=[1, 2], attention_mask=[1, 0])
    class Session:
        def __init__(self, source, **kwargs):
            assert isinstance(source, bytes) and source == (assets / 'model.onnx').read_bytes()
            assert kwargs['providers'] == ['CPUExecutionProvider']
            assert not getattr(kwargs['sess_options'], 'optimized_model_filepath', '')
            calls.append('load')
        def get_inputs(self):
            return [NS(name=name, type='tensor(int64)') for name in ('input_ids', 'attention_mask', 'token_type_ids')]
        def get_outputs(self):
            return [NS(type='tensor(float)', shape=['batch', 'sequence', 384])]
        def run(self, _, input):
            calls.append('fake-run')
            batch = len(input['input_ids'])
            data = np.tile(np.arange(768, dtype=np.float32).reshape(1, 2, 384), (batch, 1, 1))
            return [data]
    monkeypatch.setitem(sys.modules, 'tokenizers', NS(Tokenizer=Tokenizer))
    monkeypatch.setitem(sys.modules, 'onnxruntime', NS(SessionOptions=NS, GraphOptimizationLevel=NS(ORT_ENABLE_ALL=99),
                                                    InferenceSession=Session))
    value = function(assets, local._MiniLMEngine)
    actual = value(['Synthetic input'])
    engine = next(iter(local._ENGINES.values()))
    reference = ONNXMiniLM_L6_V2._forward(engine, ['Synthetic input'])
    assert np.array_equal(actual[0], reference[0]) and calls == ['load', 'fake-run', 'fake-run']


@pytest.mark.parametrize('phase', ['client', 'collection'])
def test_memory_store_initializes_once_for_concurrent_callers(tmp_path, monkeypatch, phase):
    import chromadb
    from concurrent.futures import ThreadPoolExecutor
    from core.memory import MemoryStore

    entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
    calls = {'client': 0, 'collection': 0}
    collection = object()

    def record(name):
        calls[name] += 1
        if name == phase:
            if calls[name] > 1:
                second_entered.set()
            entered.set()
            assert release.wait(5), 'Synthetic initialization was not released'

    class Client:
        def list_collections(self):
            return []

        def get_or_create_collection(self, **kwargs):
            record('collection')
            return collection

    def create_client(**kwargs):
        record('client')
        return Client()

    monkeypatch.setattr(chromadb, 'PersistentClient', create_client)
    monkeypatch.setattr(local, 'default_collection_options', dict)
    monkeypatch.setattr(local, 'require_default_compatible', lambda _: None)
    store = MemoryStore(tmp_path / 'vectors')
    barrier = threading.Barrier(8)

    def get_collection():
        barrier.wait(timeout=5)
        return store.collection

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(get_collection) for _ in range(8)]
        try:
            assert entered.wait(5)
            assert not second_entered.wait(0.2)
        finally:
            release.set()
        assert all(future.result(timeout=5) is collection for future in futures)
    assert calls == {'client': 1, 'collection': 1}


def test_memory_store_retries_collection_initialization_after_failure(tmp_path, monkeypatch):
    import chromadb
    from core.memory import MemoryStore
    attempts, collection = [], object()

    class Client:
        def list_collections(self):
            return []

        def get_or_create_collection(self, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError('synthetic temporary initialization failure')
            return collection

    monkeypatch.setattr(chromadb, 'PersistentClient', lambda **kwargs: Client())
    monkeypatch.setattr(local, 'default_collection_options', dict)
    monkeypatch.setattr(local, 'require_default_compatible', lambda _: None)
    store = MemoryStore(tmp_path / 'vectors')
    with pytest.raises(RuntimeError, match='synthetic temporary'):
        _ = store.collection
    assert store._collection is None and store.collection is collection and len(attempts) == 2


def test_native_chroma_default_legacy_snapshot_restore_never_loads_assets(tmp_path, monkeypatch):
    import chromadb
    from chromadb.config import Settings
    from chromadb.utils.embedding_functions import known_embedding_functions
    from core.memory import MemoryStore
    before = known_embedding_functions['default']
    client = chromadb.PersistentClient(path=str(tmp_path / 'vectors'), settings=Settings(anonymized_telemetry=False))
    legacy = client.create_collection('altera_memories', metadata={'hnsw:space': 'cosine'})
    a, b = [1.] + [0.] * 383, [0., 1.] + [0.] * 382
    legacy.add(ids=['one', 'two'], documents=['Synthetic tea', 'Synthetic coffee'], embeddings=[a, b])
    factory = local.default_collection_options
    monkeypatch.setattr(local, 'default_collection_options', lambda **kwargs: factory(model_path=tmp_path / 'missing', **kwargs))
    store = MemoryStore(str(tmp_path / 'vectors'))
    store._client = client
    assert len(store.get_all()) == 2
    saved = store.snapshot()
    assert saved['embedding_function'] == 'default'
    store.restore_snapshot(saved)
    assert store.snapshot() == saved
    assert store.collection.query(query_embeddings=[a], n_results=1)['ids'] == [['one']]
    with pytest.raises(local.LocalEmbeddingError):
        store.search('Synthetic query')
    assert known_embedding_functions['default'] is before and not local._ENGINES


def test_memories_saved_under_the_old_collection_name_are_kept_after_the_rename(tmp_path, monkeypatch):
    import chromadb
    from chromadb.config import Settings
    from core.memory import COLLECTION, LEGACY_COLLECTION, MemoryStore
    client = chromadb.PersistentClient(path=str(tmp_path / 'vectors'), settings=Settings(anonymized_telemetry=False))
    old = client.create_collection(LEGACY_COLLECTION, metadata={'hnsw:space': 'cosine'}, **local.default_collection_options(model_path=tmp_path / 'missing'))
    old.add(ids=['one'], documents=['Synthetic tea'], embeddings=[[1.] + [0.] * 383])
    factory = local.default_collection_options
    monkeypatch.setattr(local, 'default_collection_options', lambda **kwargs: factory(model_path=tmp_path / 'missing', **kwargs))
    store = MemoryStore(str(tmp_path / 'vectors'))
    store._client = client
    assert [item['content'] for item in store.get_all()] == ['Synthetic tea']
    assert {item.name for item in client.list_collections()} == {COLLECTION}
    store._collection = None
    client.create_collection(LEGACY_COLLECTION).add(ids=['stale'], documents=['Stale'], embeddings=[[0., 1.] + [0.] * 382])
    assert [item['content'] for item in store.get_all()] == ['Synthetic tea'], 'an existing new collection wins'


def test_native_chroma_add_update_and_query_use_only_explicit_safe_runtime(tmp_path, assets, monkeypatch):
    from chromadb.utils.embedding_functions import known_embedding_functions
    from core.memory import MemoryStore
    before = known_embedding_functions['default']
    factory = local.default_collection_options
    monkeypatch.setattr(local, 'default_collection_options', lambda **kwargs: factory(model_path=assets, engine_factory=FakeEngine, **kwargs))
    store = MemoryStore(str(tmp_path / 'vectors'))
    identifier = store.add_memory('Synthetic preference', avatar_id='fixture')
    assert store.search('Synthetic query', avatar_id='fixture')[0]['id'] == identifier
    store.update_memory(identifier, content='Synthetic corrected preference')
    engine = next(iter(local._ENGINES.values()))
    assert engine.count == 3
    saved = store.snapshot()
    store.restore_snapshot(saved)
    assert engine.count == 3 and store.snapshot() == saved
    assert known_embedding_functions['default'] is before


def test_incompatible_persisted_model_is_refused_before_silent_semantic_change():
    collection = NS(_model=NS(configuration_json={'embedding_function': {'name': 'different-model', 'type': 'known', 'config': {}}}))
    with pytest.raises(local.LocalEmbeddingError, match='different embedding model'):
        local.require_default_compatible(collection)


def test_native_chroma_custom_model_is_preserved_and_not_silently_reembedded(tmp_path):
    import chromadb
    from chromadb.api.types import EmbeddingFunction
    from chromadb.config import Settings
    from chromadb.utils.embedding_functions import known_embedding_functions
    from core.memory import MemoryStore
    class OtherModel(EmbeddingFunction):
        def __init__(self):
            pass
        @staticmethod
        def name():
            return 'altera-fixture-other-model'
        def get_config(self):
            return {}
        @staticmethod
        def build_from_config(config):
            return OtherModel()
        def __call__(self, input):
            pytest.fail('Metadata/compatibility inspection must not run another model')
    before = known_embedding_functions['default']
    client = chromadb.PersistentClient(path=str(tmp_path / 'custom-vectors'), settings=Settings(anonymized_telemetry=False))
    original = client.create_collection('altera_memories', embedding_function=OtherModel())
    original.add(ids=['old'], documents=['Original model content'], embeddings=[[1., 0., 0.]])
    store = MemoryStore(str(tmp_path / 'custom-vectors'))
    store._client = client
    with pytest.raises(local.LocalEmbeddingError, match='different embedding model'):
        store.collection  # noqa: B018 - opening the collection is what is tested
    assert original.get()['ids'] == ['old'] and original.get()['documents'] == ['Original model content']
    assert known_embedding_functions['default'] is before and not local._ENGINES
    store.clear_all()
    assert not client.list_collections() and store._collection is None
    assert known_embedding_functions['default'] is before and not local._ENGINES


def test_corrupt_native_model_load_fails_before_embedding_and_never_publishes_engine(assets):
    calls = []
    class InvalidModel:
        def __init__(self, values):
            calls.append('load')
            raise RuntimeError('Invalid synthetic ONNX model at a private path')
        def embed(self, input):
            pytest.fail('A corrupt model must not reach inference')
    with pytest.raises(local.LocalEmbeddingError) as caught:
        function(assets, InvalidModel)(['Synthetic input'])
    assert calls == ['load'] and not local._ENGINES
    assert 'private path' not in str(caught.value)


def test_oversized_asset_is_rejected_before_open_or_engine(assets, monkeypatch):
    monkeypatch.setitem(local._LIMITS, 'model.onnx', 8)
    with pytest.raises(local.LocalEmbeddingError):
        function(assets, lambda values: pytest.fail('Oversized model reached engine'))(['Synthetic input'])


def test_linked_asset_is_rejected_without_reading_its_target(assets, monkeypatch):
    original = Path.lstat
    def linked(path, *args, **kwargs):
        if path == assets / 'model.onnx':
            return NS(st_mode=stat.S_IFREG, st_file_attributes=0x400)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', linked)
    with pytest.raises(local.LocalEmbeddingError):
        function(assets, lambda values: pytest.fail('Linked model reached engine'))(['Synthetic input'])


def test_network_source_is_rejected_before_any_asset_lookup(monkeypatch):
    monkeypatch.setattr(local, '_signature', lambda *args: pytest.fail('Network source must not be inspected'))
    with pytest.raises(local.LocalEmbeddingError):
        function(r'\\untrusted-host\models')(['Synthetic input'])


def test_assets_changed_during_engine_load_are_not_cached_or_used(assets):
    engines = []
    def changed(values):
        engine = FakeEngine(values)
        engines.append(engine)
        (assets / 'vocab.txt').write_text('Changed during synthetic model load', 'utf-8')
        return engine
    with pytest.raises(local.LocalEmbeddingError):
        function(assets, changed)(['Synthetic input'])
    assert engines[0].count == 0 and not local._ENGINES
