"""Bounded version-1 encrypted backup envelopes using libsodium primitives.

This module encrypts/decrypts files only. ZIP validation and restore policy stay
in BackupService. Passwords, recovery keys and unwrapped archive keys are never
written to metadata, files, logs or exception messages.
"""
from contextlib import contextmanager
import base64
import binascii
import json
import os
from pathlib import Path
import re
import stat
import struct
import tempfile

from nacl import bindings, exceptions
from nacl.pwhash import argon2id

from core.filestat import ctime_ns

MAGIC = b'ALTERA-ENCRYPTED-BACKUP\n'
FORMAT = 'altera-encrypted-backup'
RECOVERY_SCHEME = 'altera-recovery-v1'
# Altera was called Her. Archives it wrote then carry these names, inside the authenticated header too, and their
# recovery keys start HER-RECOVERY-1-. They are read exactly as before; only new archives use the names above.
LEGACY_MAGIC = b'HER-ENCRYPTED-BACKUP\n'
_PROFILES = {MAGIC: (FORMAT, RECOVERY_SCHEME), LEGACY_MAGIC: ('her-encrypted-backup', 'her-recovery-v1')}
VERSION = 1
CHUNK_BYTES = 1024 * 1024
MAX_HEADER_BYTES = 8192
MAX_PLAINTEXT_BYTES = 2 * 1024 ** 3
KDF_MEMLIMIT = 64 * 1024 ** 2
KDF_OPSLIMIT = 3
MAX_PASSWORD_BYTES = 1024
RECOVERY_PREFIX = 'ALTERA-RECOVERY-1-'
LEGACY_RECOVERY_PREFIX = 'HER-RECOVERY-1-'
_STREAM_OVERHEAD = bindings.crypto_secretstream_xchacha20poly1305_ABYTES
_MAX_FRAMES = (MAX_PLAINTEXT_BYTES + CHUNK_BYTES - 1) // CHUNK_BYTES + 1
MAX_OVERHEAD_BYTES = max(map(len, _PROFILES)) + 4 + MAX_HEADER_BYTES + _MAX_FRAMES * (4 + _STREAM_OVERHEAD)
MAX_CIPHERTEXT_BYTES = MAX_PLAINTEXT_BYTES + MAX_OVERHEAD_BYTES
DECRYPT_ERROR = 'The encrypted backup could not be opened. Check the password or recovery key and the file.'
_IO_ERROR = 'The backup file could not be processed safely. Choose an unused destination and try again.'
_B64 = re.compile(r'[A-Za-z0-9_-]+\Z')
_CIPHER = 'secretstream-xchacha20poly1305'
_KDF = 'argon2id13'
_WRAP = 'xchacha20poly1305-ietf'


class BackupCryptoError(ValueError):
    """Safe, non-secret-bearing failure for encryption or decryption."""




def _close_checked(stream):
    try:
        stream.close()
    except BaseException:
        raise


@contextmanager
def _owned_stream(stream):
    try:
        yield stream
    finally:
        _close_checked(stream)


def _b64(value):
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _unb64(value, size):
    if not isinstance(value, str) or len(value) != (size * 8 + 5) // 6 or not _B64.fullmatch(value):
        raise ValueError('Invalid encoded field.')
    result = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    if len(result) != size or _b64(result) != value:
        raise ValueError('Invalid encoded field.')
    return result


def _password(value, *, creating=False):
    if not isinstance(value, str) or (creating and len(value) < 12) or len(value) > MAX_PASSWORD_BYTES:
        raise BackupCryptoError('Use a password of at least 12 characters and at most 1,024 UTF-8 bytes.' if creating else DECRYPT_ERROR)
    try:
        result = value.encode('utf-8', errors='strict')
    except UnicodeError:
        raise BackupCryptoError('Use valid Unicode text for the backup password.' if creating else DECRYPT_ERROR) from None
    if not result or len(result) > MAX_PASSWORD_BYTES:
        raise BackupCryptoError('Use a password of at least 12 characters and at most 1,024 UTF-8 bytes.' if creating else DECRYPT_ERROR)
    return result


def generate_recovery_key():
    """Return a printable, versioned 32-byte random recovery secret, once."""
    return RECOVERY_PREFIX + _b64(os.urandom(32))


def validate_password(value):
    """Validate create-password policy without a KDF or filesystem access."""
    _password(value, creating=True)


