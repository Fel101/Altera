# Companion memory milestone verification — October 28, 2026

This checkpoint completes the implementation and isolated verification for phase 5 of the [active roadmap](COMPANION_ROADMAP.md). The overall goal remains active: privacy/setup, remaining shared-world features, real model/microphone/speech/image acceptance and production activation are still required. The owner requested local Git commits only; no source was published or remote CI dispatched.

## Delivered behavior

- Companion-owned episodes retain original message roles, IDs, hashes and Unicode ranges. Actual exchanges, imagined scenes and derived interpretations remain distinct in inspection and recall.
- Our story supports reviewed message selection, user curation, correction, pins, importance and explicit restored-curation review. Imported curation is validated before use; a conflicting newer capture is preserved.
- Retrieval age lowers ordering without deleting saved data or changing confidence. Completed prompt inclusion is counted separately from a model mentioning or believing a fact. Episodic callbacks have a bounded frequency/size budget with stable reservations and failure release.
- Separately opted-in reflection uses bounded original user evidence, publishes derived notebook/episode candidates atomically, preserves user edits/dismissals and requires exact approval for an external connection. Reads do not start inference.
- Correction, Forget, source edits, companion/session removal, clear and restore cover the new stores and outstanding work. Explicit Forget clears derived notebook copies; old curation cannot bypass a newer recall boundary.

## Verification

The final full Python run passed **964 tests** in 358.74 seconds. One existing OpenTelemetry dependency deprecation remains. An earlier full run exposed three companion-removal failures; the cause was lazy reflection initialization during reserved maintenance. Cleanup now uses the existing SQLite transaction. The final full run includes the fixes, cold-service lifecycle regressions and removal of recall receipts for deleted messages.

The full frontend run passed **193 checks**, including 51 Story and 20 memory-priority checks. Ruff, dependency consistency and source syntax checks passed (80 Python sources and 18 application/service-worker JavaScript files). No dependency, runtime or model was installed or upgraded for this milestone.

Tests use isolated instance directories and fake model calls. They cover ownership, dataset changes, hash/range review, dirty/Forget boundaries, strong forgetting, failed publication, optimistic revisions, same-request retries, quiet hours/DST/caps/provider consent, stable recall reservations, backup round-trip, preserved pins/curation, source-role evidence and source-integrity versus truth. Frontend checks cover delayed responses, context changes, transport/reload failures, immediate post-save actions, focus and text-only rendering.

Synthetic browser QA verified default-off reflection, explicit curation restore with the existing pin, one fake reflection and its durable notebook/report, exact original evidence in the knowledge report, priority without confidence changes, readable source selection, one save, immediate pin/correction, companion switching and keyboard skip navigation. Desktop and phone layout used actual `innerWidth` values of 1280 and 390. Neither page overflowed horizontally. The final phone correction dialog was 348 pixels wide, centered at x=18, with equal 346-pixel client/scroll widths. Browser screenshots are encoded at the browser capture's dimensions; viewport measurements are recorded separately.

The only captured final browser warning was the existing disabled-workspace-tools warning. The preview used fake providers and synthetic data throughout; no real model, microphone, speech or image generation was exercised. Both owned preview processes were stopped and the viewport reset. One earlier synthetic temporary directory remained after a Windows file-lock cleanup failure; it was not manually removed. The helper now closes its seed connection and collects released handles, and a fresh startup/shutdown smoke check removed its own temporary directory. Browser tabs remain temporary; the unavailable browser close API was not used as evidence of closure. The existing production instance was not restarted or stopped.

Local evidence is under ignored `data/companion-story-20261028/`: full-run logs/receipts, frontend/lint/dependency output, source hashes, browser measurements/screenshots and shutdown evidence. It is excluded from source commits along with local settings, private data and unrelated `.claude/` files.

## Practical limits

Source checks and prompt instructions cannot establish that a selected model follows uncertainty or produces a convincing companion. Real integration acceptance remains separate. Runtime ownership/recovery supports one Her server per instance, with the previous process stopped before restart. Data and existing backup ZIPs remain plaintext until the next privacy phase is implemented and explicitly activated. The 200-episode display/recall candidate window and 60-row source scan are bounded; this is not semantic search over every conversation.

[Our story guide](../OUR_STORY.md) · [Memory controls](../MEMORY_CONTROLS.md) · [Architecture](../ARCHITECTURE.md) · [API](../API_REFERENCE.md)
