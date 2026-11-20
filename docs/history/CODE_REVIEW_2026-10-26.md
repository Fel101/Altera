# Her code review and verification — October 26, 2026

This review covers the working source, including the uncommitted polish work. Existing changes were retained. Fixes were selected from reproducible behavior rather than the origin or age of the code. The running Her instance was not restarted, and verification used isolated stores and fake model calls.

## Findings and fixes

| Priority | Finding | Resolution |
| --- | --- | --- |
| P1 | Windows restore could overwrite a reserved runtime file through a case variant such as `data/SECRET_KEY`. | Case-insensitive reserved-path checks for both backup and restore; nine archive regressions. |
| P1 | URL fetching checked DNS before urllib resolved the hostname again, permitting DNS rebinding to local services. | Validate all DNS answers at connection time and connect by numeric address; preserve HTTP Host, HTTPS SNI/certificate verification and redirect checks. Disable ambient proxies and reject shared/Tailscale addresses too. |
| P1 | Cross-site, cookie-free requests to the proactive GET endpoint could invoke a model and persist check-in state. | Apply the existing browser origin/fetch-site checks to proactive GET/HEAD before any work, with full-app negative and same-origin regressions. |
| P1 | Proactive check-ins could send chat text from before a Forget boundary and include shared stale mood/follow-up context. | Apply the owner's recall boundary to check-in input, exclude stale shared context, and skip generation when no eligible conversation remains. |
| P1 | Privacy clearing could report success after failed photo/vector/notebook deletion, or race background writers. | Reserve exclusive access before deletion, propagate storage failures, retain retryable photo records, include orphaned pictures/notebooks, clear current/legacy speech caches, and keep unfinished speech workers leased. Commit the new required dataset epoch with the database reset. A partial failure returns `clear_incomplete`; retry before using Her. |
| P2 | Repeated streamed text could be mistaken for the final aggregate, truncating and saving the reply early. | Mark aggregate chunks explicitly instead of comparing text; regression cases include repeated deltas and tool streams. |
| P2 | Helpers before the first chat could ignore the saved provider/model and use a built-in model. | Initialize helpers from saved configuration when no selected client exists. |
| P2 | Removing a busy companion could delete pictures before discovering an active reply; legacy avatar removal left owned data behind. | Both removal routes share guarded cleanup with ledger/worker/notebook preflight. Missing owned avatars cannot fall back to another companion. |
| P2 | Camera requests could use a different tab's globally active companion. | Validate the explicit session and resolve its companion before creating the picture. |
| P2 | Markdown imports truncated at `---`, allowed missing/busy sessions and committed one message at a time. A single-session export silently capped history at 10,000 messages. | Preserve embedded rules/dashes and fenced headings; validate UTF-8/target/messages, append atomically, reject active replies, and export the full conversation. Imported timestamps remain import-time; backups are the complete recovery format. |
| P2 | Workspace permissions used a cached config snapshot and enabled tools on errors. | Read the current config and require explicit boolean `true`; missing/malformed/unreadable settings fail closed. |
| P2 | Provider/model names from configured services were interpolated into option HTML. | Build literal DOM options; quote/markup regression checks prevent HTML interpretation. |
| P3 | Huge restored usage numbers could overflow diagnostics conversion. | Bound numeric values before float operations; malformed/unrepresentable measurements stay unknown. |
| P3 | Guides described older startup, privacy, schema and API behavior. | Refresh quickstart, architecture, all API routes, schema, companion/image/phone/memory guides, and label the old roadmap historical. Add route/link/page-asset regression checks. |
| P3 | Several older tests depended on enabled services in the developer's personal config; an origin test could pass because tools were disabled. | Establish temporary instance paths before collection, copy portable starter defaults, and explicitly enable integrations in tests that exercise them. |

## Verification

Final verification passed on Windows with Python **3.11.15** and Node **24.16.0**:

| Check | Result |
| --- | --- |
| Full Python suite | **662 passed**, approximately 132 seconds; one existing OpenTelemetry dependency deprecation |
| Frontend logic | **14 passed**; stream/retry/controls/drafts/caching and provider-option handling |
| Ruff | Passed for the configured application/preview-script scope |
| Dependencies | `pip check` passed; offline lockfile dry run found every pinned requirement already installed |
| Python syntax | **95** source files parsed |
| Browser/template syntax | **13** JavaScript files, **8** templates and **11** rendered inline scripts checked |
| Page rendering | **6** pages rendered with network calls disabled; documented page assets exist |
| Documentation | Registered routes covered; local Markdown links/control characters checked |
| Startup and recovery | Full suite includes real isolated Waitress/remote-header startup, native vector round trips, rollback, interrupted work and retry tests |

The first isolated full run exposed tests relying on personal configuration. Those fixtures were corrected, the test session now establishes its own instance before imports, and the full suite passed again. Check-in security/recall fixes then received another full passing run. The stronger final proactive-context assertions and documentation checks also passed together (**14** focused tests).

Local receipts and detailed check output are retained under ignored `data/review-20261026/`, including earlier failed results. No packages were upgraded or installed during this review.

This is a Python source application with static browser assets, so there is no frontend bundle or installer build step. Verification includes Python tests, isolated real Waitress startup, dependency consistency, Ruff, Python/JavaScript/template parsing, page rendering, local assets, documented routes and local documentation links.

## Practical limits

- No real model generation, microphone, cloud billing, GPU image pipeline or voice-quality acceptance was performed in this review. Passing fake-service tests does not establish integration quality.
- Local checks run on Windows with the installed environment. GitHub's Windows/Linux workflow is configured but was not pushed or dispatched here. A clean dependency installation on a second PC was not performed.
- One application process remains the supported deployment. Coordination cannot protect against another process or external file editor changing the same instance.
- Storage and backup ZIPs are unencrypted. Clearing conversations retains imported avatar identities, backups/exports, logs, workspace files and other browsers' storage. It is not secure disk erasure. A failed multi-store clear can be partial and must be retried.
- Markdown is a readable report format; ambiguous hand-edited layouts are not a general transcript interchange contract. Full backup/restore preserves the recoverable dataset.
- Source changes remain local and uncommitted. Activate updated runtime code by closing the existing Her console and launching `start.bat` when convenient.
- A separate `git diff --check` still flags cosmetic trailing whitespace in older module sections. These do not fail the configured correctness checks; broad formatting changes were kept out of the behavioral fixes. Details are in the local whitespace receipt.

[User guide](../README.md) · [Architecture](../ARCHITECTURE.md) · [API reference](../API_REFERENCE.md) · [Storage schema](../Database_Schema.md)
