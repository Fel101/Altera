# Code Review — Her

**Date:** 2026-09-21
**Reviewer:** Senior engineering review (Claude Opus 5)
**Commit at review:** `29b9a04` — *Remove POST /api/events/extract and the events blueprint*

**Scope:** ~7,000 lines of application code (`her.py`, `core/`, `static/js/`, `templates/`) plus ~4,800 lines of tests.
Every core module was read; significant findings were verified by executing code, not by inspection alone.

**Test suite at time of review:** `362 passed, 1 warning in 27.87s` — clean.

---

## Executive summary

The domain layer is genuinely good. `core/safety.py`, `core/notes.py`, `core/photos.py` and `core/adult.py` are the work
of someone who thought hard about failure modes: layered enforcement, atomic writes, history retention, fail-closed
behaviour on the safety path, and a `conftest.py` that makes it *impossible* for a test to accidentally reach a real
model. That is better than most codebases of this size.

The problems are concentrated in the HTTP layer and in the seams between the refactor "sessions". **Four things are
broken in production right now**, and one is a real security hole:

| Severity | Finding |
|---|---|
| 🔴 | Conversation search returns nothing, ever — the FTS index is never populated |
| 🔴 | All four `/api/memory/*` CRUD endpoints 500 — `get_memory_manager` is never imported |
| 🔴 | The input sanitiser strips every hyphen from every message before it is saved |
| 🔴 | 20 error paths return HTTP 200 with a JSON *array* body |
| 🔴 | Unauthenticated arbitrary file write into the project root → code execution on restart |
| 🔴 | Stored XSS on the search page, same-origin with the file-write endpoint |

---

## 1. Architecture & Design

### 🔴 Three separate `ChatEngine` singletons, and the one that serves chat is never refreshed

There are three independent `_chat_engine_instance` globals: `her.py:108`, `core/routes/core_api.py:25`,
`core/chat.py:601`. `/api/llm/switch` calls `her._refresh_chat_engine()` (`core/routes/llm.py:124`), which refreshes
her.py's copy and core/chat's copy — but `/api/chat` and `/api/chat/stream` serve from **core_api's** copy, which
nothing ever refreshes.

Model switching *appears* to work only by accident: `ChatEngine.llm_client` delegates to the `LLMClient` singleton,
which does get refreshed. But the stale engine keeps its stale `self.config` (memory `top_k`, notes settings,
web-search config) and a stale `_supports_tools` cache for the life of the process.

**Fix:** collapse to one engine owned by `core/chat.py`. `her.py:111-131` is dead code otherwise.

### 🟠 `config.yaml` is loaded in 13 places with 3 different path strategies

Import-time in `pages.py:11`, `engagement.py:13`, `search_export.py:14`, `her.py:53`; per-request in `admin.py:162`;
and **per chat turn** in `core_api.py:144` — every streamed message re-reads and re-parses the YAML from disk.

Worse, `core/llm.py:64`, `core/tts.py:58` and `core_api.py:34` use a bare relative `open("config.yaml")`, which breaks
if the app is launched from any other directory, while others correctly resolve against the project root. Since
`ProviderRegistry._save_to_config()` rewrites the file at runtime, the import-time copies go permanently stale.

**Fix:** one config module owning a lazily-loaded, explicitly-invalidated config object. A `core/config.py` already
exists (23 lines) but is not used for this.

### 🟠 CSRF protection exists in exactly 2 of 13 blueprints

`_cross_site()` is defined **twice, verbatim** (`companion.py:24`, `companions.py:23`) and applied only within those
two files. `/api/clear-data`, `/api/sessions/<id>` DELETE, `/api/memory/clear` and all of `/api/tools/*` have none.

The practical risk is limited (a JSON content-type forces a CORS preflight), but the inconsistency is the tell — this
belongs in a single `@app.before_request`.

### 🟡 Import-time side effects

`core/memory.py:507` constructs `MemoryManager()` at import, which `os.makedirs()` the Chroma directory.
`core/search.py:65` constructs `ConversationSearch()` at import, which opens the DB and attempts an FTS rebuild.
Both run relative to the current working directory, on any `import core.*`, including in tooling.

---

## 2. Edge Cases & Logic