def _recovery(value):
    for prefix in (RECOVERY_PREFIX, LEGACY_RECOVERY_PREFIX):
        if isinstance(value, str) and value.startswith(prefix):
            return _unb64(value[len(prefix):], 32)
    raise ValueError('Invalid recovery key.')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def _no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate field.')
        value[key] = item
    return value


def _no_constant(_):
    raise ValueError('Non-finite field.')


def _fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError('Unsupported envelope fields.')


def _header(raw, magic):
    """Validate the entire bounded public schema before any password KDF."""
    format_name, scheme = _PROFILES[magic]
    if not 0 < len(raw) <= MAX_HEADER_BYTES:
        raise ValueError('Header outside bounds.')
    value = json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    _fields(value, {'format', 'version', 'cipher', 'chunk_bytes', 'plaintext_bytes', 'stream_header', 'password', 'recovery'})
    if (value['format'] != format_name or type(value['version']) is not int or value['version'] != VERSION or
            value['cipher'] != _CIPHER or type(value['chunk_bytes']) is not int or value['chunk_bytes'] != CHUNK_BYTES or
            type(value['plaintext_bytes']) is not int or not 0 <= value['plaintext_bytes'] <= MAX_PLAINTEXT_BYTES):
        raise ValueError('Unsupported envelope profile.')
    _unb64(value['stream_header'], bindings.crypto_secretstream_xchacha20poly1305_HEADERBYTES)
    password = value['password']
    _fields(password, {'kdf', 'memlimit', 'opslimit', 'salt', 'wrap', 'nonce', 'wrapped_key'})
    if (password['kdf'] != _KDF or type(password['memlimit']) is not int or password['memlimit'] != KDF_MEMLIMIT or
            type(password['opslimit']) is not int or password['opslimit'] != KDF_OPSLIMIT or password['wrap'] != _WRAP):
        raise ValueError('Unsupported KDF or wrapping profile.')
    _unb64(password['salt'], argon2id.SALTBYTES)
    _unb64(password['nonce'], bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES)
    _unb64(password['wrapped_key'], 32 + bindings.crypto_aead_xchacha20poly1305_ietf_ABYTES)
    if value['recovery'] is not None:
        recovery = value['recovery']
        _fields(recovery, {'scheme', 'wrap', 'nonce', 'wrapped_key'})
        if recovery['scheme'] != scheme or recovery['wrap'] != _WRAP:
            raise ValueError('Unsupported recovery profile.')
        _unb64(recovery['nonce'], bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES)
        _unb64(recovery['wrapped_key'], 32 + bindings.crypto_aead_xchacha20poly1305_ietf_ABYTES)
    if raw != _canonical(value):
        raise ValueError('The header is not canonical.')
    return value


def _wrapper_ad(header, method, magic):
    # Ciphertexts are excluded to avoid circular dependencies between wrappers;
    # both ciphertexts remain authenticated by the stream's complete header AD.
    core = {**header, 'password': {key: value for key, value in header['password'].items() if key != 'wrapped_key'}}
    if header['recovery'] is not None:
        core['recovery'] = {key: value for key, value in header['recovery'].items() if key != 'wrapped_key'}
    return magic + b'\x00wrap-' + method.encode('ascii') + b'\x00' + _canonical(core)


def _derive(password, salt):
    return argon2id.kdf(32, password, salt, opslimit=KDF_OPSLIMIT, memlimit=KDF_MEMLIMIT)


def _metadata(header):
    return {'format': header['format'], 'version': VERSION, 'encrypted': True, 'plaintext_bytes': header['plaintext_bytes'],
            'recovery_enabled': header['recovery'] is not None}


