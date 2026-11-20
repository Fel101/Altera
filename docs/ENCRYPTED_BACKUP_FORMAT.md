# Encrypted backup envelope, version 1

`core/backup_crypto.py` wraps a portable Altera ZIP snapshot in an authenticated
`.alterabak` file. It uses the official PyNaCl bindings to libsodium Argon2id,
XChaCha20-Poly1305 AEAD key wrapping, and secretstream XChaCha20-Poly1305 file
encryption. It does not extract archives, execute SQL, restore a database, or
contact a model. ZIP contents still require BackupService's ordinary validation
after successful decryption. This application envelope has local implementation
review and regression tests; it is not a claim of independent cryptographic audit.

## Credentials and recovery

Creation requires a Python Unicode string containing **at least 12 Unicode
characters** (`len(value) >= 12`) and **at most 1,024 UTF-8 bytes**. Unpaired
surrogates, empty strings, bytes objects and other types are rejected. The codec
does not strip whitespace or apply Unicode normalization: spaces and composed or
decomposed characters are significant. A long, independently chosen passphrase is
appropriate; meeting the length limit alone does not establish password strength.

`validate_password(value) -> None` applies this creation policy without a KDF or
filesystem operation, so callers can reject invalid input before making a
plaintext snapshot. Decryption accepts a nonempty valid Unicode string bounded
to 1,024 UTF-8 bytes; it deliberately does not apply the creation minimum again.

`generate_recovery_key()` returns `ALTERA-RECOVERY-1-` followed by the canonical,
unpadded Base64URL encoding of 32 random bytes (43 encoded characters; 61 total).
It is an independent secret, not a password hint or derived archive key. The codec
returns it only from this explicit generation call and never saves it for the
caller. Providing it to `encrypt_file(..., recovery_key=...)` adds a recovery
wrapper. Omission creates an archive with password access only.

Either the correct password or an enabled recovery key can open an archive.
`decrypt_file` requires **exactly one** of these credentials per call. The recovery
key must be kept separately from the backup, for example in the owner's password
manager or a separately protected physical record. Anyone possessing it and the
archive can decrypt that archive. Losing the password and the applicable recovery
key makes the archive unrecoverable; there is no account reset, server-held key or
backdoor. A recovery key cannot unlock archives created without its wrapper.
Adding recovery access or changing a password requires a new encrypted archive.

## File layout and limits

All integer framing fields are unsigned 32-bit **big-endian** values. The layout is:

1. The 24 literal ASCII bytes `ALTERA-ENCRYPTED-BACKUP\n`.
2. Four bytes containing the JSON header length `H`, where `1 <= H <= 8192`.
3. Exactly `H` bytes of canonical JSON, as defined below.
4. Zero or more data frames: a four-byte ciphertext length followed by that
   many bytes from libsodium secretstream.
5. One final frame: the length `17`, followed by an authenticated, empty
   secretstream message carrying `TAG_FINAL`.
6. End of file. Any additional byte is an error.

The maximum plaintext ZIP size `P` is **2 GiB (2,147,483,648 bytes)**. Empty files
are representable at the codec layer; ordinary ZIP validation will reject an
empty file as a restore candidate. Each data frame contains exactly 1,048,576
plaintext bytes, except the last data frame, which may be shorter. Every data
frame carries `TAG_MESSAGE`; `TAG_PUSH`, `TAG_REKEY` or a premature `TAG_FINAL`
are rejected. The separate final frame is mandatory even when `P` is an exact
multiple of the chunk size or is zero.

Secretstream adds 17 bytes per message; framing adds four more. If
`N = ceil(P / 1,048,576)`, the exact envelope size is
`24 + 4 + H + P + 21 * (N + 1)`. There are at most 2,049 frames including the final
frame, so total ciphertext overhead is bounded by **51,249 bytes**, independent
of archive contents. `MAX_CIPHERTEXT_BYTES` is the plaintext cap plus that fixed
overhead cap. The reader checks the file size, header bounds, entire header
schema and every frame's expected length **before running Argon2id**. It then
authenticates the complete stream and checks EOF again.