### 🔴 Conversation search returns nothing. Ever. *(verified against the real database)*

The FTS5 index is **never populated**. In `core/datastore.py:1024-1029`:

```python
msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
fts_count = conn.execute(f"SELECT COUNT(*) FROM {fts_table_name}").fetchone()[0]
if fts_count != msg_count:   # never true
```

`messages_fts` is an **external-content** table (`content='messages'`), so `COUNT(*)` on it delegates to `messages`
and the two counts are *always* equal. The rebuild branch is unreachable, and nothing else ever writes to the index —
`save_message()` has no FTS hook and there are no triggers.

Measured against a copy of `data/conversations.db`:

```
messages: 38   COUNT(*) on messages_fts: 38   -> rebuild branch taken? False
rows actually in the FTS index storage (messages_fts_data): 2   (i.e. empty)
```

A fresh-database test confirms a message indexed before *and* after `ensure_fts_table()` both return 0 hits.

**Fix:** use `INSERT INTO messages_fts(messages_fts) VALUES('rebuild')` for the backfill, add the three standard
external-content triggers (`AFTER INSERT/UPDATE/DELETE ON messages`), and drop the count heuristic.

Note `datastore.py:1261` declares the table without `content_rowid` while `datastore.py:1020` declares it *with* —
`IF NOT EXISTS` means the first one wins and the second never applies. (They are semantically equivalent, since
`content_rowid` defaults to `rowid`, but the divergence is a trap.)

### 🔴 All four memory CRUD endpoints 500 — `get_memory_manager` is never imported

`core/routes/core_api.py` calls it at lines 251, 266, 291, 303 but never imports or defines it. Verified live:

```
DELETE /api/memory/abc123
[Error] Unhandled exception: name 'get_memory_manager' is not defined
500
```

`POST /api/memory`, `PUT/DELETE /api/memory/<id>` and `POST /api/memory/clear` are all dead. The catch-all
`@app.errorhandler` swallows it into a generic 500, which is why it went unnoticed. One-line fix; the real lesson is
the test gap (§5).

### 🔴 The input sanitiser strips every hyphen from every message

`her.py:157`, `her.py:170`, `core/routes/core_api.py:55` — all three copies:

```python
re.sub(r'[-\b\u000b\f\u000e-\u001f]', '', stripped)
```

A leading `-` in a character class is a **literal hyphen**, not a range. Verified:

```
'well-known e-mail 2024-01-01'  ->  'wellknown email 20240101'
NUL stripped? 'a\x00b'   # no — the one thing it should strip survives
```

Every hyphenated word, date and negative number the user types is silently corrupted before reaching the model *and*
before being written to the database. The intent was clearly `[\x00-\x08\x0b\x0c\x0e-\x1f]`; the `\x00` was lost and
`\x08` collapsed into a stray `-`.

### 🔴 Error responses return HTTP 200 with a JSON array body — 20 call sites

`sanitized_error_response()` returns a `(dict, status)` **tuple** (`core/constants.py:141`). Twenty live call sites do
`return jsonify(sanitized_error_response(...))`, which serialises the tuple:

```
STATUS: 200
BODY:   [{"code":"internal_error","error":"An internal error ..."},500]
```

Clients see success, and `response.error` is `undefined`.

Affected: `sessions.py:314`, `admin.py:78`, all 6 in `jokes_opinions.py`, all 6 in `search_export.py`, all 6 in
`personality.py`, all 3 in `relationships.py`. `companion.py` and `llm.py` do it correctly (`[0]`, then status) —
that is the pattern to propagate. Better still: make the helper return a ready `Response` so it cannot be misused.

### 🟠 `include_archived=true` returns *only* archived sessions

`sessions.py:31-34` passes the flag straight through as `archived=`, but `list_sessions(archived=True)` filters to
`WHERE archived=1`. Verified — every returned row had `"archived":true`. The parameter means "include", the behaviour
is "exclusively". `DataStore.list_sessions` already accepts `None` for "all"; pass `None if include_archived else False`.

### 🟠 Session merge silently destroys messages past 1000

