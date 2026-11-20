# Privacy and setup verification — October 28, 2026

This checkpoint implements encrypted backups and easier setup in phase 6 of the [active companion roadmap](COMPANION_ROADMAP.md). The owner selected a **separate Her password vault**; that complete unlock, lock, recovery and migration path remains unfinished. The overall goal is active, including real integration/activation and the remaining shared-world phase. Git commits stay local; GitHub and remote CI are deferred by the owner.

## Implemented behavior

New backup requests default to a versioned authenticated `.herbak` envelope. A random archive key is wrapped by a password using fixed, bounded Argon2id and authenticated encryption, with an optional independent one-time recovery key. Streaming encryption authenticates order and completion. Strict public-header and file/frame limits precede expensive password work. Plaintext ZIP is an explicit choice; readable report exports remain plaintext. See the [format](../ENCRYPTED_BACKUP_FORMAT.md) and [backup guide](../BACKUP_AND_RESTORE.md).

Preview keeps an encrypted upload encrypted on disk, decrypts only in private work space, validates the logical archive, and stores no unlock secret. Applying an encrypted source requires its credential again and an encrypted recovery copy before replacing data. Failed rollback retains a recovery journal and protected current settings; missing or changed protection cannot silently become an empty configuration. Completed restores retain a retry receipt. Postcommit cleanup failure cannot trigger rollback or lose the completed repair's recovery state.

Staging uses per-directory thread and OS file locks, private directories, exact owned files and bounded operation-triggered expiry scanning. The scanner progresses around preserved unknown entries during one service lifetime; restart resets its cursor. Expiry establishes cleanup eligibility, not a deadline. Unknown paths and interrupted-recovery work are preserved. Multipart uploads, STT and TTS temporary files now use confined instance temp space. These controls do not encrypt running storage or guarantee erasure after crashes/OS failures.

`setup.bat`, `setup.sh` and the standard-library setup script check installed Python 3.11, prepare a missing project environment, add compatible missing Python requirements, and copy missing portable starters. Existing package versions and settings/data are preserved by default. Check/dry-run/offline modes do not silently download weights or start a service. Model-list metadata probing is explicit and bounded. A separate read-only Windows device checker reports unknown on denied/unverifiable access; it is supplemental to the selected vault.

## Automated and fresh-environment evidence

- Archive/setup full Python run: **1,211 passed, 5 platform skips**, 517.23 seconds. The run excluded unfinished vault foundation tests. One existing OpenTelemetry dependency deprecation remains. The final bounded-scan progress change was made afterward and is covered by the final staging run below.
- Final staging regression run: **49 passed, 2 platform skips**, covering protected settings, active-operation locks, exact ownership, postcommit cleanup failure, repair retry and scanner progress. Earlier broader backup verification passed **99 checks, 2 skips** before the scan-progress change.
- Archive/setup frontend suite: **210 passed**. Ruff and `pip check` passed. Source syntax and final documentation checks are retained with their exact counts in the local receipts.
- The native archive tests exercise real PyNaCl authenticated file encryption, real SQLite and an existing native Chroma backup round-trip without model inference. Wrong credentials, recovery keys, corruption/truncation, unsafe logical archives, resource bounds and credential-free receipts are tested. Fake stores isolate other failure scenarios; they do not prove voice/model quality.
- A genuinely fresh Python 3.11.15 environment installed the normal runtime requirements successfully from official PyPI binary packages in 242.313 seconds. No existing environment was upgraded. The preserved fresh environment subsequently passed an actual arbitrary-working-directory offline setup check and `pip check`, with every installed version and starter/config hash unchanged by those checks.
- The fresh environment exercised genuine SQLite FTS5, cffi and a PyNaCl file round-trip. A guarded `create_app(autostart=False)` returned 200 for three pages and 22 local assets, with no network attempts, service/scheduler extension or background thread. This is factory/build acceptance, not a real model or native vault test.

The existing development environment gained only the required pinned PyNaCl 1.6.2 and its missing cffi 2.1.1 / pycparser 3.12 dependencies after official registry hash verification. No existing package was upgraded. The separate disposable fresh install downloaded its own runtime dependencies. No model weights or model/speech service were installed or started.

## Browser evidence

An owned preview used only synthetic companions/conversations, fake model services and blocked model clients. Browser interactions verified encrypted creation, the optional separate recovery key, wrong-password rejection, recovery-key preview, secret clearing on close/reopen, an encrypted recovery copy, successful restore, two encrypted saved archives and focus return. The preview was stopped, its temporary tab closed and viewport override reset; the live instance was untouched.

The desktop viewport was 1280 pixels wide, with a 620-pixel dialog and equal 612-pixel client/scroll widths. The phone viewport was 390 × 844, with a centered 354-pixel dialog and equal 346-pixel client/scroll widths. Neither had horizontal overflow. Captured screenshots are actual 1280 × 720 and 390 × 844 JPEGs. Only the existing disabled-workspace-tools warning appeared; no browser error was captured.

## Remaining work

No native securefs/WinFsp binary was installed, executed or mounted; no app vault or browser encryption was activated. Server startup and WSGI protection, real owned worker shutdown, native SQLite/vector locking/recovery, browser key handoff and durable encrypted queues, multi-tab locking, old-data migration, complete path coverage and recovery acceptance remain required. Existing ordinary storage, browser copies, completed restore receipts and plaintext exports are not protected by archive encryption.

Real local model, microphone/echo, voice and image acceptance and safe production activation also remain required under phase 0. Remote Windows/Linux CI is deferred. Phase 7 still includes shared-reference scene pictures, imagined-scene labels, room backdrops and optional restrained backchannels.

Detailed command logs, install/source hashes, crypto installation reports, browser receipts and screenshots are retained locally under ignored `data/companion-privacy-20261028/`. They are evidence artifacts, not committed personal data or a promise that unperformed acceptance checks passed.
