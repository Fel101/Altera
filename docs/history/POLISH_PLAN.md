# Altera polish goal

Agreed October 26, 2026. Work in this order; preserve existing local changes and personal data.

This goal is complete. The [companion roadmap approved October 27](COMPANION_ROADMAP.md) is the active goal and supersedes earlier feature deferrals where stated. Publishing and remote CI remain deferred by the owner; new work is committed locally.

- [x] 1. Reliable replies: explicit persistence/failure states, reconnect and deduplicated retries, consistent request and memory validation.
- [x] 2. Conversation controls: Stop and Regenerate, followed by Copy, Edit and Delete; preserve the previous reply on failure and avoid duplicate extraction.
- [x] 3. Versioned full backup/restore: consistent snapshots, validation/preview and a recovery backup before replacement.
- [x] 4. Companion-scoped remember/correct/forget with sources and explicit handling of derived notes.
- [x] 5. Shared Windows startup, stable paths, rotating logs, accurate health, accessibility, documentation, lint and CI.
- [x] 6. Local diagnostics: provider/model, latency, first text, token/context estimates, compaction and reported paid usage where available.

Cloud fallback must remain explicit and visible. Semantic conversation search and unattended proactive check-ins are deferred.

## Validation

Use isolated stores and fake models. Test interruption, replay, failures, companion isolation and restore rollback rather than making real inference requests or altering the running app's data. Do not restart live services automatically.

## Startup, interface and diagnostics

- `core/app.py:create_app` is the shared factory. Normal `altera.py` / `start.bat` startup uses one Waitress process, eight threads and the configured host/port. Factory/script initialization prepares schema, FTS and interrupted replies. Data resolves from the instance folder (`ALTERA_HOME` optional), independent of CWD; profile writes are atomic UTF-8. Signing-key failure stops startup instead of silently generating an unstable key.
- Logs rotate in `data/her.log` at 5 MiB plus three previous files, including voice child output. Model health lists metadata with a bounded timeout and explicitly does not claim inference was tested. Memory health counts stored records and shows paused knowledge. Voice honors the saved preference and exposes enabled cloud fallback. Connection status means Altera is reachable.
- Keyboard focus is contained in custom dialogs and restored to the opener. Escape closes only the front dialog. Closed Settings and the phone sidebar are inert. Theme/accent controls are native buttons with names/states. Voice/provider failures stay visible. The PWA caches application assets only; upgrades purge old caches, and generated audio bypasses both PWA and browser caches.
- Root README, portable starter config, corrected documentation links/configuration examples, line-ending policy, Ruff application checks and GitHub Actions Windows/Linux checks are included. CI has not been dispatched remotely; the equivalent local checks are run here. The unused encryption setting was removed; storage/archives remain unencrypted. No license or publish action was inferred.
- Reply diagnostics stores per-request model/call information, worker timing, provider-reported usage/cost and labeled character-based estimates. Replays reuse the same record. Missing/partial usage is labeled; no price table or implicit zero cost is used. The panel shows context source, selection/reserve and notebook compaction/boundaries without prompting a model. See [coverage and limits](../DIAGNOSTICS.md).
- Browser checks use a disposable fake-model instance: Settings focus wrap, nested-dialog Escape/focus return, backup/diagnostics, readable status and phone-width overflow. Production services were not restarted. One earlier CWD subprocess test briefly initialized the checkout database before its isolation harness was corrected; a read-only audit found no active reply records. Subsequent subprocess tests set `ALTERA_HOME` before imports.
- Polish completion validation: **551 Python tests passed**, **12 Node browser-logic checks passed**, and **Ruff passed**. The subsequent [code review](CODE_REVIEW_2026-10-26.md) records later fixes and verification. One existing OpenTelemetry dependency deprecation remains. Root/user/architecture/diagnostics/progress documentation has no broken local file links. A real isolated Waitress process verified out-of-folder startup and preserved Tailscale header checks. Browser QA covered desktop and 390 × 844 phone layout with no horizontal overflow; screenshots are in ignored `data/polish-qa/`.
- QA preview process and browser tab are closed. Automatic approval review rejected deleting the final disposable preview folder (`her-polish-qa-ve7rc74a` under the Windows temporary directory), with only “blocked by policy”; it was left in place and no alternate deletion method was tried.

### Remaining limits