`sessions.py:180` copies with `get_messages(target, limit=1000)`, then `sessions.py:188` calls
`delete_messages_by_session(target)` — which deletes *all* of them. Merging a 1,500-message session loses 500 messages
and returns `{"status":"ok"}`. Either paginate, or move rows in SQL — `DataStore.move_messages` already exists and does
exactly this.

### 🟠 Streaming is not streaming whenever tools are enabled

`core/llm.py:169-174`: when `tools` and `tool_executor` are both set, the "stream" is a generator that yields the
**entire blocking response** in one chunk. And `_detect_function_calling_support()` (`core/chat.py:441`) returns `True`
for any model that does not raise — it never inspects the response for tool-call support, despite the docstring.

So for essentially every model, the user watches a typing indicator for the full generation, then receives the whole
reply at once. The sentence-by-sentence TTS pipeline in `SpeechStream` is defeated at the same time.

### 🟡 Smaller logic issues

- **`_calculate_similarity`** (`core/memory.py:286`) averages `mem1["similarity"]` and `mem2["similarity"]` — those are
  each memory's similarity to *some unrelated earlier query*, not to each other. When both keys are set (i.e. the
  memories came from `search()`), the dedup answer is meaningless.
- **Memory IDs collide**: `f"mem_{int(timestamp())}_{hash(content) % 10000}"` (`core/memory.py:49`) — 1-second
  resolution × 10⁴ buckets, and `hash()` is per-process randomised so IDs are not reproducible. Use `uuid4()`.
- **FTS query syntax errors reach the user as a 500**: searching `"` or `foo)` raises
  `OperationalError: unterminated string`. Escape the query or wrap it as a quoted phrase.
- **`export_single_session`** (`sessions.py:263`) calls `datetime.fromisoformat()` on the raw timestamp — an empty one
  raises and 500s the export.
- **`auto_name_session`** (`sessions.py:330`): `range(len(words) - 3)` never enters for messages under 4 words, so
  short sessions always get "Conversation".
- **`import_markdown_to_session`** does not verify the session exists before writing messages into it.
- **`test_connection`** (`routes/llm.py:179`): `api_key or provider.api_key if provider else None` parses as
  `(api_key or provider.api_key) if provider else None` — a user-supplied key is discarded when the provider is unknown.
- **`update_message_mood` is defined twice** in `DataStore` (`datastore.py:588` and `datastore.py:972`) with different
  return types. The first is dead.

---

## 3. Performance & Scalability

### 🔴 No `MAX_CONTENT_LENGTH` anywhere — unbounded uploads

`/api/voice/stt` (`routes/engagement.py:197`) does `request.get_data()` or `file.read()` with no size cap, straight
into memory. Same for `/api/sessions/<id>/import-markdown`. One `app.config["MAX_CONTENT_LENGTH"]` fixes both.

### 🟠 Whisper reloads the entire model on every transcription

`core/stt.py:27`: `model = WhisperModel(model_size, device="cpu", compute_type="int8")` is constructed inside the
function — the comment directly above it says "Load model on first call (cached after)", and it is not. Every voice
message pays full model-init latency. Cache in a module-level dict keyed by `model_size`.

### 🟠 Every chat turn scans the entire photo directory

`PhotoService._all()` (`core/photos.py:386`) globs `p-*.json` and parses **every** record. It is called by
`list_premade()`, `gallery()`, `premade_keys()`, `find_match()` and `import_premade()` — and `build_context()` calls
`list_premade()` + `rank_premade()` on **every single message**. At a few hundred photos that is a few hundred file
reads and JSON parses per turn, on the request path. An in-memory index invalidated on write removes it entirely.

### 🟠 Memory deduplication is O(n²) with set-building inside the inner loop

`find_duplicates()` (`core/memory.py:245`) compares every pair within a type group, and `_calculate_similarity`
rebuilds two word sets per comparison. `/api/memory/dedup` is a synchronous GET. At 5,000 memories that is ~12M set
constructions inside a request handler. Embeddings are already stored in Chroma — ask it for neighbours instead.

### 🟡 Other

- **No index on `messages(session_id)`** — the only index in the schema is `idx_sessions_avatar`
  (`datastore.py:1307`). Every message query does a full scan. Add `idx_messages_session ON messages(session_id, id)`.
