"""Real libsodium envelopes, synthetic files, bounded KDF work; no restore/model."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import struct

from nacl import bindings
import pytest

from core import backup_crypto as crypto

PASSWORD = 'A synthetic backup password 🌿'


@pytest.fixture(scope='module')
def envelope(tmp_path_factory):
    root = tmp_path_factory.mktemp('backup-crypto')
    source = root / 'synthetic-source.zip'
    # More than two full chunks, plus a short final data chunk.
    content = b'PK\x03\x04synthetic private fixture\x00' + bytes(range(256)) * 8192 + b'last-short-block'
    source.write_bytes(content)
    recovery = crypto.generate_recovery_key()
    encrypted = root / 'synthetic.alterabak'
    metadata = crypto.encrypt_file(source, encrypted, PASSWORD, recovery_key=recovery)
    return {'root': root, 'source': source, 'content': content, 'recovery': recovery,
            'encrypted': encrypted, 'metadata': metadata, 'bytes': encrypted.read_bytes()}


def split(raw):
    offset = len(crypto.MAGIC)
    size = struct.unpack('>I', raw[offset:offset + 4])[0]
    header_start, body_start = offset + 4, offset + 4 + size
    header = json.loads(raw[header_start:body_start])
    frames, cursor = [], body_start
    while cursor < len(raw):
        length = struct.unpack('>I', raw[cursor:cursor + 4])[0]
        frames.append(raw[cursor:cursor + 4 + length])
        cursor += 4 + length
    return header, frames


def replace_header(raw, header=None, *, encoded=None):
    original, frames = split(raw)
    encoded = crypto._canonical(header if header is not None else original) if encoded is None else encoded
    return crypto.MAGIC + struct.pack('>I', len(encoded)) + encoded + b''.join(frames)


def reject(raw, tmp_path, *, recovery=None, password=None):
    source, target = tmp_path / 'candidate.alterabak', tmp_path / 'must-not-exist.zip'
    source.write_bytes(raw)
    with pytest.raises(crypto.BackupCryptoError) as caught:
        crypto.decrypt_file(source, target, recovery_key=recovery, password=password)
    assert str(caught.value) == crypto.DECRYPT_ERROR
    assert not target.exists()
    assert not list(tmp_path.glob('.altera-crypto-*.part'))
    assert source.read_bytes() == raw


def test_real_multichunk_roundtrip_password_and_recovery(envelope, tmp_path):
    assert envelope['metadata'] == {'format': 'altera-encrypted-backup', 'version': 1, 'encrypted': True,
                                    'plaintext_bytes': len(envelope['content']), 'recovery_enabled': True}
    for label, credentials in [('password', {'password': PASSWORD}), ('recovery', {'recovery_key': envelope['recovery']})]:
        target = tmp_path / f'{label}.zip'
        assert crypto.decrypt_file(envelope['encrypted'], target, **credentials) == envelope['metadata']
        assert target.read_bytes() == envelope['content']
    assert not list(tmp_path.glob('.altera-crypto-*.part'))
    assert envelope['source'].read_bytes() == envelope['content']


def test_header_contains_only_bounded_nonpersonal_profile_and_wrapped_keys(envelope):
    raw = envelope['bytes']
    header, frames = split(raw)
    assert set(header) == {'format', 'version', 'cipher', 'chunk_bytes', 'plaintext_bytes', 'stream_header', 'password', 'recovery'}
    assert header['password']['opslimit'] == 3 and header['password']['memlimit'] == 64 * 1024 ** 2
    assert len(crypto._canonical(header)) <= crypto.MAX_HEADER_BYTES
    assert len(frames) == 4
    assert len(frames[-1]) == 4 + 17
    assert len(raw) - len(envelope['content']) <= crypto.MAX_OVERHEAD_BYTES < 64 * 1024
    assert crypto.MAX_PLAINTEXT_BYTES == 2 * 1024 ** 3
    assert crypto.MAX_CIPHERTEXT_BYTES == crypto.MAX_PLAINTEXT_BYTES + crypto.MAX_OVERHEAD_BYTES
    for secret in [PASSWORD.encode('utf-8'), envelope['recovery'].encode('ascii'), envelope['source'].name.encode(), b'synthetic private fixture']:
        assert secret not in raw


def test_password_only_empty_file_has_authenticated_final_marker(tmp_path):
    source, encrypted, target = (tmp_path / name for name in ('empty.zip', 'empty.alterabak', 'empty-copy.zip'))
    source.write_bytes(b'')
    metadata = crypto.encrypt_file(source, encrypted, 'twelve chars!')
    assert metadata['recovery_enabled'] is False
    header, frames = split(encrypted.read_bytes())
    assert header['recovery'] is None and len(frames) == 1
    crypto.decrypt_file(encrypted, target, password='twelve chars!')
    assert target.read_bytes() == b''


def test_fresh_archives_use_fresh_salt_nonces_stream_headers_and_archive_keys(envelope, tmp_path):
    encrypted = tmp_path / 'second.alterabak'
    crypto.encrypt_file(envelope['source'], encrypted, PASSWORD, recovery_key=envelope['recovery'])
    first, _ = split(envelope['bytes'])
    second, _ = split(encrypted.read_bytes())
    assert encrypted.read_bytes() != envelope['bytes']
    assert first['password']['salt'] != second['password']['salt']
    assert first['password']['nonce'] != second['password']['nonce']
    assert first['recovery']['nonce'] != second['recovery']['nonce']
    assert first['stream_header'] != second['stream_header']
    recovery = crypto._recovery(envelope['recovery'])
    def archive_key(header):
        wrapper = header['recovery']
        return bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(crypto._unb64(wrapper['wrapped_key'], 48), crypto._wrapper_ad(header, 'recovery', crypto.MAGIC), crypto._unb64(wrapper['nonce'], 24), recovery)
    assert archive_key(first) != archive_key(second)


def test_an_archive_written_under_the_old_her_names_still_opens(tmp_path, monkeypatch):
    # Write exactly what the code wrote before the rename, then read it with today's code.
    source = tmp_path / 'source.zip'
    source.write_bytes(b'PK\x03\x04synthetic legacy fixture' * 1000)
    recovery = crypto.generate_recovery_key().replace(crypto.RECOVERY_PREFIX, crypto.LEGACY_RECOVERY_PREFIX)
    with monkeypatch.context() as legacy:
        legacy.setattr(crypto, 'MAGIC', crypto.LEGACY_MAGIC)
        legacy.setattr(crypto, 'FORMAT', 'her-encrypted-backup')
        legacy.setattr(crypto, 'RECOVERY_SCHEME', 'her-recovery-v1')
        crypto.encrypt_file(source, tmp_path / 'old.herbak', PASSWORD, recovery_key=recovery)
    raw = (tmp_path / 'old.herbak').read_bytes()
    assert raw.startswith(b'HER-ENCRYPTED-BACKUP\n') and b'"her-recovery-v1"' in raw
    for name, credential in (('by-password.zip', {'password': PASSWORD}), ('by-key.zip', {'recovery_key': recovery})):
        metadata = crypto.decrypt_file(tmp_path / 'old.herbak', tmp_path / name, **credential)
        assert (tmp_path / name).read_bytes() == source.read_bytes() and metadata['format'] == 'her-encrypted-backup'
    reject(raw[:len(crypto.LEGACY_MAGIC)] + raw[len(crypto.LEGACY_MAGIC):].replace(b'her-recovery-v1', b'altera-recovery-v1'),
           tmp_path, password=PASSWORD)


def test_recovery_keys_are_versioned_random_canonical_32_byte_secrets():
    first, second = crypto.generate_recovery_key(), crypto.generate_recovery_key()
    assert first.startswith('ALTERA-RECOVERY-1-') and first != second
    assert len(crypto._recovery(first)) == 32 and len(crypto._recovery(second)) == 32
    assert first.isascii() and '=' not in first and not any(char.isspace() for char in first)


@pytest.mark.parametrize('credentials', [
    {'password': 'Wrong synthetic password'}, {'recovery_key': crypto.generate_recovery_key()},
    {}, {'password': PASSWORD, 'recovery_key': crypto.generate_recovery_key()},
    {'password': 'x' * 1025}, {'password': '\ud800'}, {'password': b'not a text credential'},
    {'recovery_key': 'HER-RECOVERY-2-' + 'A' * 43}, {'recovery_key': 'HER-RECOVERY-1-' + 'A' * 42},
    {'recovery_key': 'ALTERA-RECOVERY-2-' + 'A' * 43}, {'recovery_key': 'ALTERA-RECOVERY-1-' + 'A' * 42},
])
def test_wrong_or_malformed_credentials_have_one_generic_error_and_leave_no_plaintext(envelope, tmp_path, credentials):
    reject(envelope['bytes'], tmp_path, password=credentials.get('password'), recovery=credentials.get('recovery_key'))


@pytest.mark.parametrize('password', ['', 'elevenchars', '🙂' * 257, '\ud800' * 12, b'long enough bytes', None])
def test_invalid_creation_passwords_never_create_output_or_change_source(tmp_path, password):
    source, target = tmp_path / 'source.zip', tmp_path / 'encrypted.alterabak'
    source.write_bytes(b'synthetic data')
    with pytest.raises(crypto.BackupCryptoError) as caught:
        crypto.encrypt_file(source, target, password)
    assert 'synthetic data' not in str(caught.value)
    assert not target.exists() and not list(tmp_path.glob('.altera-crypto-*.part'))
    assert source.read_bytes() == b'synthetic data'


def test_invalid_creation_recovery_key_is_rejected_without_echoing_it(tmp_path):
    source, target = tmp_path / 'source.zip', tmp_path / 'encrypted.alterabak'
    source.write_bytes(b'fixture')
    secret = 'this-secret-is-not-a-valid-recovery-key'
    with pytest.raises(crypto.BackupCryptoError) as caught:
        crypto.encrypt_file(source, target, PASSWORD, recovery_key=secret)
    assert secret not in str(caught.value) and not target.exists()


def test_password_preflight_is_pure_bounded_and_does_not_normalize(monkeypatch):
    def no_kdf(*_):
        pytest.fail('Password validation must not run a KDF')
    monkeypatch.setattr(crypto, '_derive', no_kdf)
    assert crypto.validate_password(' ' * 12) is None
    assert crypto.validate_password('🙂' * 256) is None
    assert crypto._password(' e\u0301 exactly  ', creating=True) == ' e\u0301 exactly  '.encode('utf-8')
    for value in ['a' * 11, '🙂' * 257, '\ud800' * 12]:
        with pytest.raises(crypto.BackupCryptoError):
            crypto.validate_password(value)


@pytest.mark.parametrize('mutation', ['magic', 'header length', 'truncated header', 'missing final', 'trailing byte', 'oversize frame', 'short frame', 'truncated ciphertext'])
def test_framing_errors_are_rejected_before_the_expensive_kdf(envelope, tmp_path, monkeypatch, mutation):
    raw = envelope['bytes']
    header, frames = split(raw)
    body_start = len(crypto.MAGIC) + 4 + len(crypto._canonical(header))
    candidates = {
        'magic': b'X' + raw[1:],
        'header length': crypto.MAGIC + struct.pack('>I', crypto.MAX_HEADER_BYTES + 1) + raw[len(crypto.MAGIC) + 4:],
        'truncated header': raw[:len(crypto.MAGIC) + 6],
        'missing final': raw[:-len(frames[-1])],
        'trailing byte': raw + b'X',
        'oversize frame': raw[:body_start] + struct.pack('>I', crypto.CHUNK_BYTES + 18) + raw[body_start + 4:],
        'short frame': raw[:body_start] + struct.pack('>I', 1) + raw[body_start + 4:],
        'truncated ciphertext': raw[:-1],
    }
    def no_kdf(*_):
        pytest.fail('Malformed framing reached the KDF')
    monkeypatch.setattr(crypto, '_derive', no_kdf)
    reject(candidates[mutation], tmp_path, password=PASSWORD)


@pytest.mark.parametrize('mutation', ['KDF memory', 'KDF operations', 'KDF name', 'unknown field', 'large plaintext', 'boolean plaintext', 'bad stream header', 'bad salt', 'bad nonce', 'bad wrapped key', 'recovery scheme', 'duplicate key', 'nonfinite', 'noncanonical', 'unsupported version'])
def test_header_resource_and_schema_abuse_is_rejected_before_kdf(envelope, tmp_path, monkeypatch, mutation):
    header, _ = split(envelope['bytes'])
    if mutation == 'KDF memory':
        header['password']['memlimit'] = 2 ** 63
    elif mutation == 'KDF operations':
        header['password']['opslimit'] = 1000000
    elif mutation == 'KDF name':
        header['password']['kdf'] = 'scrypt'
    elif mutation == 'unknown field':
        header['personal_name'] = 'do not process arbitrary metadata'
    elif mutation == 'large plaintext':
        header['plaintext_bytes'] = crypto.MAX_PLAINTEXT_BYTES + 1
    elif mutation == 'boolean plaintext':
        header['plaintext_bytes'] = True
    elif mutation == 'bad stream header':
        header['stream_header'] = 'A'
    elif mutation == 'bad salt':
        header['password']['salt'] = 'A'
    elif mutation == 'bad nonce':
        header['password']['nonce'] = 'A'
    elif mutation == 'bad wrapped key':
        header['password']['wrapped_key'] = 'A'
    elif mutation == 'recovery scheme':
        header['recovery']['scheme'] = 'unknown'
    elif mutation == 'unsupported version':
        header['version'] = 2
    raw_header = crypto._canonical(header)
    if mutation == 'duplicate key':
        raw_header = b'{"version":1,' + raw_header[1:]
    elif mutation == 'nonfinite':
        raw_header = raw_header.replace(b'"version":1', b'"version":NaN')
    elif mutation == 'noncanonical':
        raw_header = b' ' + raw_header
    def no_kdf(*_):
        pytest.fail('Untrusted resource/schema parameters reached the KDF')
    monkeypatch.setattr(crypto, '_derive', no_kdf)
    reject(replace_header(envelope['bytes'], encoded=raw_header), tmp_path, password=PASSWORD)


@pytest.mark.parametrize('mutation', ['data bit', 'final bit', 'reordered chunks', 'duplicate chunk', 'header nonce', 'other wrapper ciphertext'])
def test_real_authentication_rejects_tampering_and_cleans_its_partial_plaintext(envelope, tmp_path, mutation):
    header, frames = split(envelope['bytes'])
    prefix = envelope['bytes'][:len(crypto.MAGIC) + 4 + len(crypto._canonical(header))]
    if mutation == 'data bit':
        changed = bytearray(frames[1])
        changed[50] ^= 1
        frames[1] = bytes(changed)
    elif mutation == 'final bit':
        changed = bytearray(frames[-1])
        changed[-1] ^= 1
        frames[-1] = bytes(changed)
    elif mutation == 'reordered chunks':
        frames[0], frames[1] = frames[1], frames[0]
    elif mutation == 'duplicate chunk':
        frames[1] = frames[0]
    elif mutation == 'header nonce':
        nonce = bytearray(crypto._unb64(header['recovery']['nonce'], 24))
        nonce[0] ^= 1
        header['recovery']['nonce'] = crypto._b64(bytes(nonce))
        prefix = crypto.MAGIC + struct.pack('>I', len(crypto._canonical(header))) + crypto._canonical(header)
    elif mutation == 'other wrapper ciphertext':
        wrapped = bytearray(crypto._unb64(header['password']['wrapped_key'], 48))
        wrapped[0] ^= 1
        header['password']['wrapped_key'] = crypto._b64(bytes(wrapped))
        prefix = crypto.MAGIC + struct.pack('>I', len(crypto._canonical(header))) + crypto._canonical(header)
    reject(prefix + b''.join(frames), tmp_path, recovery=envelope['recovery'])


def retagged(envelope, *, data_tag=None, final_tag=None):
    """Authenticated adversarial fixture: real recovery AEAD and secretstream."""
    header, _ = split(envelope['bytes'])
    key = bindings.crypto_secretstream_xchacha20poly1305_keygen()
    state = bindings.crypto_secretstream_xchacha20poly1305_state()
    header['stream_header'] = crypto._b64(bindings.crypto_secretstream_xchacha20poly1305_init_push(state, key))
    header['recovery']['nonce'] = crypto._b64(os.urandom(24))
    # Recovery authentication is real. The password ciphertext stays opaque, as
    # it may be to any recovery-key holder; whole-header stream AD still binds it.
    header['recovery']['wrapped_key'] = crypto._b64(bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(key, crypto._wrapper_ad(header, 'recovery', crypto.MAGIC), crypto._unb64(header['recovery']['nonce'], 24), crypto._recovery(envelope['recovery'])))
    raw = crypto._canonical(header)
    associated = crypto.MAGIC + struct.pack('>I', len(raw)) + raw
    frames = []
    for position in range(0, len(envelope['content']), crypto.CHUNK_BYTES):
        block = envelope['content'][position:position + crypto.CHUNK_BYTES]
        tag = bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE if data_tag is None else data_tag
        encrypted = bindings.crypto_secretstream_xchacha20poly1305_push(state, block, ad=associated, tag=tag)
        frames.append(struct.pack('>I', len(encrypted)) + encrypted)
    tag = bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL if final_tag is None else final_tag
    final = bindings.crypto_secretstream_xchacha20poly1305_push(state, b'', ad=associated, tag=tag)
    return associated + b''.join(frames) + struct.pack('>I', len(final)) + final


def test_authenticated_push_tag_cannot_substitute_for_data_message(envelope, tmp_path):
    reject(retagged(envelope, data_tag=bindings.crypto_secretstream_xchacha20poly1305_TAG_PUSH), tmp_path, recovery=envelope['recovery'])


def test_authenticated_message_tag_cannot_substitute_for_final_marker(envelope, tmp_path):
    reject(retagged(envelope, final_tag=bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE), tmp_path, recovery=envelope['recovery'])


def test_existing_destination_source_and_unrelated_files_are_never_overwritten(envelope, tmp_path):
    target = tmp_path / 'existing.zip'
    target.write_bytes(b'keep existing destination')
    unrelated = tmp_path / '.altera-crypto-unrelated.part'
    unrelated.write_bytes(b'not owned by this operation')
    for operation in [lambda: crypto.decrypt_file(envelope['encrypted'], target, recovery_key=envelope['recovery']),
                      lambda: crypto.encrypt_file(envelope['source'], target, PASSWORD),
                      lambda: crypto.encrypt_file(envelope['source'], envelope['source'], PASSWORD)]:
        with pytest.raises(crypto.BackupCryptoError):
            operation()
    assert target.read_bytes() == b'keep existing destination'
    assert envelope['source'].read_bytes() == envelope['content']
    assert unrelated.read_bytes() == b'not owned by this operation'


def test_atomic_publication_never_exposes_a_partial_destination_and_honors_late_collision(envelope, tmp_path, monkeypatch):
    target = tmp_path / 'collision.zip'
    observed = []
    original = crypto._publish
    def collision(temporary, destination):
        assert not destination.exists()
        observed.append(Path(temporary).read_bytes() == envelope['content'])
        destination.write_bytes(b'competing owner file')
        original(temporary, destination)
    monkeypatch.setattr(crypto, '_publish', collision)
    with pytest.raises(crypto.BackupCryptoError, match='could not be opened'):
        crypto.decrypt_file(envelope['encrypted'], target, recovery_key=envelope['recovery'])
    assert observed == [True]
    assert target.read_bytes() == b'competing owner file'
    assert not list(tmp_path.glob('.altera-crypto-*.part'))


def test_publication_failure_cleans_only_own_authenticated_plaintext_temp(envelope, tmp_path, monkeypatch):
    unrelated = tmp_path / '.altera-crypto-do-not-touch.part'
    unrelated.write_bytes(b'unrelated')
    def fail(*_):
        raise OSError('synthetic private path must not be in public error')
    monkeypatch.setattr(crypto, '_publish', fail)
    target = tmp_path / 'result.zip'
    with pytest.raises(crypto.BackupCryptoError) as caught:
        crypto.decrypt_file(envelope['encrypted'], target, recovery_key=envelope['recovery'])
    assert str(caught.value) == crypto.DECRYPT_ERROR
    assert not target.exists()
    assert list(tmp_path.glob('.altera-crypto-*.part')) == [unrelated]


def test_encryption_plaintext_bound_is_checked_before_kdf_and_output(tmp_path, monkeypatch):
    source, target = tmp_path / 'too-large.zip', tmp_path / 'result.alterabak'
    source.write_bytes(b'x' * 17)
    monkeypatch.setattr(crypto, 'MAX_PLAINTEXT_BYTES', 16)
    def no_kdf(*_):
        pytest.fail('Oversized plaintext reached the KDF')
    monkeypatch.setattr(crypto, '_derive', no_kdf)
    with pytest.raises(crypto.BackupCryptoError, match='limit'):
        crypto.encrypt_file(source, target, PASSWORD)
    assert not target.exists() and source.read_bytes() == b'x' * 17


def test_ciphertext_bound_is_checked_before_kdf(envelope, tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, 'MAX_CIPHERTEXT_BYTES', len(envelope['bytes']) - 1)
    def no_kdf(*_):
        pytest.fail('Oversized ciphertext reached the KDF')
    monkeypatch.setattr(crypto, '_derive', no_kdf)
    reject(envelope['bytes'], tmp_path, password=PASSWORD)


def test_streaming_decryption_never_requests_an_unbounded_or_oversized_read(envelope, tmp_path, monkeypatch):
    original = crypto._input
    requested = []
    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream
        def read(self, size=-1):
            assert 0 <= size <= crypto.CHUNK_BYTES + 17
            requested.append(size)
            return self.stream.read(size)
        def __getattr__(self, key):
            return getattr(self.stream, key)
    @contextmanager
    def bounded(source, destination):
        with original(source, destination) as (stream, target, info):
            yield BoundedReader(stream), target, info
    monkeypatch.setattr(crypto, '_input', bounded)
    target = tmp_path / 'bounded.zip'
    crypto.decrypt_file(envelope['encrypted'], target, recovery_key=envelope['recovery'])
    assert max(requested) == crypto.CHUNK_BYTES + 17
    assert target.read_bytes() == envelope['content']


def test_source_change_during_encryption_aborts_before_publication(tmp_path, monkeypatch):
    source, target = tmp_path / 'changing.zip', tmp_path / 'result.alterabak'
    source.write_bytes(b'A' * (crypto.CHUNK_BYTES + 20))
    original = crypto._input
    class ChangingReader:
        changed = False
        def __init__(self, stream):
            self.stream = stream
        def read(self, size=-1):
            value = self.stream.read(size)
            if not self.changed:
                self.changed = True
                before = source.stat()
                with source.open('r+b') as writer:
                    writer.write(b'B')
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
            return value
        def __getattr__(self, key):
            return getattr(self.stream, key)
    @contextmanager
    def changing(candidate, destination):
        with original(candidate, destination) as (stream, result, info):
            yield ChangingReader(stream), result, info
    monkeypatch.setattr(crypto, '_input', changing)
    with pytest.raises(crypto.BackupCryptoError):
        crypto.encrypt_file(source, target, PASSWORD)
    assert not target.exists() and not list(tmp_path.glob('.altera-crypto-*.part'))
    assert source.read_bytes().startswith(b'B')
