# Private storage readiness

Altera keeps its running data unencrypted on disk: the conversation database, the memory index, pictures, voice notes, notes, logs and the browser's own storage. Encrypted backup archives protect the copies you make, not the running app. To protect the data at rest, use full-disk encryption such as BitLocker on every drive Altera uses. Moving temporary files into the instance improves confinement and cleanup; it does not encrypt them. The read-only checker below reports whether the drives you name are protected.

## Read-only scoped checker

Run the standard-library script explicitly from an installed Python:

```powershell
python scripts/check_private_storage.py --home 'D:\Altera-private-instance' --additional-path 'C:\Browser-profile' --additional-path 'E:\Backup-downloads'
```

`--home` defaults to `ALTERA_HOME`, or the source checkout when unset. Repeat `--additional-path` for actual browser profiles, downloads, custom caches and local model/voice/image service locations. Paths are resolved before reducing them to local drive letters; repeated drives are queried once. UNC shares, volume-GUID-only paths, unresolved paths and unsupported mount forms remain explicit unknown scope issues. The checker does not enumerate private configuration or discover missing external paths for you.

On Windows, the checker always includes the OS drive and the resolved instance drive. It locates Windows PowerShell through the OS system directory, without a PATH lookup, loads the built-in BitLocker module from that trusted directory, and calls `Get-BitLockerVolume -ErrorAction Stop`. Only validated drive letters travel through stdin JSON; no supplied path is interpolated into a shell command. There is no elevation or encryption, unlock, permission or recovery-key operation.

The query selects only `MountPoint`, `VolumeStatus`, `ProtectionStatus`, `LockStatus` and `EncryptionPercentage`. It never requests key-protector data. Helper runtime is limited to 15 seconds, with bounded termination/drain grace, and combined stdout/stderr is capped at 64 KiB while reading. Output contains only scoped drive letters, the five allowed evidence fields when valid, and safe reason codes. Raw PowerShell errors, private paths, credentials and recovery identifiers are not printed. See Microsoft's [Get-BitLockerVolume reference](https://learn.microsoft.com/en-us/powershell/module/bitlocker/get-bitlockervolume?view=windowsserver2025-ps).

| Per-drive result | Meaning |
| --- | --- |
| `PROTECTED` | A valid current response says `FullyEncrypted`, protection `On`, `Unlocked`, and 100% encryption. |
| `LOCKED` | The volume reports locked; application access is unavailable. This is distinct from a readable protected volume. |
| `UNPROTECTED` | Valid evidence shows protection off or encryption incomplete. This does not mean every byte is plaintext. |
| `UNKNOWN` | Access denied, missing/unsupported volume, unavailable command, timeout, malformed/inconsistent output or another verification failure. Unknown is never inferred unencrypted or ready. |

JSON global `status` is `QUERIED_DRIVES_PROTECTED` only when every requested path resolves and every queried drive passes. Exit status is 0 for that scoped result, 1 for known locked/unprotected results, and 2 for unknown or unsupported results. Non-Windows hosts return `UNSUPPORTED` without running PowerShell. `whole_app_coverage_verified` is always false: even a zero exit status establishes only the named drives' reported condition, not complete application coverage, recovery readiness, a successful migration or protection of another browser device.

Access denial is a verification failure even when a helper exits zero and also prints a misleading “no associated volume” message. Windows 11 Pro and command availability do not establish active encryption. The checker does not retry with administrator privileges.

## Coverage required before a complete protection claim

| Surface | Required locations and remaining work |
| --- | --- |
| Conversations and vectors | SQLite database, WAL/SHM and temporary data; Chroma metadata, embeddings and HNSW files. Protect their actual resolved volumes. |
| Instance files | Editable notebooks/history, derived stores, avatar/photo media, profile, private configuration/environment files, logs and workspace output. |
| Voice and temporary data | Uploaded recordings, retained reply audio, TTS caches, instance `data/tmp`, child-service caches/logs/temp, backup upload/decryption/rollback staging. |
| OS residue | OS/browser profile, pagefile, hibernation and crash dumps, plus any configured destinations on other volumes. Plaintext remains accessible to the unlocked OS/processes. |
| Browser and exports | Offline-message and voice-note IndexedDB, pending/transcript sessionStorage, browser-restored tabs, downloads, readable exports, old backups and other client devices. Altera cannot verify another device's disk from the server. |

The current instance-private temp helper rejects directory links/junctions and removes only files it created. Cleanup on ordinary failure/cancellation is tested. Process termination or filesystem errors can leave files behind; no silent sweep or guaranteed secure erasure is promised. A custom cache on another volume cannot use an atomic rename from instance temp; audio publication fails rather than copying plaintext as a fallback.

BitLocker protection being suspended is insufficient even when the volume reports encrypted: Microsoft documents that suspension places an unprotected clear key on disk. Protection must be enabled and independently verified. Windows volume protection addresses offline storage access; an unlocked Windows session, administrator access, malware, external providers and user-created copies require their own controls. See Microsoft's [BitLocker FAQ](https://learn.microsoft.com/en-us/windows/security/operating-system-security/data-protection/bitlocker/bitlocker-overview-and-requirements-faq).

## Windows recovery

Windows manages any separately enabled OS/data-volume unlock and BitLocker recovery. Keep Windows recovery material away from the protected device. A backup-archive password or recovery key and a BitLocker credential have separate purposes. Microsoft describes its [Windows recovery options](https://learn.microsoft.com/en-us/windows/security/operating-system-security/data-protection/bitlocker/recovery-overview).
