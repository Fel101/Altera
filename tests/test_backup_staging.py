"""Private backup staging and fail-closed recovery use isolated real files only."""
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
from types import SimpleNamespace as NS
import uuid

import pytest

from core import backups
from core.backups import BackupError, BackupService, PREVIEW_TTL, STAGING_OWNER
from core.state_gate import StateBusy, StateGate
from test_backups import backup_world, contents, newer  # noqa: F401

NOW = 1793203200.
PASSWORD = 'Synthetic archive password'
RECOVERY_PASSWORD = 'Synthetic recovery password'


@pytest.mark.parametrize('failure', ['unlock', 'close'])
@pytest.mark.parametrize('operation', ['direct', 'sweep'])
def test_unconfirmed_staging_lock_cleanup_fences_work_and_releases_local_mutex(backup_world, monkeypatch, failure, operation):
    service = backup_world.service
    original_open, original_close = backups.os.open, backups.os.close
    descriptors, closed = set(), []

    def open_lock(path, flags, *args):
        descriptor = original_open(path, flags, *args)
        if str(path) == str(service.directory / '.staging.lock'):
            descriptors.add(descriptor)
        return descriptor

    def close_lock(descriptor):
        if descriptor in descriptors:
            descriptors.remove(descriptor)
            closed.append(descriptor)
            original_close(descriptor)
            if failure == 'close':
                raise OSError('synthetic sensitive close detail')
            return
        return original_close(descriptor)

    monkeypatch.setattr(backups.os, 'open', open_lock)
    monkeypatch.setattr(backups.os, 'close', close_lock)
    if backups.os.name == 'nt':
        import msvcrt
        original_locking = msvcrt.locking

        def locking(descriptor, mode, count):
            result = original_locking(descriptor, mode, count)
            if failure == 'unlock' and descriptor in descriptors and mode == msvcrt.LK_UNLCK:
                raise OSError('synthetic sensitive unlock detail')
            return result

        monkeypatch.setattr(msvcrt, 'locking', locking)
    else:
        import fcntl
        original_locking = fcntl.flock

        def locking(descriptor, mode):
            result = original_locking(descriptor, mode)
            if failure == 'unlock' and descriptor in descriptors and mode == fcntl.LOCK_UN:
                raise OSError('synthetic sensitive unlock detail')
            return result

        monkeypatch.setattr(fcntl, 'flock', locking)
    with pytest.raises(backups.BackupCleanupError, match='cleanup could not be confirmed') as error:
        if operation == 'sweep':
            service.cleanup_staging()
        else:
            with service._staging_operation():
                pass
    assert 'sensitive' not in str(error.value) and len(closed) == 1
    assert service.gate.draining and service.staging_lock.acquire(blocking=False)
    service.staging_lock.release()
    with pytest.raises(StateBusy, match='shutting down'):
        with service._staging_operation():
            pytest.fail('A fenced service must not start another operation.')


def test_staging_rechecks_drain_after_acquiring_mutex(backup_world, monkeypatch):
    service = backup_world.service
    class DrainingLock:
        released = False

        def acquire(self, blocking=False):
            service.gate.begin_shutdown()
            return True

        def release(self):
            self.released = True

    lock = DrainingLock()
    service.staging_lock = lock
    monkeypatch.setattr(service, '_check_staging', lambda: pytest.fail('Drained staging must not access files.'))
    with pytest.raises(StateBusy, match='shutting down'):
        with service._staging_operation():
            pytest.fail('Drained staging must not start.')
    assert lock.released


@pytest.fixture
def clock(monkeypatch):
    moment = [NOW]
    monkeypatch.setattr(backups.time, 'time', lambda: moment[0])
    return moment


def preview_folder(service, *, created=NOW - 2 * PREVIEW_TTL, receipt=True, owner=True, token=None):
    token = token or uuid.uuid4().hex
    folder = service.staging / token
    folder.mkdir(mode=0o700)
    upload = b'PK synthetic private upload'
    (folder / 'upload.zip').write_bytes(upload)
    if owner:
        (folder / STAGING_OWNER).write_text(json.dumps({'format': 'her-preview-stage', 'version': 1,
            'token': token, 'created_at': created}), 'utf-8')
    if receipt:
        (folder / 'preview.json').write_text(json.dumps({'token': token, 'expires_at': created + PREVIEW_TTL,
            'sha256': hashlib.sha256(upload).hexdigest(), 'restore_connections': False,
            'counts': {}, 'created_at': '2026-10-28T00:00:00+00:00', 'version': 1, 'encrypted': False}), 'utf-8')
    return folder