- **`list_avatar_sessions`** (`datastore.py:512`) loads *all* sessions then filters in Python; `avatar_id` is already
  indexed. Use a `WHERE`.
- **`get_user_profile`** (`datastore.py:214`) opens a fresh SQLite connection per key — 9 connections per call.
- **`DataStore._connect()`** opens/closes a connection and re-issues `PRAGMA journal_mode=WAL` on *every* method call.
  A thread-local connection would be cheap and correct.
- **`_chat_with_retry`** (`core/llm.py:186`) retries all exceptions including permanent ones (bad model name, HTTP 400),
  burning 6s of sleep before failing.

---

## 4. Security

### 🔴 Unauthenticated arbitrary file write into the project root → code execution

`/api/tools/file` POST (`core/routes/agent_tools.py:110`) writes any UTF-8 text to any path under `PROJECT_ROOT`, with
no authentication, no CSRF check, and no extension filtering. The path-traversal guard is correct — but it is the wrong
guard. Verified live:

```
POST /api/tools/file  {"path":"core/routes/pwned.py","content":"# proof"}
200 {"path":"core/routes/pwned.py","size":7,"status":"ok"}
```

(The file was deleted immediately after.) Writing into `core/routes/` or overwriting `her.py` / `conftest.py` gives
arbitrary code execution on the next restart. `IGNORED_DIRS` is applied only to *listing*, not to read or write. GET on
the same route reads `config.yaml` — including any API keys — with no auth.

Binding to `127.0.0.1` limits this to local origins, but "local" includes every page open in the browser and every
other process on the machine.

**Recommendations:** restrict writes to an explicit allowlisted subdirectory (e.g. `workspace/`), refuse `.py` / `.yaml`
/ `.json` config paths outright, require the `_cross_site()` check, and put the whole blueprint behind a config flag
that defaults to off.

### 🔴 Stored XSS on the search page

`templates/search.html:398`:

```js
html += `<div class="result-content">${content}</div>`;   // content = result.content, unescaped
container.innerHTML = html;
```

`result.mood` and `result.role` (lines 390-391) are unescaped too. Message content is fully attacker-influenced — via
anything the user pastes, and via anything the *model* emits, which matters when the model is summarising fetched web
pages through the `web_search` tool.

Every other renderer in the codebase escapes correctly (`app.js:formatMessage`, `knowledge.html`, `memory.html`,
`timeline.html`), so this is a single miss — but it is same-origin with `/api/tools/file`, which turns a chat message
into code execution. Escape before applying the highlight regex, then re-apply the highlight to escaped text.

### 🟠 SSRF: `/api/tools/fetch` will fetch any URL

`core/routes/agent_tools.py:135` validates only the scheme. No blocklist for loopback, RFC1918, or link-local
(`169.254.169.254`), and no redirect restriction. Unauthenticated, and it returns the response body to the caller —
usable to probe LM Studio, ComfyUI, NPC Forge and anything else on the LAN. Resolve the host and reject
private/loopback ranges before connecting, and disable redirects.

### 🟠 API keys get written into a version-controlled file

`ProviderRegistry._save_to_config()` (`core/providers.py:196`) serialises every provider's `api_key` into
`config.yaml`, and `PUT /api/llm/config` (`routes/llm.py:196`) accepts one over HTTP. `config.yaml` is **tracked in
git** — `.gitignore` covers `user_profile.yaml`, `data/` and `config.local.yaml` but not `config.yaml`. The moment
someone enters an OpenRouter or SerpAPI key in the UI, it is staged for commit.

Either move keys to `.env` (`python-dotenv` is already loaded) or write them to the already-ignored
`config.local.yaml`.

`config.yaml` also currently commits a machine-specific absolute path:
`npc_forge_library: 'C:\Users\<user>\ComfyUI-Shared\...'`.

### 🟡 Lower severity

- **`app.secret_key = os.urandom(24).hex()`** (`her.py:64`) regenerates on every start, silently invalidating every
  session cookie on restart. Load from env with a persisted fallback.
- **`requirements.txt` is incomplete** — `PyYAML`, `requests`, `urllib3` and `werkzeug` are all imported directly but
  unlisted. They install today anyway, as dependencies of `chromadb`, `huggingface_hub` and `flask`, so a fresh install
  does currently work — but that means whether Her starts is decided by what a vector database happens to depend on.
  No versions are pinned either (`>=` only), so builds are not reproducible.