@contextmanager
def _input(source, destination):
    """Open only a regular input; do not expose paths in public exceptions."""
    source, destination = Path(source), Path(destination)
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    if not parent.is_dir() or os.path.lexists(destination) or not stat.S_ISREG(source.lstat().st_mode):
        raise OSError('Unsafe input or existing destination.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(source, flags)
    try:
        with _owned_stream(os.fdopen(descriptor, 'rb')) as stream:
            descriptor = None
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise OSError('Input must be a regular file.')
            yield stream, destination, info
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                raise


def _unchanged(stream, before):
    after = os.fstat(stream.fileno())
    if (any(getattr(before, key) != getattr(after, key) for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns'))
            or ctime_ns(before) != ctime_ns(after)):
        raise OSError('Source changed while processing.')


@contextmanager
def _output(destination):
    # mkstemp creates an unpredictable file with mode 0600 where supported. It
    # lives on the destination filesystem, so publication cannot cross devices.
    descriptor, name = tempfile.mkstemp(prefix='.altera-crypto-', suffix='.part', dir=destination.parent)
    temporary = Path(name)
    try:
        try:
            stream = os.fdopen(descriptor, 'wb')
        except BaseException:
            try:
                os.close(descriptor)
            except BaseException:
                raise
            raise
        with _owned_stream(stream):
            yield stream, temporary
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # Archive publication may already have completed.


def _publish(temporary, destination):
    # Windows rename refuses an existing destination. POSIX rename replaces it,
    # so use an atomic no-clobber hard link there instead. Both stay on one FS.
    if os.name == 'nt':
        os.rename(temporary, destination)
    else:
        os.link(temporary, destination)


def _read_magic(stream):
    """The current or the legacy magic; the legacy one is shorter and the two differ in their first byte."""
    start = _read_exact(stream, len(LEGACY_MAGIC))
    if start == LEGACY_MAGIC:
        return LEGACY_MAGIC
    if start + _read_exact(stream, len(MAGIC) - len(LEGACY_MAGIC)) == MAGIC:
        return MAGIC
    raise ValueError('Not an encrypted backup.')


def _read_exact(stream, size):
    value = stream.read(size)
    if len(value) != size:
        raise ValueError('Truncated envelope.')
    return value


def _frame(stream, ciphertext):
    stream.write(struct.pack('>I', len(ciphertext)))
    stream.write(ciphertext)


def _preflight(stream, magic, header_bytes, size, total):
    start = stream.tell()
    frames = (size + CHUNK_BYTES - 1) // CHUNK_BYTES
    if total != len(magic) + 4 + header_bytes + size + (frames + 1) * (4 + _STREAM_OVERHEAD):
        raise ValueError('Envelope size does not match its profile.')
    remaining = size
    while remaining:
        block = min(remaining, CHUNK_BYTES)
        if struct.unpack('>I', _read_exact(stream, 4))[0] != block + _STREAM_OVERHEAD:
            raise ValueError('Invalid frame length.')
        stream.seek(block + _STREAM_OVERHEAD, os.SEEK_CUR)
        remaining -= block
    if struct.unpack('>I', _read_exact(stream, 4))[0] != _STREAM_OVERHEAD:
        raise ValueError('Invalid final frame length.')
    stream.seek(_STREAM_OVERHEAD, os.SEEK_CUR)
    if stream.tell() != total or stream.read(1):
        raise ValueError('Trailing encrypted data.')
    stream.seek(start)


def encrypt_file(source, destination, password, *, recovery_key=None):
    """Encrypt one stable file into a new, atomically published destination."""
    password_bytes = _password(password, creating=True)
    try:
        recovery = _recovery(recovery_key) if recovery_key is not None else None
    except (TypeError, ValueError, binascii.Error):
        raise BackupCryptoError('Use a valid generated recovery key.') from None
    try:
        with _input(source, destination) as (plain, target, before):
            if not 0 <= before.st_size <= MAX_PLAINTEXT_BYTES:
                raise BackupCryptoError('The plaintext backup exceeds the 2 GiB encrypted-backup limit.')
            key = bindings.crypto_secretstream_xchacha20poly1305_keygen()
            state = bindings.crypto_secretstream_xchacha20poly1305_state()
            stream_header = bindings.crypto_secretstream_xchacha20poly1305_init_push(state, key)
            header = {'format': FORMAT, 'version': VERSION, 'cipher': _CIPHER, 'chunk_bytes': CHUNK_BYTES,
                      'plaintext_bytes': before.st_size, 'stream_header': _b64(stream_header),
                      'password': {'kdf': _KDF, 'opslimit': KDF_OPSLIMIT, 'memlimit': KDF_MEMLIMIT,
                                   'salt': _b64(os.urandom(argon2id.SALTBYTES)), 'wrap': _WRAP,
                                   'nonce': _b64(os.urandom(bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES))},
                      'recovery': {'scheme': RECOVERY_SCHEME, 'wrap': _WRAP,
                                   'nonce': _b64(os.urandom(bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES))} if recovery is not None else None}
            derived = _derive(password_bytes, _unb64(header['password']['salt'], argon2id.SALTBYTES))
            header['password']['wrapped_key'] = _b64(bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
                key, _wrapper_ad(header, 'password', MAGIC), _unb64(header['password']['nonce'], 24), derived))
            if recovery is not None:
                header['recovery']['wrapped_key'] = _b64(bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
                    key, _wrapper_ad(header, 'recovery', MAGIC), _unb64(header['recovery']['nonce'], 24), recovery))
            raw = _canonical(header)
            _header(raw, MAGIC)  # Keep the writer and strict version-1 reader aligned.
            associated = MAGIC + struct.pack('>I', len(raw)) + raw
            with _output(target) as (encrypted, temporary):
                encrypted.write(associated)
                remaining = before.st_size
                while remaining:
                    block = _read_exact(plain, min(remaining, CHUNK_BYTES))
                    _frame(encrypted, bindings.crypto_secretstream_xchacha20poly1305_push(
                        state, block, ad=associated, tag=bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE))
                    remaining -= len(block)
                if plain.read(1):
                    raise OSError('Source grew during processing.')
                _frame(encrypted, bindings.crypto_secretstream_xchacha20poly1305_push(
                    state, b'', ad=associated, tag=bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL))
                _unchanged(plain, before)
                encrypted.flush()
                os.fsync(encrypted.fileno())
                _close_checked(encrypted)
                _publish(temporary, target)
            return _metadata(header)
    except BackupCryptoError:
        raise
    except (OSError, ValueError, TypeError, OverflowError, RuntimeError, exceptions.CryptoError):
        raise BackupCryptoError(_IO_ERROR) from None


