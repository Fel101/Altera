"""Read-only local MiniLM embeddings, retaining Chroma's default identity.

Only explicit embedding calls inspect assets or initialize the local CPU engine.
This module never downloads, extracts, creates directories or publishes files.
"""
from collections import OrderedDict
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import threading

import numpy as np
from chromadb.api.types import DefaultEmbeddingFunction, EmbeddingFunction

from core.filestat import ctime_ns

ASSETS = ('config.json', 'model.onnx', 'special_tokens_map.json', 'tokenizer_config.json', 'tokenizer.json', 'vocab.txt')
MAX_ENGINES = 2
MAX_DOCUMENTS = 256
MAX_INPUT_BYTES = 1024 * 1024
_LIMITS = {'model.onnx': 256 * 1024 ** 2, 'tokenizer.json': 16 * 1024 ** 2,
           'vocab.txt': 4 * 1024 ** 2}
_ERROR = ('Local MiniLM embeddings are unavailable. Supply the six existing all-MiniLM-L6-v2 ONNX assets '
          'through memory.embedding_model_path (relative to ALTERA_HOME), or restore the installed Chroma cache. '
          'Altera will not download or repair model assets during chat.')
_LOCK = threading.RLock()
_ENGINES = OrderedDict()


class LocalEmbeddingError(ValueError):
    """A fixed actionable error without document text or private paths."""


class IncompatibleEmbeddingModel(LocalEmbeddingError):
    """Stored vector semantics require review; explicit deletion is still safe."""


def _source_path(configured=None, root=None):
    if configured is None:
        from core import config
        settings = config.get_config().get('memory', {})
        if not isinstance(settings, dict):
            raise LocalEmbeddingError(_ERROR)
        configured = settings.get('embedding_model_path')
        root = config.ROOT if root is None else root
    if configured in (None, ''):
        # Match the installed Chroma default without constructing its downloader.
        return Path.home() / '.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx'
    if not isinstance(configured, (str, Path)) or not 0 < len(str(configured)) <= 4096:
        raise LocalEmbeddingError(_ERROR)
    text = str(configured)
    if text.startswith(('\\\\', '//')) or any(char in text for char in '\x00\r\n'):
        raise LocalEmbeddingError(_ERROR)
    path = Path(configured).expanduser()
    if not path.is_absolute():
        if root is None:
            from core.config import ROOT
            root = ROOT
        if not Path(root).is_absolute():
            raise LocalEmbeddingError(_ERROR)
        path = Path(root) / path
    return path


def _signature(folder):
    info = folder.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise LocalEmbeddingError(_ERROR)
    result = []
    for name in ASSETS:
        item = (folder / name).lstat()
        limit = _LIMITS.get(name, 1024 * 1024)
        if (not stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode)
                or getattr(item, 'st_file_attributes', 0) & 0x400 or not 0 < item.st_size <= limit):
            raise LocalEmbeddingError(_ERROR)
        result.append((name, item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, ctime_ns(item)))
    return tuple(result)


@contextmanager
def _owned_asset(stream):
    try:
        yield stream
    finally:
        try:
            stream.close()
        except BaseException:
            raise LocalEmbeddingError('Local embedding asset cleanup could not be confirmed.') from None


def _read_assets(folder, signature):
    values = {}
    for name, *expected in signature:
        with _owned_asset((folder / name).open('rb')) as stream:
            before = stream.fileno()
            info = os.fstat(before)
            actual = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, ctime_ns(info))
            if actual != tuple(expected):
                raise LocalEmbeddingError(_ERROR)
            values[name] = stream.read(_LIMITS.get(name, 1024 * 1024) + 1)
            after = os.fstat(before)
            if actual != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, ctime_ns(after)):
                raise LocalEmbeddingError(_ERROR)
        if len(values[name]) != expected[2]:
            raise LocalEmbeddingError(_ERROR)
    if _signature(folder) != signature:
        raise LocalEmbeddingError(_ERROR)
    return values


def _metadata(values):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    def invalid(_):
        raise ValueError()
    parsed = {name: json.loads(values[name], object_pairs_hook=unique, parse_constant=invalid)
              for name in ASSETS if name.endswith('.json')}
    if any(not isinstance(item, dict) for item in parsed.values()):
        raise LocalEmbeddingError(_ERROR)
    config = parsed['config.json']
    if (config.get('model_type') != 'bert' or config.get('hidden_size') != 384
            or config.get('num_hidden_layers') != 6 or config.get('vocab_size') != 30522):
        raise LocalEmbeddingError(_ERROR)
    model = parsed['tokenizer.json'].get('model', {})
    vocabulary = model.get('vocab') if isinstance(model, dict) else None
    lines = values['vocab.txt'].decode('utf-8', errors='strict').splitlines()
    if (model.get('type') != 'WordPiece' or not isinstance(vocabulary, dict) or len(vocabulary) != 30522
            or len(lines) != 30522 or any(type(vocabulary.get(word)) is not int or vocabulary[word] != index
                                         for index, word in enumerate(lines))):
        raise LocalEmbeddingError(_ERROR)
    for word, identifier in (('[PAD]', 0), ('[UNK]', 100), ('[CLS]', 101), ('[SEP]', 102), ('[MASK]', 103)):
        if vocabulary.get(word) != identifier:
            raise LocalEmbeddingError(_ERROR)
    special = parsed['special_tokens_map.json']
    for key, word in (('pad_token', '[PAD]'), ('unk_token', '[UNK]'), ('cls_token', '[CLS]'),
                      ('sep_token', '[SEP]'), ('mask_token', '[MASK]')):
        value = special.get(key)
        if (value.get('content') if isinstance(value, dict) else value) != word:
            raise LocalEmbeddingError(_ERROR)