def faulted_restore(w, *, encrypted=False):
    create = {'password': PASSWORD} if encrypted else {}
    saved = w.service.create(**create)
    newer(w)
    expected = contents(w)
    preview = w.service.preview(w.service.archive_path(saved['id']), **create)
    w.memory.fail = 2
    apply = {'password': PASSWORD, 'recovery_password': RECOVERY_PASSWORD} if encrypted else {}
    with pytest.raises(BackupError, match='automatic rollback failed'):
        w.service.apply(preview['token'], **apply)
    record = w.service._recovery_record()
    protected = w.service.work / record['stage'] / 'protected-settings.yaml'
    return record, protected, expected


def test_new_recovery_journal_binds_protection_and_encrypted_restart_preserves_current_credentials(backup_world):
    w = backup_world
    record, protected, expected = faulted_restore(w, encrypted=True)
    assert record['protected_settings_sha256'] == hashlib.sha256(protected.read_bytes()).hexdigest()
    assert PASSWORD not in w.service.journal.read_text() and RECOVERY_PASSWORD not in w.service.journal.read_text()
    restarted = BackupService(w.root, w.store, w.memory, gate=StateGate())
    preview = restarted.preview(restarted.archive_path(record['recovery_backup']), password=RECOVERY_PASSWORD)
    restarted.apply(preview['token'], password=RECOVERY_PASSWORD)
    assert contents(w) == expected
    assert not restarted.gate.fault and not restarted.journal.exists()


@pytest.mark.parametrize('mutation', ['missing', 'bad_yaml', 'valid_mapping_changed', 'not_mapping', 'empty', 'directory', 'bad_hash', 'hash_type', 'unreadable'])
def test_missing_or_corrupt_recovery_protection_never_replaces_current_state(backup_world, monkeypatch, mutation):
    w = backup_world
    record, protected, _ = faulted_restore(w)
    if mutation == 'missing':
        protected.unlink()
    elif mutation == 'bad_yaml':
        protected.write_text('llm: [unfinished', 'utf-8')
    elif mutation == 'valid_mapping_changed':
        protected.write_text('llm: {provider: changed}\n', 'utf-8')
    elif mutation in {'not_mapping', 'empty'}:
        # Old journals do not have a digest, but still require a readable mapping.
        record.pop('protected_settings_sha256')
        protected.write_text('[]\n' if mutation == 'not_mapping' else '', 'utf-8')
    elif mutation == 'directory':
        protected.unlink()
        protected.mkdir(mode=0o700)
    elif mutation == 'bad_hash':
        record['protected_settings_sha256'] = '0' * 64
    elif mutation == 'hash_type':
        record['protected_settings_sha256'] = []
    else:
        read = backups._regular_bytes
        def unavailable(path, limit):
            if path == protected:
                raise PermissionError('Synthetic unreadable recovery protection')
            return read(path, limit)
        monkeypatch.setattr(backups, '_regular_bytes', unavailable)
    w.service.journal.write_text(json.dumps(record), 'utf-8')
    before = contents(w)
    restarted = BackupService(w.root, w.store, w.memory, gate=StateGate())
    preview = restarted.preview(restarted.archive_path(record['recovery_backup']))
    with pytest.raises(BackupError):
        restarted.apply(preview['token'])
    assert restarted.gate.fault and restarted.journal.exists()
    assert contents(w) == before
    assert (restarted.work / record['stage']).is_dir()


def test_legacy_journal_without_hash_still_requires_and_preserves_intact_protection(backup_world):
    w = backup_world
    record, protected, expected = faulted_restore(w)
    record.pop('protected_settings_sha256')
    w.service.journal.write_text(json.dumps(record), 'utf-8')
    assert protected.is_file()
    restarted = BackupService(w.root, w.store, w.memory, gate=StateGate())
    preview = restarted.preview(restarted.archive_path(record['recovery_backup']))
    restarted.apply(preview['token'])
    assert contents(w) == expected and not restarted.gate.fault