No archive member names, companion names, conversation text, machine paths,
passwords or unwrapped keys are stored in the public header. The plaintext byte
count, encryption profile and existence of a recovery wrapper are public. File
length and filesystem metadata remain observable.

## Canonical header schema

Canonical JSON means Python `json.dumps(value, sort_keys=True, separators=(',',
':'), ensure_ascii=True, allow_nan=False)`, encoded as ASCII, with no leading or
trailing whitespace. Duplicate keys, non-finite values, unknown fields,
unsupported versions and noncanonical encodings are rejected. Integer fields
must be JSON integers, never booleans or floating-point values. All binary fields
use canonical **unpadded Base64URL**, with the exact decoded lengths below.

| Object | Exact fields and values |
| --- | --- |
| Header | `format: "altera-encrypted-backup"`, `version: 1`, `cipher: "secretstream-xchacha20poly1305"`, `chunk_bytes: 1048576`, `plaintext_bytes: P`, `stream_header` (24 bytes), `password` (object), `recovery` (object or `null`) |
| `password` | `kdf: "argon2id13"`, `opslimit: 3`, `memlimit: 67108864`, `salt` (16 bytes), `wrap: "xchacha20poly1305-ietf"`, `nonce` (24 bytes), `wrapped_key` (48 bytes) |
| `recovery`, when present | `scheme: "altera-recovery-v1"`, `wrap: "xchacha20poly1305-ietf"`, `nonce` (24 bytes), `wrapped_key` (48 bytes) |

The KDF profile is fixed, not an attacker-selected resource request: version 1
accepts only Argon2id version 1.3 through `nacl.pwhash.argon2id.kdf`, with a 32-byte
output, three operations and 64 MiB of memory. Future profiles require a deliberate
format-version decision. The parser validates even an unused password wrapper
when a recovery key is supplied.

## Key wrapping and authenticated data

Each archive receives a fresh random 32-byte **archive key** from libsodium's
secretstream key generator and a fresh secretstream header from `init_push`.
Password derivation uses a fresh random salt. Password and recovery wrapping
each use a fresh random 24-byte nonce. The recovery secret is already a uniformly
random 32-byte key, so it does not undergo the password KDF.

Both wrappers encrypt the same archive key with
`crypto_aead_xchacha20poly1305_ietf_encrypt`. The 48-byte wrapped value contains
the 32-byte encrypted key and its 16-byte authentication tag. The **wrapper core**
is the canonical complete header with only each wrapper's `wrapped_key` field
removed. Its parameters, salt, nonce, stream header, plaintext size and recovery
presence are all included. Associated data for the password wrapper is:

`MAGIC || 0x00 || "wrap-password" || 0x00 || canonical(wrapper_core)`

The recovery wrapper uses `"wrap-recovery"` in the same position. Distinct method
labels bind each wrapper to its intended use. Excluding wrapped ciphertexts avoids
a circular dependency between the two wrappers.

Every secretstream message, including the final message, uses the **entire exact
envelope prefix** as associated data:

`MAGIC || uint32be(H) || canonical(complete_header)`

This includes both wrapped ciphertexts. Altering an unused password wrapper while
opening with recovery therefore still fails stream authentication. Libsodium
secretstream supplies sequencing, nonce/key evolution and message authentication;
the application does not invent a chunk cipher or independently derive per-chunk
nonces. Reordering, duplicating or modifying chunks fails authentication. A valid
tagged message cannot replace the required final marker.

## API and publication behavior

```python
validate_password(password)  # no file access or KDF
recovery = generate_recovery_key()  # optional; present separately to the owner
metadata = encrypt_file(source_zip, new_encrypted_path, password,
                        recovery_key=recovery)
metadata = decrypt_file(encrypted_path, new_plaintext_path, password=password)
# Or, in a separate call:
metadata = decrypt_file(encrypted_path, new_plaintext_path,
                        recovery_key=recovery)
```

Both file functions return only `format`, `version`, `encrypted: True`,
`plaintext_bytes` and `recovery_enabled`. They do not return keys or headers.
Constants `MAGIC`, `MAX_PLAINTEXT_BYTES`, `MAX_CIPHERTEXT_BYTES` and the exception
`BackupCryptoError` are public integration points. Callers must serialize expensive
decrypt attempts and bound uploads independently; the codec's KDF and file bounds
limit each invocation, not the number of concurrent invocations.