One application process is supported. ZIP backups are unencrypted and retained until managed by the owner. Diagnostics are a debugging view, not account-wide billing; background notes, extraction, speech and image jobs are outside the reply totals. Live model/voice integration was not exercised during this polish. Deployment requires restarting the existing app when convenient. Semantic conversation search and unattended proactive check-ins remain deferred as agreed.

## Earlier phase records

The sections below retain validation counts and observations from each implementation phase. The completion status above supersedes the plans that were pending at those checkpoints. The subsequent [code review](CODE_REVIEW_2026-10-26.md) records later fixes and verification.

- Baseline: 450 tests passed (one existing dependency deprecation warning).
- Reliability: durable request IDs and reply/event records; one active reply per conversation; browser reconnect retrieves existing work. Background generation survives browser disconnects. A process restart marks unfinished work interrupted. Failures retain partial text and never acknowledge persistence.
- Text and voice share the same stream parser. Pending requests are kept separately per conversation in the browser session. Offline retries reuse their saved IDs. Completed offline items are removed.
- Added a global write-origin guard alongside the existing Tailscale identity checks. Manual memory input rejects blank/non-text content and invalid/non-finite confidence; memory edits preserve ownership and update in place.
- Conversation deletion and full-data clearing remove reply/event records too, and reject clearing an active reply. Partial replies remain visible after reload and are excluded from model conversation history.
- Validation: 473 Python tests passed, plus five Node stream/parser/reconnect checks. Full visual QA is reserved for the interface phase. The running app has not been restarted.

## Reliable chat API

Send a stable `request_id` (8–100 letters, numbers, underscores or hyphens) with `POST /api/chat/stream` or `POST /api/chat`. Reuse the same payload and ID after a transport failure. Reusing an ID for different input returns 409. `GET /api/chat/requests/<request_id>` retrieves the durable state. The stream reports `accepted`, text/audio events, and then `saved`, `failed` or `interrupted`; only `saved` confirms an assistant message ID. JSON callers receive 503 for failed/interrupted work. Legacy callers without request IDs retain their previous response shape.