def test_expired_owned_completed_and_interrupted_uploads_are_removed_but_recent_and_unknown_are_kept(backup_world, clock):
    service = backup_world.service
    expired = preview_folder(service)
    interrupted = preview_folder(service, receipt=False)
    legacy = preview_folder(service, owner=False)
    recent = preview_folder(service, created=NOW)
    unknown = preview_folder(service, receipt=False, owner=False)
    user_folder = preview_folder(service)
    (user_folder / 'user-notes.txt').write_text('Keep this unknown user file.', 'utf-8')
    non_token = service.staging / 'user-project'
    non_token.mkdir()
    (non_token / 'notes.txt').write_text('Keep', 'utf-8')
    assert service.cleanup_staging() == 3
    assert not expired.exists() and not interrupted.exists() and not legacy.exists()
    assert recent.is_dir() and unknown.is_dir() and user_folder.is_dir() and non_token.is_dir()
    assert service.cleanup_staging() == 0


def test_cleanup_is_bounded_and_startup_expires_only_owned_uploads(backup_world, clock, monkeypatch):
    w = backup_world
    folders = [preview_folder(w.service) for _ in range(4)]
    monkeypatch.setattr(backups, 'STAGING_CLEANUP_LIMIT', 3)
    assert w.service.cleanup_staging() == 3
    assert sum(folder.exists() for folder in folders) == 1
    restarted = BackupService(w.root, w.store, w.memory, gate=StateGate())
    assert not any(folder.exists() for folder in folders)
    assert restarted.cleanup_staging() == 0


def cleanup_scan_fixture(monkeypatch, service, paths, *, fail_first=False):
    """Order real candidate paths predictably, tracking consumed entries/close."""
    original = backups.os.scandir
    scans = []

    class Scan:
        def __init__(self, names, fail):
            self.names, self.fail = iter(names), fail
            self.yielded, self.closed = 0, False

        def __next__(self):
            if self.fail and self.yielded == 1:
                raise OSError('Synthetic directory enumeration failure')
            name = next(self.names)
            self.yielded += 1
            return NS(name=name)

        def close(self):
            self.closed = True

    def scandir(path):
        if not isinstance(path, (str, os.PathLike)) or Path(path) != service.staging:
            return original(path)
        scan = Scan([item.name for item in paths if item.exists()], fail_first and not scans)
        scans.append(scan)
        return scan

    monkeypatch.setattr(backups.os, 'scandir', scandir)
    return scans


def test_bounded_cleanup_progresses_past_preserved_entries_and_closes_finished_scan(backup_world, clock, monkeypatch):
    service = backup_world.service
    unknown = [preview_folder(service, owner=False, receipt=False) for _ in range(6)]
    expired = [preview_folder(service) for _ in range(2)]
    paths = unknown + expired
    scans = cleanup_scan_fixture(monkeypatch, service, paths)
    monkeypatch.setattr(backups, 'STAGING_CLEANUP_LIMIT', 3)
    removed = []
    for _ in range(3):
        before = sum(scan.yielded for scan in scans)
        removed.append(service.cleanup_staging())
        assert sum(scan.yielded for scan in scans) - before <= 3
    assert removed == [0, 0, 2]
    assert all(path.is_dir() for path in unknown)
    assert not any(path.exists() for path in expired)
    assert len(scans) == 1 and scans[0].closed and service._cleanup_scan is None
    # Entries created after a completed cycle are picked up by a fresh scan.
    newest = preview_folder(service)
    paths.append(newest)
    for _ in range(3):
        service.cleanup_staging()
    assert not newest.exists()


def test_resumed_cleanup_revalidates_a_previously_listed_directory(backup_world, clock, monkeypatch):
    service = backup_world.service
    unknown = [preview_folder(service, owner=False, receipt=False) for _ in range(3)]
    changed, expired = preview_folder(service), preview_folder(service)
    scans = cleanup_scan_fixture(monkeypatch, service, unknown + [changed, expired])
    monkeypatch.setattr(backups, 'STAGING_CLEANUP_LIMIT', 3)
    assert service.cleanup_staging() == 0
    (changed / 'keep.txt').write_text('Unknown content added between cleanup passes.', 'utf-8')
    assert service.cleanup_staging() == 1
    assert (changed / 'keep.txt').read_text('utf-8') == 'Unknown content added between cleanup passes.'
    assert not expired.exists() and scans[0].closed


