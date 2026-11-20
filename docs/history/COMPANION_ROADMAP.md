# Altera companion roadmap

The current local release is tracked in the [ordinary release checklist](ORDINARY_RELEASE_ACCEPTANCE.md). The password vault and its historical verification are archived on a [dedicated branch](VAULT_RELEASE_MILESTONE.md).

The phases below preserve the agreed feature scope. The release checklist records which hardware and device checks remain open.

## 0. Release checkpoint and verification

- [x] Preserve the reviewed source in a local commit: `f2249d4`, on `codex/companion-roadmap`. All 142 files in the prior review's source receipt matched before the checkpoint; 132 changed source/documentation/test files were committed. Local configuration changes and unrelated `.claude` state were excluded.
- [x] Keep runtime configuration local, ship portable defaults, and verify startup from a fresh source checkout without private configuration. Focused startup/config checks: 24 passed; the live configuration remains on disk and is no longer tracked.
- [x] Verify the first milestone with isolated Python/Node checks and current documentation: 687 Python tests, 34 Node checks and Ruff passed. Re-run appropriate checks for subsequent changes.
- [x] Perform bounded real local model and speech smoke checks in isolated instances. Qwen passed streaming and Stop, and the cached local speech engines passed transcription, synthesis and replay. The [release checklist](ORDINARY_RELEASE_ACCEPTANCE.md) tracks the microphone and device checks separately.
- [ ] Activate the completed release safely when no work is running, then verify the running version. Do not force-stop unknown processes or repeat previously rejected stop/cleanup actions. Record any manual activation dependency explicitly.
- Remote CI: **deferred by the owner**, not required to complete this local goal. The existing workflow stays available as source for a later release.

The prior review passed 662 Python and 14 Node checks. Those are historical results, not acceptance evidence for new features. No production restart or real inference was part of that review.

## 1. Interruptible voice

- [x] Mic-button activation immediately silences playing/queued speech and suppresses later clips for that request.
- [x] Coordinate Stop before/after request acceptance, reconnect, and completed-text/unfinished-speech cases. Preserve saved answers and interrupted drafts.
- [x] Recording can begin immediately; sending the new transcript waits for the previous turn to settle. Preserve reviewable text on cancellation/network failures, including cancellation before the user message is saved.
- [x] Capture the recording's companion/session and dataset identity. Navigation or a changed dataset leaves the transcript for review; it cannot silently send into another companion's conversation.
- [x] Add optional automatic speech-triggered interruption with visible microphone state, an explicit tab-scoped opt-in, echo handling, preserved onset and a manual fallback. Installed Silero CPU inference was smoke-tested with synthetic silence; real microphone/echo acceptance remains below.
- [x] Automated tests cover late audio, duplicate/pre-acceptance stop, failed permission/storage/transport, consecutive voice replies, replay, navigation and dataset changes. Cancellation releases stream waiting while retaining the native speech writer's lease.
- [ ] Perform actual listening/microphone acceptance separately from fake-service tests.

## 2. Inspectable knowledge and uncertainty

- [x] A deterministic per-companion “What she knows about me” report with safe JSON and Markdown export.
- [x] Show saved content, origin, current/changed/unavailable sources, manual corrections, notebook provenance limits, recall boundaries and repair state.
- [x] Distinguish stored information and recall eligibility from actual prompt selection. This inventory does not capture the exact prompt sent for a turn, and does not use a generated summary as its evidence.
- [x] Label inferred or derived material; source integrity is not proof an extracted claim is true. Exclude another companion's data and machine settings.
- [x] Personal-fact/episodic prompts ask when evidence is missing, do not invent shared history, and preserve uncertainty. Notebook instructions distinguish assistant guesses and imagined scenes from user biography. Prompt tests cannot establish perfect hallucination prevention.
- [x] Verify ownership, stale sources, dirty knowledge, Forget, manual corrections, Unicode/HTML safety, export parity, and absence of inference side effects. Browser checks verified per-companion report switching and source details at 1440- and 390-pixel viewports with no horizontal overflow or captured console warnings/errors.

## 3. Asynchronous voice notes

- [x] Record, review/edit transcription, send once, and receive an explicitly replayable spoken reply with text available. Reply audio is retained by worker completion even with no browser polling; actual voice quality remains an acceptance dependency.
- [x] Persist upload/transcription/delivery state and audio attachments; use stable note/request IDs for retries and retrieve existing replies without duplicate user bubbles.
- [x] Support bounded offline audio storage, visible pending/error/review states, MIME capability detection, verified container/codec and five-minute/20 MiB limits, and explicit retry-safe retention/deletion.
- [x] Handle reload, connectivity changes, companion navigation, dataset replacement, partial failures and quotas without silent loss or automatic sending. Frozen owner/revision/recall/epoch context prevents delayed uploads from bypassing Forget or moving to another companion. Server backups round-trip recordings; browser-only originals remain separately downloadable, as documented in [Voice](../VOICE.md).