The reply worker is designed for one application process (including Waitress's thread workers). Multiple independent WSGI processes are not supported. A failed request is retained rather than automatically repeating inference. Try again / Regenerate makes an explicit new request using the original user message.

## Conversation controls

- Stop closes the request's owned model client/stream and records the partial draft as interrupted. Stopping after persistence returns saved and keeps the completed answer.
- Regenerate replaces the selected assistant message in place, using the conversation before that answer, and **keeps the answer it replaces** (`message_versions`, with the message's own id as the first version). Earlier answers can be shown again from the message's controls; the one showing is the one history, memories and notes use. Later messages remain as written. A failed or stopped replacement preserves the old answer and adds no version. Greetings and the latest unanswered user message can also be retried. User messages and extracted memories are not duplicated.
- Copy omits internal picture markers. Edit uses an inline editor (Escape cancels; Ctrl/Cmd+Enter saves). Delete removes the selected message while retaining later messages. A message with more than one answer shows a small "◀ Earlier / Later ▶" switcher with a "2 of 3" count.
- Editing/deleting removes related replay text/events and the earlier answers kept for that message (a hand edit is a correction, not another answer). Opaque request-ID tombstones reject stale retries with 410, so a browser cannot accidentally re-create deleted content.
- Changes invalidate old derived notes, memories and the compaction cutoff for that conversation. Those sources are excluded from replies pending source-based repair. Rebuild controls were added in phase 4. Showing an earlier answer marks the same review/rebuild.
- PWA cache v92 includes the new controls so installed clients receive the updated interface.

API: `POST /api/chat/regenerate` accepts `session_id`, optional `message_id` (latest by default), and a new stable `request_id`. `POST /api/chat/requests/<request_id>/stop` requests cancellation. `GET /api/messages/item/<id>` reads one saved message. `GET /api/messages/<id>/versions` lists the kept answers and `POST /api/messages/<id>/versions/<version_id>` shows one. `PATCH /api/messages/<id>` accepts `session_id` and `content`; `DELETE /api/messages/<id>` requires `session_id` in JSON or the query string. Mutations during an active reply return 409.

Validation: 486 Python tests and seven Node checks pass. Isolated browser verification covered regeneration, preserving the previous answer on Stop, inline editing, retaining a partial new reply, and Try again without a duplicate user row. Preview receipts/screenshots are in ignored `data/polish-qa/`; `scripts/preview_polish.py` reproduces a fake-model preview using temporary data and blocking model clients. Production services were not restarted. Desktop geometry was checked at 1440 px and the mobile controls inspected at 363 px. Additional mobile viewport/keyboard layout polish remains in phase 5.

## Backup and restore

Version 1 archives cover SQLite rows/reply state, vectors with embeddings, companion files/notes/photos, workspace files, preferences and this browser’s unsent drafts. Request/background leases prevent application writers during a snapshot or replacement. Restore validates paths, hashes, sizes, schema and companion rules before replacement, imports into application-owned SQL schema, rebuilds FTS and saves a recovery ZIP first. API keys, remote access and tool permissions are retained; importing model/voice connections requires explicit opt-in. Adult mode returns to off.

Database, vector, preference and filesystem failures trigger rollback. A persistent restore journal keeps the app paused after failed rollback or an interrupted restore; a standalone recovery page can restore the marked recovery archive even when ordinary pages are unavailable. Old browser requests are retired, mutations require a fresh dataset identifier after restore, and restored drafts require explicit review. PWA v15 caches static assets only so navigation cannot replay an old dataset identifier from cached HTML.

Validation: the full Python suite passed **504 tests**. Follow-up backup/blueprint checks passed **22 tests**, including native Chroma round-trip without an embedding call; the only remaining warning is from the existing OpenTelemetry dependency. **11 Node checks** cover stream persistence, controls, stale drafts, epoch headers and fresh navigation. Isolated browser QA restored an edited sample chat and retained its unsent draft without sending it, verified the acknowledgment gate, and checked the updated dialog. No production service was restarted. See [Backup and restore](../BACKUP_AND_RESTORE.md) for the workflow, API and limits, including the current absence of automatic archive/staging retention.

The phone check also fixed Settings immediately closing from the bottom navigation and the sidebar covering the panel. Backup access was verified at 390 px, with a 354 px dialog and no horizontal overflow. Browser drafts were explicitly moved back into the composer without sending; no console errors were recorded. Preview servers were stopped and temporary workspaces cleaned by the helper.

## Companion memory controls

The Memory page now has explicit companion selection, Remember/Correct/Forget, source inspection, scoped clearing and duplicate review. Chat messages have Remember and a Review memories link after edits/deletion/replacement. New automatic facts and repeated confirmations retain source IDs and hashes. Older untracked facts remain visible for review, and changed/unavailable sources cannot be recalled. Corrections become user assertions rather than fabricated source links.

Correction and forgetting clear notebook files/history and create a recall boundary; earlier conversation remains visible but is excluded from prompts and subsequent notebook rebuilding. Other saved memories are retained. Repair rebuilds automatic facts from eligible current messages while preserving added/corrected entries and valid memories retained before a boundary. Stale manual copies cannot swallow current facts through duplicate detection. Notebook provenance records conversation ranges and retains flags for user edits and earlier untracked notes.

Session splits now retain owner, IDs and timestamps atomically, so source links follow them. Merges reject different companions. Both operations retire stale replay records and invalidate derived knowledge. Deleting the last conversation keeps owner-level knowledge paused until repair. Mutations coordinate with requests, reply workers and note writers; failures keep knowledge paused.

Validation for this phase is recorded below. See [Memory controls](../MEMORY_CONTROLS.md) for behavior, API and the distinction between forgetting a memory and erasing conversations/backups. Phase 5 subsequently completed startup, paths, logging, health, accessibility, docs and CI; phase 6 added diagnostics.

Validation: **532 Python tests passed**, including 27 memory-control cases, native vector snapshot/restore and a backup round trip preserving source links and recall boundaries. **11 Node checks passed** and the memory script passed syntax validation. Isolated browser checks exercised Remember from chat, direct manual creation, companion selection, Correct/Forget, source edits, rebuilding current facts while retaining stale manual copies for review, and legacy-source rebuilding. Phone layout was inspected at 390 px with no horizontal overflow. Console error checks were empty. Screenshots are in ignored `data/polish-qa/memory-controls.jpg` and `memory-mobile.jpg`; the preview helper now includes synthetic tracked/legacy memories for reproducing these checks. Preview processes were stopped and their temporary data cleaned. The production service and personal data were not changed.

Regression work also fixed a test that deleted the real MemoryStore collection property after substituting a fake, and replaced shallow memory CRUD stubs with isolated collection fixtures. Tests do not open the developer’s live store or call real inference services.