- **STT leaks internals**: `core/stt.py` returns `str(e)` and `routes/engagement.py:216` forwards it to the client —
  raw exception text and filesystem paths, contradicting the project's own sanitised-error policy.
- **`/api/voice/tts`** accepts unbounded `text` with no length cap.
- **`_cross_site()` fails open** for clients that send neither `Origin` nor `Sec-Fetch-Site`. Acceptable for browsers;
  worth knowing.

### On the safety-critical path specifically

`core/safety.py` + `core/age_check.py` + the tier gates are well-designed: code-enforced invariants that the YAML tiers
cannot loosen, a vision-model second layer, fail-closed at suggestive-and-above when the checker cannot answer, and
blocked images never written to disk. **No bypass was found.**

One structural note: `adult.tiers()` reads `_cache["needs"]` outside `_cache_lock` in `tier_problem()` — a benign race
today, but a two-line fix.

---

## 5. Clean Code

### Naming and duplication

- `_cross_site()` and `_forbidden()` are duplicated verbatim across two route modules → move to a shared guards module.
- `validate_message_input` exists twice (`her.py:148`, `core_api.py:46`) with **different return shapes** — her.py's
  returns a 3-tuple on error and a 2-tuple on success, which would raise `ValueError` on unpack. It is dead code today,
  which is the only reason it is not a live bug.
- `validate_text_input` is a byte-for-byte copy of `validate_message_input` with "Message" → "Text".
- In `search_export.py:27` the route function `conversation_search()` shadows the module singleton
  `conversation_search` it then re-imports locally. It works; it is confusing.
- `her.py:138` defines `get_memory_manager()`, shadowing the identically-named import at `her.py:26`.

### Dead code

- `her.py:111-131` (`_get_chat_engine`; `_refresh_chat_engine` is called but the instance it manages is unused),
  `her.py:148-171` (both validators).
- First `DataStore.update_message_mood` (`datastore.py:588`).
- `memories_fts` table in `SCHEMA_SQL` (`datastore.py:1244`) — created, never read or written.
- `import numpy as np` and `import json` in `core/memory.py` — unused.
- `config.yaml.before-qwen` and `server.log` are in the working tree.

### Consistency

- Logging is split between `print()` (`her.py:181`, `sessions.py:313`, `search_export.py:67`, `memory.py:378`) and
  `LOGGER` (everywhere else). `LOG_FORMAT` / `LOG_LEVEL_DEFAULT` are defined in `constants.py` and never used —
  `logging.basicConfig()` is never called, so `LOGGER.debug` output is invisible and `LOGGER.exception` goes to stderr
  unformatted.
- Inline `from core.x import y` inside function bodies is used heavily to dodge circular imports. It works, but it is
  the symptom — `her.py` importing from `core.routes` while `core/routes/llm.py` imports back from `her` is the cycle
  to break.
- `photos_module._keys(scene)` (`core/chat.py:298`) reaches into another module's private function.
- `core/constants.py` defines `AppError` / `ValidationError` / `NotFoundError` and `LLM_MAX_TOKENS_*`,
  `MAX_LLM_PROMPT_LENGTH`, `OFFLINE_QUEUE_MAX_SIZE` — none are used anywhere.

### Test coverage gap

362 tests, and coverage of companions / photos / notes / voice is genuinely strong. But *none* of the four blockers
above are covered: no test touches `/api/memory/*`, `include_archived`, the error-response shape, or asserts that a
saved message is findable by search.

That is the pattern worth fixing — the route layer is tested for happy-path construction (`test_blueprints.py`) but not
for behaviour.

---

## Remediation