## 4. Presence, check-ins and rituals

- [x] Durable per-companion scheduled work and inbox/read/dismiss state; missed schedules have bounded catch-up and expiry.
- [x] Quiet hours, timezone, frequency limits, duplicate prevention, an off switch and bounded inference work. Never infer consent to cloud providers.
- [x] Use the companion's persona and eligible sources. Cancel/invalidate queued or generated messages on Forget, source changes, deletion or restore.
- [x] A “she has something to say” banner when opening Altera; browser push is a separate explicit future integration, not silently enabled by this goal.
- [x] Optional time-aware greetings/goodnight/weekly rituals, with controls that prevent repeated or intrusive prompts.
- [x] Persistent companion expression state that decays toward a baseline and drives tone/portrait consistently. User emotion analysis remains separate. Default behavior does not punish absence or demand attention.
- [x] Verify restart recovery, competing tabs/workers, quiet hours/DST, stale jobs, deletion and source boundaries with a controllable clock.

## 5. Episodes, memory salience and Our story

- [x] Companion-owned episodes with source IDs/hashes/ranges and a clear distinction between actual exchanges and imagined scenes.
- [x] Occasional relevant episodic recall with a frequency budget. Never manufacture “remember when” detail.
- [x] User pins and inspectable salience/recency; decay lowers retrieval priority rather than silently deleting pinned or explicitly saved information. Extraction confidence stays separate from salience.
- [x] A curated milestone/story view built on existing event and notebook foundations; provide sources and correction controls.
- [x] Bounded scheduled reflection that updates current notes/episodes from eligible conversations, retains uncertainty, and does not turn previous generated reflection into independent evidence.
- [x] All derived stores/jobs participate in correction, Forget, invalidation, backup/restore and companion removal. Test idempotent retries and failure recovery.

## 6. Privacy and easier setup

- [x] Versioned authenticated encrypted backup format, password/key handling, size/resource bounds, wrong-key/corruption checks, and tested restore/recovery. Plaintext exports remain clearly labelled.
- [x] A single setup entry point with prerequisite checks, portable defaults, useful failure messages, health/readiness steps and repeatable upgrade behavior.
- [x] Support the installed local model service first. Setup can explicitly probe configured model-list metadata and never installs a service or weights. Any future optional runtime/model downloader must be explicit, version/checksum/licence aware and resumable. No duplicate model installation or automatic environment upgrade occurs.
- [x] Verify a clean isolated Windows runtime install, missing/offline prerequisites, existing configurations, upgrade preservation, and backup recovery instructions. Linux/macOS wrappers have syntax/isolated contract checks; native installs on those systems are not claimed.

The password vault and its recovery and migration work are tracked in the [deferred milestone](VAULT_RELEASE_MILESTONE.md).

## 7. Shared world and expression

- [x] **Picture of us** uses an explicitly supplied user reference and the companion portrait. The picture flow reviews recipients and uses the existing age and photo safeguards. A real configured-image run remains open in the [release checklist](ORDINARY_RELEASE_ACCEPTANCE.md).
- [x] Generated scenes carry an imagined-scene label. The chat and notebook treat their provenance separately from real-event evidence, and generating a picture does not create an episode.
- [x] A browser-persistent decorative room/backdrop with optional time/season changes, still choices and reduced-motion support; no inference on page open. Actual companion-image and device quality remain under acceptance below.
- [x] Tie portrait expression to the companion's persistent expression state while preserving speech/lip synchronization. Simulated-frame tests cover priority and decay; actual portrait/audio quality remains part of final acceptance.
- [x] Optional prepared backchannels are off by default and suppress playback on recording and interruption. Clips stay separate from saved replies. Listening and microphone acceptance remain open.
- [ ] Verify image failures, ownership/reference deletion, provenance, responsive layout, reduced motion, resource usage, and actual audio/visual quality.

## Completion standard

For the ordinary-app release, each in-scope phase requires working behavior, appropriate isolated regression tests, updated user/API/architecture documentation, and a local commit. Real model, microphone, speech and image acceptance must be reported separately from simulated checks. Record remaining limitations honestly; do not call the ordinary release complete while its required acceptance work remains. The password-vault requirements are tracked separately in [the deferred vault milestone](VAULT_RELEASE_MILESTONE.md).

The app remains local by default. Optional microphone monitoring, scheduled inference, downloads, cloud use and additional data retention must be visible and configurable. Existing origin protection, dataset epochs, companion ownership, recall boundaries and exclusive writer coordination apply to every new feature.