def decrypt_file(source, destination, *, password=None, recovery_key=None):
    """Authenticate the complete envelope before publishing any plaintext file.

    Exactly one credential is accepted. Every invalid credential, unsupported or
    corrupt envelope, and unsafe filesystem operation has the same public error.
    """
    try:
        if (password is None) == (recovery_key is None):
            raise ValueError('Choose one credential.')
        password_bytes = _password(password) if password is not None else None
        recovery = _recovery(recovery_key) if recovery_key is not None else None
        with _input(source, destination) as (encrypted, target, before):
            if before.st_size > MAX_CIPHERTEXT_BYTES:
                raise ValueError('Envelope outside bounds.')
            magic = _read_magic(encrypted)
            length = struct.unpack('>I', _read_exact(encrypted, 4))[0]
            if not 0 < length <= MAX_HEADER_BYTES:
                raise ValueError('Header outside bounds.')
            raw = _read_exact(encrypted, length)
            header = _header(raw, magic)
            _preflight(encrypted, magic, length, header['plaintext_bytes'], before.st_size)
            if password_bytes is not None:
                wrapper = header['password']
                wrapping_key = _derive(password_bytes, _unb64(wrapper['salt'], argon2id.SALTBYTES))
                method = 'password'
            else:
                wrapper = header['recovery']
                if wrapper is None:
                    raise ValueError('No matching credential wrapper.')
                wrapping_key, method = recovery, 'recovery'
            key = bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
                _unb64(wrapper['wrapped_key'], 48), _wrapper_ad(header, method, magic), _unb64(wrapper['nonce'], 24), wrapping_key)
            state = bindings.crypto_secretstream_xchacha20poly1305_state()
            bindings.crypto_secretstream_xchacha20poly1305_init_pull(state, _unb64(header['stream_header'], 24), key)
            associated = magic + struct.pack('>I', len(raw)) + raw
            with _output(target) as (plain, temporary):
                remaining = header['plaintext_bytes']
                while remaining:
                    size = min(remaining, CHUNK_BYTES)
                    if struct.unpack('>I', _read_exact(encrypted, 4))[0] != size + _STREAM_OVERHEAD:
                        raise ValueError('Invalid frame length.')
                    block, tag = bindings.crypto_secretstream_xchacha20poly1305_pull(state, _read_exact(encrypted, size + _STREAM_OVERHEAD), ad=associated)
                    if tag != bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE or len(block) != size:
                        raise ValueError('Invalid data frame tag.')
                    plain.write(block)
                    remaining -= len(block)
                if struct.unpack('>I', _read_exact(encrypted, 4))[0] != _STREAM_OVERHEAD:
                    raise ValueError('Invalid final frame length.')
                final, tag = bindings.crypto_secretstream_xchacha20poly1305_pull(state, _read_exact(encrypted, _STREAM_OVERHEAD), ad=associated)
                if final or tag != bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL or encrypted.read(1):
                    raise ValueError('Missing final marker or trailing bytes.')
                _unchanged(encrypted, before)
                plain.flush()
                os.fsync(plain.fileno())
                _close_checked(plain)
                _publish(temporary, target)
            return _metadata(header)
    except (BackupCryptoError, OSError, ValueError, TypeError, OverflowError, RuntimeError, RecursionError, exceptions.CryptoError):
        raise BackupCryptoError(DECRYPT_ERROR) from None