The source must be a stable regular file. Source symlinks, existing destinations
(including dangling symlinks), missing destination parents and source/destination
collisions fail. The codec checks source size and file identity/timestamps during
processing. Callers must keep their snapshot immutable while encryption runs;
these checks are not a filesystem snapshot or a lock against an adversarial writer.

Output streams into an unpredictable `.altera-crypto-*.part` file in the destination
directory, with mode 0600 where supported. Each plaintext chunk is written only
after its own authentication, and the destination name is published only after
the full stream, final marker, size and EOF checks pass and the output file is
flushed and synced. Windows uses a same-filesystem rename, which refuses an
existing destination. POSIX uses atomic hard-link creation to avoid rename's
replacement behavior. Filesystems that cannot support that operation fail closed.
A competing destination created before publication is preserved.

Cleanup removes only the operation's own temporary path. It never removes the
input, an existing destination or other temporary files. An OS refusal to unlink
or a process/power failure can leave an orphan temporary file; cleanup is best
effort, and the destination is never published before successful authentication.
The directory entry is not explicitly fsynced, so crash durability is distinct
from cryptographic integrity and atomic visibility.

Every failed decrypt, including wrong/missing/malformed credentials, corruption,
unsupported format and filesystem refusal, raises the same `BackupCryptoError`
message: “The encrypted backup could not be opened. Check the password or recovery
key and the file.” This is a uniform error message, not a constant-time claim:
malformed public framing is rejected before the deliberately costly password KDF.
Creation validation can return separate non-secret-bearing policy messages.

## Coverage and limits

This envelope protects the saved encrypted backup. It does **not** encrypt the
running SQLite database, vectors, notebook files, pictures, speech, browser
storage or the plaintext source snapshot. Decryption produces a plaintext ZIP;
the staging directory and its cleanup need the restore workflow's protection.
An interrupted decrypt may leave authenticated plaintext chunks in its private
temporary file. Windows permissions inherit the chosen directory's ACLs; POSIX
0600 does not encrypt storage. Deletion is not secure erasure on SSDs, journals,
backups or copy-on-write filesystems.

Python strings and bytes, CFFI buffers, garbage collection, swapping and crash
dumps prevent a reliable application-level promise to zero every password/key or
plaintext copy. The codec does not persist an unwrapped key or intentionally log
secrets. This is different from guaranteeing that secrets cannot remain in process
memory or operating-system storage. Keep recovery secrets out of logs, URLs and
browser persistence in the surrounding UI.

Real-library tests cover multi-chunk/password/recovery round trips, fresh keys and
nonces, wrong credentials, malformed public/KDF bounds, chunk tampering and order,
authenticated tag misuse, mandatory final/EOF, no-clobber publication and temporary
cleanup. Reused synthetic fixtures bound expensive KDF work; authentication is not
disabled. Reduced-limit tests exercise size rejection without allocating a 2 GiB
fixture. This is functional/security regression evidence, not a real-data recovery
exercise or a performance claim at the maximum archive size.

## Archives from before the rename

Altera was called Her until November 2026. Archives written then are the same
format under the old names: the 21-byte magic `HER-ENCRYPTED-BACKUP\n`,
`format: "her-encrypted-backup"`, `scheme: "her-recovery-v1"`, recovery keys that
start `HER-RECOVERY-1-` and the `.herbak` extension. The reader recognises either
magic and then requires the matching names inside the header, which stays
authenticated exactly as it was written. New archives always use the Altera names.

## Primary references

- [Libsodium secretstream file-encryption API](https://doc.libsodium.org/secret-key_cryptography/secretstream).
- [PyNaCl 1.6.2 secretstream binding signatures](https://github.com/pyca/pynacl/blob/1.6.2/src/nacl/bindings/crypto_secretstream.py).
- [PyNaCl 1.6.2 AEAD bindings](https://github.com/pyca/pynacl/blob/1.6.2/src/nacl/bindings/crypto_aead.py).
- [PyNaCl 1.6.2 Argon2id KDF interface](https://github.com/pyca/pynacl/blob/1.6.2/src/nacl/pwhash/argon2id.py).