class _MiniLMEngine:
    def __init__(self, assets):
        import onnxruntime
        from tokenizers import Tokenizer
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
        self.tokenizer = Tokenizer.from_str(assets['tokenizer.json'].decode('utf-8'))
        self.tokenizer.enable_truncation(max_length=256)
        self.tokenizer.enable_padding(pad_id=0, pad_token='[PAD]', length=256)
        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3
        options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
        # Bytes prevent native model-path/external-data resolution; CPU avoids
        # execution-provider disk caches. No optimized model output is selected.
        self.model = onnxruntime.InferenceSession(assets['model.onnx'], providers=['CPUExecutionProvider'],
                                                  sess_options=options)
        inputs = self.model.get_inputs()
        outputs = self.model.get_outputs()
        if ({item.name for item in inputs} != {'input_ids', 'attention_mask', 'token_type_ids'}
                or any(item.type != 'tensor(int64)' for item in inputs) or not outputs
                or outputs[0].type != 'tensor(float)' or outputs[0].shape[-1] != 384):
            raise LocalEmbeddingError(_ERROR)
        self._forward = ONNXMiniLM_L6_V2._forward
        self._normalize = lambda vector: ONNXMiniLM_L6_V2._normalize(self, vector)

    def max_tokens(self):
        return 256

    def embed(self, documents):
        # Reuse the installed default's exact token pooling/normalization math.
        return self._forward(self, documents)


def clear_embedding_cache():
    """Release this module's bounded engine references; no filesystem changes."""
    with _LOCK:
        _ENGINES.clear()


class CachedLocalDefaultEmbeddingFunction(EmbeddingFunction):
    """Compatible default identity with explicit, local-only runtime behavior."""
    def __init__(self, model_path=None, *, root=None, engine_factory=None):
        self._path, self._root, self._factory = model_path, root, engine_factory or _MiniLMEngine

    @staticmethod
    def name():
        return 'default'

    def get_config(self):
        return {}

    @staticmethod
    def build_from_config(config):
        if config != {}:
            raise LocalEmbeddingError(_ERROR)
        return CachedLocalDefaultEmbeddingFunction()

    def max_tokens(self):
        return 256

    def __call__(self, input):
        if (not isinstance(input, list) or not 0 < len(input) <= MAX_DOCUMENTS
                or any(not isinstance(item, str) for item in input)):
            raise LocalEmbeddingError('Use a bounded nonempty list of text documents for local embeddings.')
        total = 0
        try:
            for item in input:
                if len(item) > MAX_INPUT_BYTES:
                    raise ValueError()
                total += len(item.encode('utf-8'))
                if total > MAX_INPUT_BYTES:
                    raise ValueError()
        except (ValueError, UnicodeError):
            raise LocalEmbeddingError('Use at most 1 MiB of valid UTF-8 text per local embedding request.') from None
        try:
            with _LOCK:
                folder = _source_path(self._path, self._root)
                signature = _signature(folder)
                key = (str(folder.absolute()), signature, self._factory)
                engine = _ENGINES.get(key)
                if engine is None:
                    assets = _read_assets(folder, signature)
                    _metadata(assets)
                    engine = self._factory(assets)
                    if _signature(folder) != signature:
                        raise LocalEmbeddingError(_ERROR)
                    while len(_ENGINES) >= MAX_ENGINES:
                        _ENGINES.popitem(last=False)
                    _ENGINES[key] = engine
                _ENGINES.move_to_end(key)
                try:
                    vectors = np.asarray(engine.embed(input), dtype=np.float32)
                    if vectors.shape != (len(input), 384) or not np.isfinite(vectors).all():
                        raise ValueError()
                except Exception:
                    _ENGINES.pop(key, None)
                    raise
                return [vector for vector in vectors]
        except Exception:
            raise LocalEmbeddingError(_ERROR) from None


def default_collection_options(*, embedding_function=None, model_path=None, root=None, engine_factory=None):
    """Keep runtime safety separate from the persisted builtin default identity.

    Chroma auto-registers configuration classes globally; supplying its inert
    builtin here avoids registering our runtime wrapper as the global default.
    """
    function = embedding_function if isinstance(embedding_function, CachedLocalDefaultEmbeddingFunction) else (
        CachedLocalDefaultEmbeddingFunction(model_path, root=root, engine_factory=engine_factory))
    return {'embedding_function': function, 'configuration': {'embedding_function': DefaultEmbeddingFunction()}}


def require_default_compatible(collection):
    """Refuse silent replacement of another model's stored vector semantics."""
    model = getattr(collection, '_model', None)
    config = getattr(model, 'configuration_json', None)
    if config is None:
        return
    function = config.get('embedding_function') if isinstance(config, dict) else False
    if function is None or isinstance(function, dict) and function.get('type') == 'legacy':
        return
    if (not isinstance(function, dict) or function.get('name') not in {'default', 'onnx_mini_lm_l6_v2'}
            or function.get('name') == 'default' and function.get('config') != {}):
        raise IncompatibleEmbeddingModel('This memory collection uses a different embedding model. Preserve its vectors and select a compatible collection explicitly.')