| # | Fix | Status |
|---|---|---|
| 1 | FTS triggers + `'rebuild'` backfill — search is dead | ✅ fixed |
| 2 | Import `get_memory_manager` in `core_api.py` | ✅ fixed |
| 3 | Fix the sanitiser character class | ✅ fixed (5 copies, not 3) |
| 4 | Escape `result.content` in `search.html` | ✅ fixed |
| 5 | Lock down `/api/tools/*`, add SSRF guard | ✅ fixed |
| 6 | Error responses that return HTTP 200 | ✅ fixed (31 call sites) |
| 7 | `MAX_CONTENT_LENGTH` + cache the Whisper model | ✅ fixed |
| 8 | `include_archived` semantics + merge-session data loss | ✅ fixed |
| 9 | Single config owner; collapse the three chat engines | ✅ fixed |
| 10 | Photo-record in-memory index; `messages(session_id)` index | ✅ fixed |
| — | `requirements.txt` incomplete and unpinned | ✅ fixed |

Test suite: **406 passed**, stable across consecutive runs (was 362 — 44 new regression tests in
`test_review_regressions.py`, and one pre-existing flake fixed).

### What changed, item by item

**1 — search.** `core/datastore.py`: added `fts_schema_sql()`, which declares the index *and* the three
`AFTER INSERT/UPDATE/DELETE ON messages` triggers that keep it current, so a message is searchable the moment it is
saved. `ensure_fts_table()` now backfills pre-existing messages once with FTS5's `'rebuild'` command and records that
in a new `fts_state` table, instead of the unreachable count comparison; it also commits, which the old row-by-row
rebuild never did. Added `rebuild_fts_index()` for recovery, and `fts_match_query()`, which quotes each word so a
stray `"` or `)` in the search box is a search, not an `OperationalError`. Removed the dead `memories_fts` table.
*Verified on a copy of the real database: a real word went from 0 hits to 10.*

**2 — memory CRUD.** One import in `core/routes/core_api.py`.

**3 — the sanitiser.** `_CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')`, replacing all five copies
(two more were hiding in `core/routes/jokes_opinions.py`). Hyphens, dates and negative numbers survive; NUL and the
other control characters no longer do; tab, newline and carriage return are kept.

**4 — the XSS.** `templates/search.html` escapes content *before* highlighting, escapes the query to match the escaped
text, escapes `mood`/`role`, and reduces the mood class to a bare word so it cannot break out of the attribute.
*Verified in a browser: before, 4 payloads fired and injected 8 elements; after, none fired, 0 injected, and the
search-term highlighting still works.*

**5 — the tools surface.** `core/routes/agent_tools.py` rewritten: writes confined to `workspace/` (the RCE path is
gone — a `.py` file can no longer be dropped anywhere the app imports from), reads refuse `config.yaml`,
`user_profile.yaml`, `*.db`, `*.env`, `*.key`, `*.pem` and friends, every route refuses cross-site requests, the whole
surface can be switched off with `tools.enabled: false`, and `fetch` resolves the hostname and rejects loopback,
private, link-local, reserved and multicast addresses — on the initial URL *and* on every redirect hop.

**6 — error responses.** New `api_error()` in `core/constants.py` returns a ready Flask response, so the
`jsonify(tuple)` mistake is not expressible. 31 call sites converted (23 broken, 8 verbose-but-correct). A
`sanitized_error_response` docstring now explains the trap, and a test walks `core/routes/*.py` to stop it coming back.
Also fixed the catch-all handler in `her.py`, which was turning Flask's own 404/405/413 into 500s.

**7 — limits and the model reload.** `app.config["MAX_CONTENT_LENGTH"] = 25 MB`; `MAX_TTS_CHARS` on the TTS endpoint;
`core/stt.py` caches one `WhisperModel` per size behind a lock instead of rebuilding it per transcription, and returns
a fixed message instead of the exception text (which carried filesystem paths straight to the browser).

**8 — sessions.** `include_archived=true` now passes `archived=None` ("both"). Merge uses the existing
`DataStore.move_messages()` — one `UPDATE`, nothing capped, nothing dropped — and reports `messages_moved`.

**9 — one config, one chat engine.** `core/config.py` is now the only reader and writer of `config.yaml` and
`personality.yaml`: it caches by (mtime, size), re-reads when the file changes, and owns the atomic write, so a change
made through the UI is visible everywhere at once. All thirteen other readers now go through it, and a test walks
`core/` and `her.py` to make sure none comes back. `core/chat.py` owns the single `ChatEngine`; the copies in `her.py`
and `core/routes/core_api.py` are gone, along with the `her.py` ↔ `core/routes/llm.py` import cycle that caused them.
`her.py` also loses its dead duplicate validators and the shadowing `get_memory_manager`.

