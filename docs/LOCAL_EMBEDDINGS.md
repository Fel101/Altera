# Cached local memory embeddings

`core.local_embeddings` makes Altera's default Chroma memory embedding path read-only and local. It keeps the existing all-MiniLM-L6-v2 model, tokenizer settings, pooling, normalization, 384-dimensional vectors and persisted `name="default"` / `get_config() == {}` identity. It does not replace the model with a different semantic space or re-embed existing memories.

The implementation was reviewed against installed ChromaDB 1.5.9, ONNX Runtime 1.27.0 and tokenizers 0.23.1. Chroma's default `__call__` normally constructs `ONNXMiniLM_L6_V2`, whose missing-file path can download an archive, create a cache directory and extract files. Altera never invokes that call/download/extraction path. It reuses only the installed MiniLM `_forward` numerical implementation after local validation. Automated tests use synthetic model bytes and fake engines.

## Existing asset source

Runtime requirements pin ChromaDB 1.5.9 and constrain tokenizers to `>=0.23.1,<0.24`; the recorded lock keeps tokenizers 0.23.1. The adapter relies on reviewed Chroma collection-configuration and internal forward interfaces; dependency upgrades must recheck those contracts rather than silently changing them. Promoting the tokenizer to a direct dependency requires no installation or lock change in an already compatible environment. The preserved fresh runtime has tokenizer patch 0.23.3; its separate acceptance is recorded with its actual versions rather than downgrading it to match the development runtime.

By default, Altera reads the six already installed assets directly from:

```text
~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/
```

An optional existing source folder can be selected with `memory.embedding_model_path`. Relative paths resolve against the instance's `ALTERA_HOME`/`core.config.ROOT`, independently of the shell's working directory. For example, `models/minilm/onnx` refers to that existing folder under the instance. The setting identifies the folder containing the six files directly; it is not a download URL, archive path or cache destination. Absolute local paths can identify approved read-only public model assets. UNC/network source paths are refused; deployment must also ensure that a selected drive is local.

Required files are `config.json`, `model.onnx`, `special_tokens_map.json`, `tokenizer_config.json`, `tokenizer.json` and `vocab.txt`. A missing folder/file, linked or nonregular asset, oversized/empty file, malformed metadata, incompatible MiniLM dimensions/vocabulary, changed source during loading, tokenizer failure or ONNX load failure produces a fixed actionable `LocalEmbeddingError`. Altera never repairs, downloads, extracts, duplicates or creates a model cache during query/add/update. An archive alone is insufficient; supplying/extracting approved assets remains an explicit operator setup action outside chat. `setup.bat --models` is that action for the default folder: it uses Chroma's own downloader, which checks the archive's pinned SHA-256, and then runs the checks below on the result.

The adapter validates the expected BERT MiniLM metadata and WordPiece vocabulary agreement, including the fixed special-token IDs. The [upstream model configuration](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/raw/main/config.json) identifies the 384-dimensional, six-layer, 30,522-token model. These checks detect missing/incompatible/corrupt content; they are not signed per-file provenance or a sandbox for arbitrary ONNX models. Reuse trusted existing MiniLM assets. A lookalike model with altered weights must not be substituted under the unchanged default identity.

The native tokenizer and ONNX session initialize only for an actual embedding request. Tokenizer truncation/padding remains 256 tokens; the installed upstream attention-weighted mean pooling and float32 L2 normalization remain unchanged. Native sessions receive model bytes and use only CPUExecutionProvider with no optimized-model output path. GPU/CoreML/TensorRT disk caches or provider fallback are not selected. Separate existing-weight CPU acceptance verified finite normalized/repeatable vectors and unchanged source assets. Complete native filesystem behavior on the actual protected mount remains unverified; this change does not claim full application/network confinement or encryption.

## Lazy collection and backup compatibility

`CachedLocalDefaultEmbeddingFunction(model_path=None, *, root=None, engine_factory=None)` is an `EmbeddingFunction` wrapper, rather than a subclass of Chroma's `DefaultEmbeddingFunction`. This distinction matters: the reviewed collection implementation bypasses subclasses of its default function and may reconstruct the persisted download-capable default instead.

`default_collection_options(...)` supplies two separate objects: the safe runtime wrapper in `embedding_function`, and Chroma's inert built-in default in `configuration.embedding_function`. Chroma serializes the unchanged built-in default configuration while retaining Altera's explicit runtime wrapper. This avoids replacing its global default registry class. `MemoryStore` uses these options for initial collection handles and default snapshot restore. It refuses unsupported persisted custom-model identities before silently changing their vector semantics; legacy/default and the named ONNX MiniLM identity remain supported. Explicit global clear/Forget can still delete an unsupported collection through the narrowly typed compatibility error, without embedding or relabelling its vectors.

Wrapper construction, `name`, `get_config`, config reconstruction and collection metadata operations do not resolve model settings, inspect assets, initialize native sessions or embed documents. Listing, pin/priority metadata updates, export/snapshot, deletion/clear and restoration of saved vectors remain possible without assets or inference. Snapshot identity stays `default`; restore supplies stored vectors explicitly and preserves IDs/content/metadata without re-embedding. Existing explicitly injected custom test/model handles retain their separate snapshot identity.

Each `MemoryStore` serializes its own lazy client and collection initialization, including compatibility checks, so concurrent callers do not publish competing handles. An initialization error leaves the corresponding handle unset and a later call may retry. This coordination does not replace the application's maintenance gate or prove Chroma's native filesystem behavior on an encrypted mount.

This is a guarantee for Altera's operational `MemoryStore` handles and its document query/add/update APIs. A later raw Chroma `client.get_collection()` without the explicit safe wrapper can reconstruct its ordinary default; schema-specific metadata-vector APIs can also have different embedding dispatch. Altera does not expose those paths here, and no vendor/global registry monkeypatch is installed. New Chroma usage or dependency upgrades must retain and recheck this boundary.

## Memory and concurrency

One process-local lock serializes initialization and embedding, preventing overlapping native work or duplicate engine creation. At most two native engines are retained, keyed by approved source path, six-file identity/size/timestamps and the engine factory. Every embedding rechecks asset metadata; a changed source is revalidated before a new engine is cached. The cache retains model/tokenizer objects, not document text or embedding results. `clear_embedding_cache()` releases these engine references without touching disk.

An embedding request accepts at most 256 text documents and 1 MiB of valid UTF-8 input. The upstream forward pass batches at 32. Errors contain fixed instructions without document content or private paths, and a failed inference engine is evicted. Model memory, request/exception frames, ONNX native buffers, process paging and crash dumps cannot be promised zeroized; worker shutdown and browser and device protection remain separate responsibilities.

## Validation

`test_local_embeddings.py` denies HTTP, archive extraction and upstream download/default embedding calls. It exercises lazy metadata identity, missing/corrupt/incompatible assets, relative source resolution, unchanged source bytes, bounded cache/concurrency/input, safe load/inference errors and the CPU settings. A fake tokenizer/session compares the reused upstream numerical pipeline without loading or running real weights.

Native Chroma tests use temporary synthetic collections and saved vectors to verify legacy default reopen, exact snapshot/restore, model-free metadata, refusal of a different persisted model, explicit safe runtime dispatch for add/query/update, and unchanged global default registry identity. Existing custom no-model backup regression remains supported. No test downloads, opens personal model caches, installs a runtime or runs actual model weights. An approved local asset source can be exercised once by an explicit isolated operator acceptance run before widening the real chat path.