def test_cleanup_closes_a_failed_scan_and_safely_restarts(backup_world, clock, monkeypatch):
    service = backup_world.service
    unknown = preview_folder(service, owner=False, receipt=False)
    expired = preview_folder(service)
    scans = cleanup_scan_fixture(monkeypatch, service, [unknown, expired], fail_first=True)
    assert service.cleanup_staging() == 0
    assert len(scans) == 1 and scans[0].closed and service._cleanup_scan is None
    assert expired.is_dir()
    assert service.cleanup_staging() == 1
    assert len(scans) == 2 and scans[1].closed
    assert unknown.is_dir() and not expired.exists()


@pytest.mark.parametrize('mutation', ['bad_owner_json', 'owner_token', 'owner_version', 'owner_time_nan', 'owner_time_future',
                                    'bad_receipt_json', 'receipt_token', 'receipt_nan', 'receipt_boolean', 'receipt_negative',
                                    'receipt_sha', 'unknown_receipt_field', 'oversized_metadata'])
def test_cleanup_preserves_malformed_or_ambiguous_metadata(backup_world, clock, mutation):
    service = backup_world.service
    folder = preview_folder(service)
    path = folder / (STAGING_OWNER if mutation.startswith('owner') or mutation == 'bad_owner_json' else 'preview.json')
    if mutation in {'bad_owner_json', 'bad_receipt_json'}:
        path.write_text('{broken', 'utf-8')
    elif mutation == 'oversized_metadata':
        path.write_text('x' * 17000, 'utf-8')
    else:
        value = json.loads(path.read_text())
        if mutation in {'owner_token', 'receipt_token'}:
            value['token'] = '0' * 32
        elif mutation == 'owner_version':
            value['version'] = True
        elif mutation == 'owner_time_nan':
            value['created_at'] = float('nan')
        elif mutation == 'owner_time_future':
            value['created_at'] = NOW + 1
        elif mutation == 'receipt_nan':
            value['expires_at'] = float('nan')
        elif mutation == 'receipt_boolean':
            value['expires_at'] = True
        elif mutation == 'receipt_negative':
            value['expires_at'] = -1
        elif mutation == 'receipt_sha':
            value['sha256'] = []
        else:
            value['user_note'] = 'Unknown metadata must not be discarded.'
        path.write_text(json.dumps(value), 'utf-8')
    original = {p.name: p.read_bytes() for p in folder.iterdir()}
    assert service.cleanup_staging() == 0
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == original


@pytest.mark.parametrize('moment', [float('nan'), float('inf'), -1, 0, True, 'expired'])
def test_cleanup_never_guesses_from_an_invalid_clock(backup_world, moment):
    folder = preview_folder(backup_world.service)
    assert backup_world.service.cleanup_staging(now=moment) == 0
    assert folder.is_dir()