**10 — the photo store.** `PhotoService` keeps an in-memory index of records, rebuilt only when the folder actually
changes and updated in place by `_save`/`delete`. Measured with 400 photos: a chat turn's photo work went from
**56 ms to 3.1 ms (18×)**, with the index verified to agree with the disk after writes, deletes and external changes.
`idx_messages_session ON messages(session_id, id)` was added in item 1's schema work.

**requirements.** `PyYAML`, `requests`, `urllib3` and `werkzeug` declared; `requirements.lock.txt` added from the
working environment; the unused `numpy`/`json` imports dropped from `core/memory.py`. Verified by installing into a
clean venv and booting the app. A test asserts every directly imported package is declared.

### Found while fixing

- The catch-all handler in `her.py` was reporting every Flask HTTP error — 404, 405, 413 — as a 500.
- `NotesStore.write()` named history files by timestamp alone, so two rewrites in the same microsecond overwrote each
  other and a saved version was lost. This was also an intermittent test failure.
- `core/routes/core_api.py` swallowed voice-setup failures with a bare `except: pass`, so a broken voice configuration
  was indistinguishable from voice being switched off. It logs now — and caught a real bug within minutes.
- Two separate places relied on filesystem timestamps for correctness (the config cache and the photo index). Both now
  handle the case where a write lands in the same clock tick as the previous read.
- `conftest.py` gained a guard: every test gets its own copy of `config.yaml`/`personality.yaml`, so no test can write
  the developer's real config. While the file was opened by bare relative name this was true by accident; once the
  path was resolved properly, a provider-switch test rewrote the real file. (It did, once, during this work.)

### The lower-severity findings — all fixed

**Streaming was fake whenever tools were enabled.** `_chat_stream` now runs the tool rounds itself and keeps
streaming through them, reassembling each tool call from the fragments the API sends it. The reply reaches the reader
piece by piece again, which is also what the sentence-by-sentence speech needs. `_detect_function_calling_support`
claimed to detect model support and returned `True` for anything that did not raise; it is now
`_server_accepts_tools`, which says what it actually checks, is probed once under a lock, and is honest about it.

**API keys no longer reach the tracked file.** `save_config()` splits credentials out to `config.local.yaml`
(already git-ignored) and writes them blank to `config.yaml`; `get_config()` merges them back, so nothing downstream
notices. `HER_<PROVIDER>_API_KEY` overrides either, so a key need never be written to a file at all. Well-known local
placeholders (`lm-studio`, `ollama`, …) stay in the shareable config, and a blank key no longer stops the OpenAI
client being constructed.

**`app.secret_key`** comes from `HER_SECRET_KEY`, or a key generated once and kept in `data/secret_key`. Restarting
no longer signs the person out.

**Memory.** IDs are `uuid4` — a one-second timestamp plus 10⁴ buckets of a per-process-randomised `hash()` could
collide for two facts from one message, and ChromaDB would have kept only the last. `_calculate_similarity` no longer
averages each memory's similarity to some earlier, unrelated query. The dedup pass prepares each memory's text once
instead of re-splitting both sides on every comparison: **2.4× faster, and verified to find exactly the same groups**.
The growth is still quadratic — a short memory can match a longer one that contains it, so no cheap test rules a pair
out — so `/api/memory/dedup` now caps what it examines and says so in the response.

**Logging.** `basicConfig` was never called, so every `LOGGER.debug` went nowhere and every `LOGGER.exception`
reached stderr unformatted — which is why half the codebase had fallen back to `print()`. It is configured now
(`HER_LOG_LEVEL=DEBUG` for the streaming and picture traces), and the 54 stray `print()` diagnostics across 17 files
are logger calls, using `LOGGER.exception` inside `except` blocks so the traceback survives. Three tests guard it:
no `print()` in `core/`, logging is configured, and no log message carries a placeholder nothing fills.

### Corrections to this review

- **`core/events.py` is not dead.** It is used by `core/export.py`, `core/routes/search_export.py`, `her.py` and
  `test_model_helpers.py`. The §5 claim was wrong.
- **The `requirements.txt` severity was overstated** — see §4.