def test_recovery_referenced_upload_and_work_stage_survive_cleanup_and_malformed_journal_blocks_sweep(backup_world, clock):
    service = backup_world.service
    protected, ordinary = preview_folder(service), preview_folder(service)
    stage = service.work / ('restore-' + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    (stage / 'protected-settings.yaml').write_text('private: retained\n', 'utf-8')
    record = {'stage': stage.name, 'token': protected.name, 'recovery_backup': 'her-20261028T000000Z-0123456789ab.zip'}
    service.journal.write_text(json.dumps(record), 'utf-8')
    assert service.cleanup_staging() == 1
    assert protected.is_dir() and not ordinary.exists() and stage.is_dir()
    other = preview_folder(service)
    service.journal.write_text('{bad recovery journal', 'utf-8')
    assert service.cleanup_staging() == 0
    assert protected.is_dir() and other.is_dir() and stage.is_dir()


def test_cleanup_and_another_service_never_delete_an_active_preview(backup_world, clock):
    w = backup_world
    saved = w.service.create()
    started, finish = threading.Event(), threading.Event()
    results, errors = [], []
    class SlowUpload:
        def save(self, target):
            target.write_bytes(w.service.archive_path(saved['id']).read_bytes())
            clock[0] += PREVIEW_TTL * 2
            started.set()
            assert finish.wait(10)
    def upload():
        try:
            results.append(w.service.preview(SlowUpload()))
        except Exception as error:
            errors.append(error)
    worker = threading.Thread(target=upload)
    worker.start()
    assert started.wait(10)
    try:
        assert w.service.cleanup_staging() == 0
        other = BackupService(w.root, w.store, w.memory, gate=StateGate())
        assert other.cleanup_staging() == 0
        assert len(list(w.service.staging.iterdir())) == 1
        with pytest.raises(StateBusy):
            other.preview(w.service.archive_path(saved['id']))
    finally:
        finish.set()
        worker.join(10)
    assert not errors and len(results) == 1
    clock[0] += PREVIEW_TTL + 1
    assert w.service.cleanup_staging() == 1


def test_os_lock_also_blocks_cleanup_with_an_independent_thread_guard(backup_world, clock):
    w = backup_world
    folder = preview_folder(w.service)
    other = BackupService(w.root, w.store, w.memory, gate=StateGate())
    # Construct after a recent preview so startup does not expire this fixture.
    folder = preview_folder(w.service)
    other.staging_lock = threading.Lock()
    with w.service._staging_operation():
        assert other.cleanup_staging() == 0
        assert folder.is_dir()
    assert other.cleanup_staging() == 1


def test_active_apply_is_not_swept_and_successful_apply_retries_keep_their_receipt(backup_world, clock, monkeypatch):
    w = backup_world
    saved = w.service.create()
    preview = w.service.preview(w.service.archive_path(saved['id']))
    started, finish = threading.Event(), threading.Event()
    normalize = w.service._normalize_database
    results, errors = [], []
    def paused(*args):
        clock[0] += 2 * PREVIEW_TTL
        started.set()
        assert finish.wait(10)
        return normalize(*args)
    monkeypatch.setattr(w.service, '_normalize_database', paused)
    def apply():
        try:
            results.append(w.service.apply(preview['token']))
        except Exception as error:
            errors.append(error)
    worker = threading.Thread(target=apply)
    worker.start()
    assert started.wait(10)
    try:
        assert w.service.cleanup_staging() == 0
        assert (w.service.staging / preview['token'] / 'upload.zip').is_file()
    finally:
        finish.set()
        worker.join(10)
    assert not errors and len(results) == 1
    assert w.service.apply(preview['token']) == results[0]


@pytest.mark.parametrize('repairing', [False, True])
@pytest.mark.parametrize('cleanup_error', [PermissionError, BackupError])
def test_committed_restore_cleanup_failure_keeps_success_and_retryable_receipt(backup_world, clock, monkeypatch,
                                                                              repairing, cleanup_error):
    w = backup_world
    expected = None
    if repairing:
        record, _, expected = faulted_restore(w, encrypted=True)
        service = BackupService(w.root, w.store, w.memory, gate=StateGate())
        source = service.archive_path(record['recovery_backup'])
        source_password = RECOVERY_PASSWORD
        recovery = {}
    else:
        saved = w.service.create(password=PASSWORD)
        newer(w)
        service = w.service
        source = service.archive_path(saved['id'])
        source_password = PASSWORD
        recovery = {'recovery_password': RECOVERY_PASSWORD}
    preview = service.preview(source, password=source_password)
    folder = service.staging / preview['token']
    original_remove = service._remove_preview
    cleanup_attempts = []

    def unavailable_once(path):
        cleanup_attempts.append(path)
        if len(cleanup_attempts) == 1:
            raise cleanup_error('Synthetic postcommit preview cleanup failure')
        return original_remove(path)

    monkeypatch.setattr(service, '_remove_preview', unavailable_once)
    result = service.apply(preview['token'], password=source_password, **recovery)
    assert result['status'] == 'restored'
    assert not service.gate.fault and not service.journal.exists()
    assert folder.is_dir() and (folder / STAGING_OWNER).is_file()
    assert (service.results / f"{preview['token']}.json").is_file()
    if expected is not None:
        assert contents(w) == expected
    else:
        assert [message.content for message in w.store.get_messages(w.sid)] == ['Hello', 'Hi']
        assert (w.root / 'workspace/note.txt').read_text('utf-8') == 'Original workspace note'
    committed = contents(w)
    epoch = w.store.dataset_epoch()['value']
    archive_ids = [item['id'] for item in service.list()]

    def no_more_restore(*args, **kwargs):
        pytest.fail('A completed receipt must not decrypt, snapshot or restore again')

    from core import backup_crypto
    monkeypatch.setattr(backup_crypto, 'decrypt_file', no_more_restore)
    monkeypatch.setattr(service, '_create', no_more_restore)
    monkeypatch.setattr(w.memory, 'restore_snapshot', no_more_restore)
    assert service.apply(preview['token']) == result
    assert folder.is_dir(), 'Unexpired owned uploads are retained for safe bounded cleanup'
    clock[0] += PREVIEW_TTL + 1
    # Repair also releases the interrupted restore's previously protected upload.
    assert service.cleanup_staging() == (2 if repairing else 1)
    assert not folder.exists()
    assert service.apply(preview['token']) == result
    assert contents(w) == committed and w.store.dataset_epoch()['value'] == epoch
    assert [item['id'] for item in service.list()] == archive_ids
    assert not service.gate.fault and not service.journal.exists()


def test_expired_apply_rejects_then_removes_its_owned_upload(backup_world, clock):
    w = backup_world
    saved = w.service.create()
    preview = w.service.preview(w.service.archive_path(saved['id']))
    clock[0] += PREVIEW_TTL + 1
    before = contents(w)
    with pytest.raises(BackupError, match='expired'):
        w.service.apply(preview['token'])
    assert contents(w) == before and not (w.service.staging / preview['token']).exists()


@pytest.mark.parametrize('mode', [0o755, 0o777, 0o750, 0o500])
def test_private_permission_policy_refuses_exposure_without_chmod(mode, monkeypatch):
    current = 42
    monkeypatch.setattr(backups.os, 'geteuid', lambda: current, raising=False)
    info = NS(st_mode=stat.S_IFDIR | mode, st_uid=current)
    with pytest.raises(BackupError, match='permissions'):
        backups._private_stat(info, posix=True)
    assert stat.S_IMODE(info.st_mode) == mode
    backups._private_stat(NS(st_mode=stat.S_IFDIR | 0o700, st_uid=current), posix=True)
    with pytest.raises(BackupError, match='another owner'):
        backups._private_stat(NS(st_mode=stat.S_IFDIR | 0o700, st_uid=current + 1), posix=True)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX mode bits have no Windows ACL meaning')
def test_existing_exposed_staging_is_refused_without_changing_permissions(backup_world):
    w = backup_world
    w.service.work.chmod(0o755)
    with pytest.raises(BackupError, match='permissions'):
        BackupService(w.root, w.store, w.memory, gate=StateGate())
    assert stat.S_IMODE(w.service.work.stat().st_mode) == 0o755


def test_reparse_staging_path_is_refused_without_touching_its_files(backup_world, monkeypatch):
    w = backup_world
    private = w.service.work / 'keep.txt'
    private.write_text('Keep private staging content.', 'utf-8')
    lstat = Path.lstat
    def reparse(path, *args, **kwargs):
        actual = lstat(path, *args, **kwargs)
        if path == w.service.work:
            return NS(st_mode=actual.st_mode, st_uid=actual.st_uid, st_file_attributes=0x400)
        return actual
    monkeypatch.setattr(Path, 'lstat', reparse)
    with pytest.raises(BackupError, match='linked'):
        BackupService(w.root, w.store, w.memory, gate=StateGate())
    assert private.read_text() == 'Keep private staging content.'


def test_linked_token_or_linked_upload_is_never_swept(backup_world, tmp_path, clock):
    service = backup_world.service
    external = tmp_path / 'external private source'
    external.mkdir()
    (external / 'keep.txt').write_text('Keep outside staging.', 'utf-8')
    linked_folder = service.staging / uuid.uuid4().hex
    try:
        linked_folder.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip('Symbolic links are unavailable for this Windows account')
    folder = preview_folder(service)
    upload = folder / 'upload.zip'
    upload.unlink()
    upload.symlink_to(external / 'keep.txt')
    assert service.cleanup_staging() == 0
    assert linked_folder.is_symlink() and upload.is_symlink()
    assert (external / 'keep.txt').read_text() == 'Keep outside staging.'
