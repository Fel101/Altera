"""Versioned, portable snapshots and validated, recoverable restores.

Only application data is imported. Python, SQL schema, credentials and remote
access policy are never imported from an archive.
"""
from contextlib import contextmanager
from datetime import datetime, UTC
import hashlib
import hmac
import itertools
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
import zipfile
from urllib.parse import urlsplit

import yaml

from core.datastore import init_db
from core.constants import MAX_MESSAGE_LENGTH
from core.filestat import ctime_ns
from core.memory import MemoryStore
from core.state_gate import STATE, StateBusy, request_lease

FORMAT = "altera-backup"
# Altera was called Her. Backups from then are still listed, previewed and restored: their files are named
# her-<time>-<id>.zip or .herbak, their manifests say her-backup and their browser settings are her_<name>.
LEGACY_FORMAT = "her-backup"
ENCRYPTED_SUFFIXES = (".alterabak", ".herbak")
LEGACY_BROWSER_PREFIX = "her_"
STAGING_LOCK = b'ALTERA-STAGING-LOCK-1\n'
STAGING_LOCKS = {STAGING_LOCK, b'HER-STAGING-LOCK-1\n'}
STAGING_FORMATS = {'altera-preview-stage', 'her-preview-stage'}
VERSION = 1
MAX_ARCHIVE_BYTES = 2 * 1024 ** 3
MAX_EXPANDED_BYTES = 8 * 1024 ** 3
MAX_JSON_BYTES = 256 * 1024 ** 2
MAX_BROWSER_STATE_BYTES = 8 * 1024 ** 2
MAX_FILES = 100000
PREVIEW_TTL = 15 * 60
STAGING_CLEANUP_LIMIT = 100
STAGING_OWNER = '.preview-owner.json'
STAGING_FILES = {'upload.zip', 'preview.json', STAGING_OWNER}
_STAGING_LOCKS = {}
_STAGING_LOCKS_GUARD = threading.Lock()
_BACKUP_SERVICE_LOCK = threading.RLock()
BACKUP_ID = re.compile(r"^(?:altera|her)-(\d{8}T\d{6}Z)-[0-9a-f]{12}\.(?:zip|alterabak|herbak)$")
TOKEN = re.compile(r"^[0-9a-f]{32}$")
SECRET_FIELDS = {"api_key", "apikey", "token", "secret", "secret_key", "password", "access_token", "refresh_token", "client_secret", "authorization"}
CONNECTION_FIELDS = ("llm", "voice", "imagegen", "forge", "web_search")
SKIP_DATA = {"chroma_db", "secret_key", "voice_service.log", "altera.log", "her.log", "polish-qa", "__pycache__", "cache", "logs", "tmp"}
PREFERENCE_TARGETS = {"config.yaml", "config.local.yaml", "personality.yaml", "user_profile.yaml", "content/adult_tiers.yaml"}
BROWSER_KEYS = {"altera_theme", "altera_accent", "altera_dismissed_hints", "altera_room"}


class BackupError(ValueError):
    pass


class BackupCleanupError(BackupError):
    """A backup lock or descriptor could not be confirmed released."""




@contextmanager
def _owned_backup_stage(directory, gate):
    stage = tempfile.TemporaryDirectory(dir=directory)
    try:
        yield Path(stage.name)
    finally:
        try:
            stage.cleanup()
        except BaseException:
            raise


@contextmanager
def _owned_snapshot_stream(stream, gate):
    try:
        yield stream
    finally:
        try:
            stream.close()
        except BaseException:
            raise


@contextmanager
def _snapshot_input(path, gate):
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    try:
        stream = os.fdopen(descriptor, 'rb')
    except BaseException:
        try:
            os.close(descriptor)
        except BaseException:
            raise
        raise
    with _owned_snapshot_stream(stream, gate):
        yield stream


def _snapshot_fingerprint(info):
    fields = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns')
    return tuple(getattr(info, name) for name in fields) + (ctime_ns(info), getattr(info, 'st_file_attributes', 0))


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _private_stat(info, *, private=True, posix=None):
    posix = os.name != 'nt' if posix is None else posix
    if posix:
        mode = stat.S_IMODE(info.st_mode)
        if (private and mode != 0o700) or (not private and mode & 0o022):
            raise BackupError('Backup staging directories must be private to their owner. Correct their permissions manually before continuing.')
        if hasattr(os, 'geteuid') and info.st_uid != os.geteuid():
            raise BackupError('Backup staging belongs to another owner. Choose a private instance directory.')


def _private_directory(path, *, create=False, private=True):
    try:
        if create:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
        info = path.lstat()
    except OSError:
        raise BackupError('A backup staging directory is unavailable. Check its location and permissions.') from None
    if _linked(info) or not stat.S_ISDIR(info.st_mode):
        raise BackupError('Backup staging cannot use linked or non-directory paths.')
    _private_stat(info, private=private)
    return path


def _regular_bytes(path, limit):
    info = path.lstat()
    if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise BackupError('A backup staging file is linked, oversized or unreadable.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        actual = os.fstat(stream.fileno())
        if not stat.S_ISREG(actual.st_mode) or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino):
            raise BackupError('A backup staging file changed while being read.')
        value = stream.read(limit + 1)
        if len(value) > limit:
            raise BackupError('A backup staging file exceeds its supported size.')
        return value


def _json_file(path, limit=16384):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate metadata field.')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('Non-finite metadata field.')
    try:
        value = json.loads(_regular_bytes(path, limit), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise BackupError('Backup staging metadata could not be read safely.') from None
    if not isinstance(value, dict):
        raise BackupError('Backup staging metadata must be an object.')
    return value


def _finite_time(value):
    return type(value) in {int, float} and math.isfinite(value) and 0 < value < 2 ** 53


def _preview_metadata(value, token):
    required = {'token', 'expires_at', 'sha256', 'restore_connections', 'counts', 'created_at', 'version'}
    return (isinstance(value, dict) and required <= set(value) <= required | {'encrypted'}
            and value['token'] == token and _finite_time(value['expires_at'])
            and isinstance(value['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', value['sha256'])
            and type(value['restore_connections']) is bool and isinstance(value['counts'], dict)
            and isinstance(value['created_at'], str) and len(value['created_at']) <= 128
            and type(value['version']) is int and value['version'] == VERSION
            and ('encrypted' not in value or type(value['encrypted']) is bool))


def _sha(path, gate=None):
    digest = hashlib.sha256()
    with _owned_snapshot_stream(Path(path).open("rb"), gate or STATE) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_snapshot_text(path, text, gate):
    with _owned_snapshot_stream(path.open('x', encoding='utf-8'), gate) as output:
        if output.write(text) != len(text):
            raise OSError('The backup snapshot write did not complete.')


def _read_snapshot_text(path, gate):
    with _owned_snapshot_stream(path.open('r', encoding='utf-8'), gate) as stream:
        return stream.read()


def _backup_query(conn, sql, gate, parameters=()):
    with _owned_snapshot_stream(conn.execute(sql, parameters), gate) as cursor:
        return cursor.fetchall()


def _backup_write(conn, sql, gate, parameters=()):
    with _owned_snapshot_stream(conn.execute(sql, parameters), gate):
        pass


class _CheckedRestoreWrites:
    """Adapter for restore invalidators, whose SQL results are not used."""
    def __init__(self, connection, gate):
        self.connection, self.gate = connection, gate

    def execute(self, sql, parameters=()):
        _backup_write(self.connection, sql, self.gate, parameters)


def _read_yaml(path, default=None, *, gate=None):
    if not path.exists():
        return {} if default is None else default
    with _owned_snapshot_stream(path.open('r', encoding='utf-8'), gate or STATE) as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise BackupError("A preference file must contain a YAML mapping.")
    return data


def _merge(base, update):
    result = dict(base)
    for key, value in update.items():
        result[key] = _merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result


def _split_settings(data):
    public, private = {}, {}
    for key, value in data.items():
        if str(key).casefold() in SECRET_FIELDS or key == "remote_access":
            private[key] = value
        elif isinstance(value, dict):
            left, right = _split_settings(value)
            if left or not value:
                public[key] = left
            if right:
                private[key] = right
        else:
            public[key] = value
    return public, private


def _safe_name(name):
    if not isinstance(name, str) or "\\" in name or ":" in name or "\x00" in name or name.startswith("/"):
        raise BackupError("The backup contains an unsafe path.")
    parts = name.split("/")
    reserved = {"con", "prn", "aux", "nul", "clock$"} | {f"{prefix}{n}" for prefix in ("com", "lpt") for n in range(10)}
    if any(not p or p in {".", ".."} or p.endswith((".", " ")) or p.split(".")[0].casefold() in reserved for p in parts):
        raise BackupError("The backup contains an unsafe path.")
    return PurePosixPath(name)


def _allowed_member(name):
    if name in {"database.sqlite3", "memories.json", "preferences/config.yaml", "preferences/personality.yaml", "preferences/user_profile.yaml", "preferences/adult_tiers.yaml", "preferences/browser.json"}:
        return True
    return name.startswith(("files/data/", "files/workspace/"))




def validate_browser_state(state):
    if not isinstance(state, dict) or not isinstance(state.get("preferences", {}), dict) or not isinstance(state.get("drafts", []), list):
        raise BackupError("Invalid browser preference/draft data.")
    prefs = state.get("preferences", {})
    renamed = {"altera_" + key[len(LEGACY_BROWSER_PREFIX):] if key.startswith(LEGACY_BROWSER_PREFIX) else key: value
               for key, value in prefs.items()}
    prefs = renamed if len(renamed) == len(prefs) else prefs  # Both names at once is refused below.
    if not set(prefs) <= BROWSER_KEYS or any(not isinstance(v, str) or len(v) > 4096 for v in prefs.values()):
        raise BackupError("Unsupported browser preferences.")
    if "altera_theme" in prefs and prefs["altera_theme"] not in {"light", "dark"}:
        raise BackupError("Invalid appearance preference.")
    if "altera_accent" in prefs and not re.fullmatch(r"#[0-9a-fA-F]{6}", prefs["altera_accent"]):
        raise BackupError("Invalid accent colour.")
    if "altera_room" in prefs and prefs["altera_room"] not in {"off", "day", "night", "auto"}:
        raise BackupError("Invalid room preference.")
    if "altera_dismissed_hints" in prefs:
        try:
            hints = json.loads(prefs["altera_dismissed_hints"])
            if not isinstance(hints, list) or len(hints) > 128 or any(not isinstance(h, str) or len(h) > 100 for h in hints):
                raise ValueError()
        except ValueError as e:
            raise BackupError("Invalid saved feature hints.") from e
    drafts = state.get("drafts", [])
    if len(drafts) > 1000:
        raise BackupError("Too many browser drafts.")
    for draft in drafts:
        if not isinstance(draft, dict) or not isinstance(draft.get("message"), str) or len(draft["message"]) > MAX_MESSAGE_LENGTH:
            raise BackupError("Invalid browser draft.")
        sid = draft.get("session_id")
        if sid is not None and (isinstance(sid, bool) or not isinstance(sid, int) or sid <= 0):
            raise BackupError("Invalid draft conversation.")
    return {"preferences": prefs, "drafts": [{"message": d["message"], "session_id": d.get("session_id")} for d in drafts]}


def serialize_browser_state(state):
    """Use the same byte bound on creation and restore preview."""
    validated = validate_browser_state(state)
    try:
        text = json.dumps(validated, ensure_ascii=False)
        size = len(text.encode('utf-8'))
    except (UnicodeError, TypeError, ValueError):
        raise BackupError('Browser preference/draft data could not be encoded safely.') from None
    if size > MAX_BROWSER_STATE_BYTES:
        raise BackupError('The browser draft snapshot is too large.')
    return validated, text


class FileBatch:
    """Move old files aside before publishing replacements; reverse those moves on failure."""
    def __init__(self, rollback):
        self.rollback = rollback
        self.moves = []

    def replace(self, target, source):
        target.parent.mkdir(parents=True, exist_ok=True)
        old = self.rollback / str(len(self.moves))
        existed = target.exists()
        if existed:
            os.replace(target, old)
        self.moves.append((target, old if existed else None))
        if source is not None and source.exists():
            os.replace(source, target)

    def undo(self):
        for target, old in reversed(self.moves):
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            if old is not None:
                os.replace(old, target)


class BackupService:
    def __init__(self, root, store, memories, gate=STATE, on_restored=None, *, sweep_on_init=True):
        self.root = Path(root).resolve()
        self.store, self.memories, self.gate = store, memories, gate
        self.directory = self.root / "backups"
        self.staging = self.directory / ".staging"
        self.work = self.directory / ".work"
        self.results = self.directory / ".results"
        self.root.mkdir(parents=True, exist_ok=True)
        _private_directory(self.directory, create=True, private=False)
        for path in (self.staging, self.work, self.results):
            _private_directory(path, create=True)
        self.on_restored = on_restored or (lambda: None)
        self.crypto_lock = threading.Lock()
        with _STAGING_LOCKS_GUARD:
            self.staging_lock = _STAGING_LOCKS.setdefault(str(self.directory), threading.Lock())
        self.journal = self.directory / "restore-in-progress.json"
        self._cleanup_scan = None
        if self.journal.exists():
            self.gate.fail_closed("An interrupted restore needs recovery. Open Backup recovery before resuming Altera.")
        if sweep_on_init:
            self.cleanup_staging()

    def __del__(self):
        try:
            self._close_cleanup_scan()
        except Exception:
            pass

    def _close_cleanup_scan(self):
        listing = self._cleanup_scan
        if listing is not None:
            try:
                listing.close()
            except BaseException:
                raise
            self._cleanup_scan = None

    def close(self):
        """Close the owned idle scan after the host has drained all work."""
        self._close_cleanup_scan()

    def _cleanup_candidates(self):
        """Continue a bounded directory scan; restarting the service resets it.

        The operation lock protects this cursor. Names are only candidates:
        ownership, paths and metadata must still be checked immediately before
        removal. Newly created entries may wait until the next scan cycle.
        """
        fresh = self._cleanup_scan is None
        candidates = []
        try:
            if fresh:
                self._cleanup_scan = os.scandir(self.staging)
            while len(candidates) < STAGING_CLEANUP_LIMIT:
                try:
                    candidates.append(next(self._cleanup_scan).name)
                except StopIteration:
                    self._close_cleanup_scan()
                    if candidates or fresh:
                        break
                    # An old scan can have reached its end between calls.
                    # Reopen once so an otherwise empty pass sees new entries.
                    self._cleanup_scan = os.scandir(self.staging)
                    fresh = True
            return candidates
        except BaseException:
            self._close_cleanup_scan()
            raise

    def _check_staging(self):
        _private_directory(self.directory, private=False)
        for path in (self.staging, self.work, self.results):
            _private_directory(path)

    @contextmanager
    def _staging_operation(self, *, cleanup=False):
        """One cross-thread/process archive operation; cleanup never races it."""
        with self.gate.lock:
            draining = self.gate.draining
        if draining:
            if cleanup:
                yield False
                return
            raise StateBusy('Altera is shutting down. Restart before using backup controls.', 409)
        if not self.staging_lock.acquire(blocking=False):
            if cleanup:
                yield False
                return
            raise StateBusy('Another backup preview or restore is active. Try again shortly.', 409)
        descriptor = None
        locked = False
        try:
            # The gate may have drained after the first check while another
            # thread still held the local mutex.
            with self.gate.lock:
                draining = self.gate.draining
            if draining:
                if cleanup:
                    yield False
                    return
                raise StateBusy('Altera is shutting down. Restart before using backup controls.', 409)
            self._check_staging()
            if os.path.lexists(self.journal) and not self.gate.fault:
                self.gate.fail_closed('An interrupted restore needs recovery before saved data can be changed.')
            lock_path = self.directory / '.staging.lock'
            flags = os.O_RDWR | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            try:
                descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(descriptor, STAGING_LOCK)
            except FileExistsError:
                info = lock_path.lstat()
                if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_size not in {len(lock) for lock in STAGING_LOCKS}:
                    raise BackupError('The backup staging lock is unsafe. Inspect it before continuing.') from None
                if os.name != 'nt' and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid()):
                    raise BackupError('The backup staging lock is not private to its owner.') from None
                descriptor = os.open(lock_path, flags)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise BackupError('The backup staging lock is not a regular file.')
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if cleanup:
                    yield False
                    return
                raise StateBusy('Another backup preview or restore is active. Try again shortly.', 409) from None
            if os.read(descriptor, 64) not in STAGING_LOCKS:
                raise BackupError('The backup staging lock has unknown contents; it was left intact.')
            yield True
        finally:
            try:
                failed = False
                if descriptor is not None:
                    try:
                        if locked:
                            os.lseek(descriptor, 0, os.SEEK_SET)
                            if os.name == 'nt':
                                import msvcrt
                                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                            else:
                                import fcntl
                                fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except BaseException:
                        failed = True
                    try:
                        os.close(descriptor)
                    except BaseException:
                        failed = True
                if failed:
                    # Fence before releasing the process-local mutex: neither
                    # a failed unlock nor a failed close proves handle release.
                    self.gate.begin_shutdown()
                    raise BackupCleanupError('Backup cleanup could not be confirmed. Restart Altera and review saved backups.') from None
            finally:
                self.staging_lock.release()

    def _preview_folder(self, folder):
        if not TOKEN.fullmatch(folder.name) or folder.parent != self.staging or folder.resolve().parent != self.staging.resolve():
            raise BackupError('Unknown restore preview directory.')
        _private_directory(folder)
        entries = list(itertools.islice(folder.iterdir(), len(STAGING_FILES) + 1))
        if len(entries) > len(STAGING_FILES) or any(path.name not in STAGING_FILES for path in entries):
            raise BackupError('A preview directory contains unknown files; it was left intact.')
        for path in entries:
            info = path.lstat()
            if _linked(info) or not stat.S_ISREG(info.st_mode):
                raise BackupError('A preview directory contains linked or non-file content.')
        return entries

    def _remove_preview(self, folder):
        # Only these already verified regular files are removed. No recursive
        # traversal can consume an unknown user directory or follow a link.
        entries = self._preview_folder(folder)
        entries.sort(key=lambda path: path.name == STAGING_OWNER)
        for path in entries:
            path.unlink()
        folder.rmdir()

    def _protected_cleanup_token(self):
        if not os.path.lexists(self.journal):
            return ''
        try:
            record = _json_file(self.journal)
            if (not isinstance(record.get('stage'), str) or not re.fullmatch(r'restore-[0-9a-f]{32}', record['stage'])
                    or not isinstance(record.get('token'), str) or not TOKEN.fullmatch(record['token'])
                    or not isinstance(record.get('recovery_backup'), str) or not BACKUP_ID.fullmatch(record['recovery_backup'])):
                return None
            return record['token']
        except (OSError, ValueError, TypeError):
            return None  # An uncertain recovery journal prevents all sweeping.

    def cleanup_staging(self, *, now=None, keep_token=None):
        """Bounded expiry of owned preview uploads; never touches .work recovery."""
        moment = time.time() if now is None else now
        if not _finite_time(moment):
            return 0
        removed = 0
        try:
            with self._staging_operation(cleanup=True) as acquired:
                if not acquired:
                    return 0
                protected = self._protected_cleanup_token()
                if protected is None:
                    self._close_cleanup_scan()
                    return 0
                for name in self._cleanup_candidates():
                    if not TOKEN.fullmatch(name) or name in {protected, keep_token}:
                        continue
                    folder = self.staging / name
                    try:
                        self._preview_folder(folder)
                        owner_path, receipt_path = folder / STAGING_OWNER, folder / 'preview.json'
                        if owner_path.exists():
                            owner = _json_file(owner_path)
                            if (set(owner) != {'format', 'version', 'token', 'created_at'} or owner['format'] not in STAGING_FORMATS
                                    or type(owner['version']) is not int or owner['version'] != 1 or owner['token'] != name
                                    or not _finite_time(owner['created_at']) or owner['created_at'] > moment):
                                continue
                            created = owner['created_at']
                        elif receipt_path.exists():
                            created = None  # Compatibility with verified pre-marker preview receipts.
                        else:
                            continue
                        if receipt_path.exists():
                            receipt = _json_file(receipt_path)
                            expires = receipt.get('expires_at')
                            if (not _preview_metadata(receipt, name)
                                    or expires <= (created if created is not None else PREVIEW_TTL)):
                                continue
                        else:
                            expires = created + PREVIEW_TTL
                        if expires > moment:
                            continue
                        self._remove_preview(folder)
                        removed += 1
                    except (OSError, ValueError, TypeError):
                        continue
        except BackupCleanupError:
            try:
                self._close_cleanup_scan()
            except OSError:
                pass
            raise
        except (OSError, BackupError):
            self._close_cleanup_scan()
            return removed  # Unsafe/inaccessible paths are never repaired or removed automatically.
        except BaseException:
            self._close_cleanup_scan()
            raise
        return removed

    def _settings(self):
        return _merge(_read_yaml(self.root / "config.yaml", gate=self.gate),
                      _read_yaml(self.root / "config.local.yaml", gate=self.gate))

    def _include_data(self, path):
        # ZIP member names keep their original spelling, but Windows resolves
        # SECRET_KEY to the same file as secret_key. Apply the reserved-name
        # policy independently of the host filesystem's case sensitivity.
        name = path.name.casefold()
        database = Path(getattr(self.store, 'db_path', self.root / 'data/conversations.db')).resolve()
        raw_memory = Path(getattr(self.memories, "persist_directory", self.root / "data/chroma_db")).resolve()
        return (name not in SKIP_DATA
                and not name.startswith("polish-baseline-") and ".before-" not in name
                and not name.endswith((".log", ".tmp")) and path.resolve() != raw_memory
                and path.resolve() not in {database, Path(str(database) + "-wal"), Path(str(database) + "-shm")})

    def _data_roots(self):
        data = self.root / "data"
        return [p for p in data.iterdir() if self._include_data(p)] if data.exists() else []

    def _copy_tree(self, source, target):
        before = source.lstat()
        if _linked(before):
            raise BackupError("Backups cannot follow symbolic links or junctions in application data.")
        if stat.S_ISDIR(before.st_mode):
            target.mkdir(parents=True, exist_ok=True)
            for child in source.iterdir():
                self._copy_tree(child, target / child.name)
            if _snapshot_fingerprint(source.lstat()) != _snapshot_fingerprint(before):
                raise BackupError("A directory changed outside Altera while the snapshot was being copied. Please retry.")
        else:
            if not stat.S_ISREG(before.st_mode):
                raise BackupError("Backup source files must be regular, unlinked files.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with _snapshot_input(source, self.gate) as incoming:
                if _snapshot_fingerprint(os.fstat(incoming.fileno())) != _snapshot_fingerprint(before):
                    raise BackupError("A file changed outside Altera while the snapshot was being copied. Please retry.")
                with _owned_snapshot_stream(target.open('xb'), self.gate) as output:
                    shutil.copyfileobj(incoming, output, 1024 * 1024)
                if (_snapshot_fingerprint(os.fstat(incoming.fileno())) != _snapshot_fingerprint(before)
                        or _snapshot_fingerprint(source.lstat()) != _snapshot_fingerprint(before)):
                    raise BackupError("A file changed outside Altera while the snapshot was being copied. Please retry.")
                shutil.copystat(source, target, follow_symlinks=False)
                if _snapshot_fingerprint(source.lstat()) != _snapshot_fingerprint(before):
                    raise BackupError("A file changed outside Altera while the snapshot was being copied. Please retry.")

    def _sqlite_copy(self, source, destination):
        with (_owned_snapshot_stream(sqlite3.connect(str(source)), self.gate) as src,
              _owned_snapshot_stream(sqlite3.connect(str(destination)), self.gate) as dst):
            src.backup(dst)

    def _write_snapshot_archive(self, stage, temporary):
        with _owned_snapshot_stream(zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, allowZip64=True), self.gate) as archive:
            for path in stage.rglob('*'):
                if not path.is_file():
                    continue
                before = path.lstat()
                if _linked(before) or not stat.S_ISREG(before.st_mode):
                    raise BackupError('A backup snapshot file is unsafe.')
                name = path.relative_to(stage).as_posix()
                _safe_name(name)
                info = zipfile.ZipInfo.from_file(path, name)
                info.compress_type = archive.compression
                info._compresslevel = archive.compresslevel
                with _snapshot_input(path, self.gate) as incoming:
                    if _snapshot_fingerprint(os.fstat(incoming.fileno())) != _snapshot_fingerprint(before):
                        raise BackupError('A backup snapshot file changed while it was being archived.')
                    with _owned_snapshot_stream(archive.open(info, 'w'), self.gate) as output:
                        shutil.copyfileobj(incoming, output, 1024 * 1024)
                    if (_snapshot_fingerprint(os.fstat(incoming.fileno())) != _snapshot_fingerprint(before)
                            or _snapshot_fingerprint(path.lstat()) != _snapshot_fingerprint(before)):
                        raise BackupError('A backup snapshot file changed while it was being archived.')

    @staticmethod
    def _validate_password(password):
        from core.backup_crypto import BackupCryptoError, validate_password
        try:
            validate_password(password)
        except BackupCryptoError as error:
            raise BackupError(str(error)) from None

    @contextmanager
    def _crypto_slot(self):
        # Bound KDF memory and long archive work to one request per instance.
        if not self.crypto_lock.acquire(blocking=False):
            raise StateBusy('Another encrypted backup is being processed. Try again shortly.', 409)
        try:
            yield
        finally:
            self.crypto_lock.release()

    def _encrypted(self, path):
        from core.backup_crypto import LEGACY_MAGIC, MAGIC
        with _owned_snapshot_stream(Path(path).open('rb'), self.gate) as stream:
            start = stream.read(len(MAGIC))
            return start == MAGIC or start.startswith(LEGACY_MAGIC)

    @contextmanager
    def _plain_archive(self, source, destination, *, password=None, recovery_key=None):
        from core.backup_crypto import BackupCryptoError, decrypt_file
        if not self._encrypted(source):
            yield source
            return
        published = False
        try:
            with self._crypto_slot():
                decrypt_file(source, destination, password=password, recovery_key=recovery_key)
            published = True
            yield destination
        except BackupCryptoError as error:
            raise BackupError(str(error)) from None
        finally:
            if published:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    raise

    def create(self, browser_state=None, *, password=None, recovery_key=None):
        if password is not None:
            self._validate_password(password)
        elif recovery_key is not None:
            raise BackupError('A password is required when adding an archive recovery key.')
        if self.gate.fault:
            raise BackupError("Recover the interrupted restore before making a new backup.")
        self.cleanup_staging()
        with self._staging_operation(), self.gate.exclusive(allow_lease=request_lease(self.gate)):
            # Staging admission can discover a restore journal after the
            # initial fault check. Recovery maintenance may enter exclusive
            # mode while faulted, but a new backup must never do so.
            if self.gate.fault:
                raise BackupError("Recover the interrupted restore before making a new backup.")
            if browser_state is not None:
                serialize_browser_state(browser_state)
            if browser_state is not None and browser_state.get("dataset_epoch") != self.store.dataset_epoch()["value"]:
                raise BackupError("Reload Altera before backing up browser drafts from an older data session.")
            return self._create(browser_state, password=password, recovery_key=recovery_key)

    def _create(self, browser_state=None, *, password=None, recovery_key=None):
        self.store._ensure_schema()
        self.store.dataset_epoch()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        encrypted = password is not None
        name = f"altera-{stamp}-{uuid.uuid4().hex[:12]}.{'alterabak' if encrypted else 'zip'}"
        target = self.directory / name
        temporary = self.work / ('snapshot-' + uuid.uuid4().hex + '.zip')
        try:
            with _owned_backup_stage(self.work, self.gate) as stage:
                self._sqlite_copy(self.store.db_path, stage / "database.sqlite3")
                snapshot = self.memories.snapshot()
                MemoryStore.validate_snapshot(snapshot)
                _write_snapshot_text(stage / "memories.json", json.dumps(snapshot, ensure_ascii=False, allow_nan=False), self.gate)
                prefs = stage / "preferences"
                prefs.mkdir()
                if browser_state is not None:
                    browser_state, browser_json = serialize_browser_state(browser_state)
                    _write_snapshot_text(prefs / "browser.json", browser_json, self.gate)
                settings, _ = _split_settings(self._settings())
                _write_snapshot_text(prefs / "config.yaml", yaml.safe_dump(settings, allow_unicode=True), self.gate)
                for source_name, archive_name in (("personality.yaml", "personality.yaml"), ("user_profile.yaml", "user_profile.yaml"),
                                                   ("content/adult_tiers.yaml", "adult_tiers.yaml")):
                    source = self.root / source_name
                    if source.exists():
                        self._copy_tree(source, prefs / archive_name)
                for source in self._data_roots():
                    self._copy_tree(source, stage / "files/data" / source.name)
                if (self.root / "workspace").exists():
                    self._copy_tree(self.root / "workspace", stage / "files/workspace")
                entries = {}
                for path in stage.rglob("*"):
                    if path.is_file():
                        rel = path.relative_to(stage).as_posix()
                        _safe_name(rel)
                        size = path.stat().st_size
                        entries[rel] = {"bytes": size, "sha256": _sha(path, self.gate)}
                if sum(e["bytes"] for e in entries.values()) > MAX_EXPANDED_BYTES or len(entries) > MAX_FILES:
                    raise BackupError("This backup exceeds the supported archive size or file count.")
                counts = self._counts(stage / "database.sqlite3", len(snapshot["records"]), entries)
                counts["unsent_drafts"] = len((browser_state or {}).get("drafts", []))
                manifest = {"format": FORMAT, "version": VERSION, "created_at": datetime.now(UTC).isoformat(),
                            "counts": counts, "files": entries, "credentials_included": False}
                _write_snapshot_text(stage / "manifest.json", json.dumps(manifest, indent=2), self.gate)
                self._write_snapshot_archive(stage, temporary)
            if temporary.stat().st_size > MAX_ARCHIVE_BYTES:
                raise BackupError("This backup exceeds the supported compressed size.")
            if encrypted:
                from core.backup_crypto import BackupCryptoError, encrypt_file
                try:
                    with self._crypto_slot():
                        encrypt_file(temporary, target, password, recovery_key=recovery_key)
                except BackupCryptoError as error:
                    raise BackupError(str(error)) from None
            else:
                os.replace(temporary, target)
            return {"id": name, "created_at": manifest["created_at"], "bytes": target.stat().st_size,
                    "counts": counts, 'encrypted': encrypted, 'recovery_enabled': recovery_key is not None,
                    "download_url": f"/api/backups/{name}/download"}
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                raise

    def _counts(self, database, memories, entries):
        with _owned_snapshot_stream(sqlite3.connect(str(database)), self.gate) as conn:
            with _owned_snapshot_stream(conn.execute("SELECT count(*) FROM sessions"), self.gate) as cursor:
                sessions = cursor.fetchone()[0]
            with _owned_snapshot_stream(conn.execute("SELECT count(*) FROM messages"), self.gate) as cursor:
                messages = cursor.fetchone()[0]
        return {"sessions": sessions, "messages": messages, "memories": memories,
                "companions": sum(p.endswith("/avatar.json") and p.startswith("files/data/avatars/") for p in entries),
                "pictures": sum(p.startswith("files/data/photos/") and p.endswith(".png") for p in entries),
                "note_files": sum("/notes/" in p and p.endswith(".md") for p in entries), "files": len(entries)}

    def archive_path(self, name):
        if not isinstance(name, str) or not BACKUP_ID.fullmatch(name):
            raise BackupError("Unknown backup.")
        path = self.directory / name
        if not path.is_file() or path.resolve().parent != self.directory.resolve() or path.is_symlink():
            raise BackupError("Unknown backup.")
        return path

    def list(self):
        results = []
        found = [*self.directory.glob("altera-*"), *self.directory.glob("her-*")]
        for path in sorted(found, key=lambda item: item.name.split('-', 1)[-1], reverse=True):
            try:
                self.archive_path(path.name)
                if path.suffix in ENCRYPTED_SUFFIXES:
                    from core.backup_crypto import MAX_CIPHERTEXT_BYTES
                    if path.stat().st_size > MAX_CIPHERTEXT_BYTES or not self._encrypted(path):
                        continue
                    created = datetime.strptime(BACKUP_ID.fullmatch(path.name).group(1), '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC).isoformat()
                    results.append({'id': path.name, 'bytes': path.stat().st_size, 'created_at': created,
                                    'counts': {}, 'encrypted': True, 'download_url': f'/api/backups/{path.name}/download'})
                    continue
                with zipfile.ZipFile(path) as archive:
                    info = archive.getinfo("manifest.json")
                    if info.file_size > 32 * 1024 ** 2:
                        continue
                    manifest = json.loads(archive.read(info))
                results.append({"id": path.name, "bytes": path.stat().st_size, "created_at": manifest.get("created_at"),
                                "counts": manifest.get("counts", {}), 'encrypted': False,
                                "download_url": f"/api/backups/{path.name}/download"})
            except BackupCleanupError:
                raise
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                continue
        return results

    def _unpack(self, archive_path, destination):
        try:
            with _owned_snapshot_stream(zipfile.ZipFile(archive_path), self.gate) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_FILES + 1 or sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES:
                    raise BackupError("The archive is too large.")
                names = set()
                for info in infos:
                    _safe_name(info.filename)
                    if info.filename.casefold() in names or info.is_dir() or info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16):
                        raise BackupError("Duplicate, encrypted, linked or unsupported archive entries.")
                    names.add(info.filename.casefold())
                info = archive.getinfo("manifest.json")
                if info.file_size > 32 * 1024 ** 2:
                    raise BackupError("The backup manifest is too large.")
                with _owned_snapshot_stream(archive.open(info), self.gate) as manifest_stream:
                    manifest = json.load(manifest_stream)
                if not isinstance(manifest, dict) or manifest.get("format") not in (FORMAT, LEGACY_FORMAT) or type(manifest.get("version")) is not int or manifest.get("version") != VERSION:
                    raise BackupError("This backup format/version is not supported.")
                if not isinstance(manifest.get('created_at'), str) or len(manifest['created_at']) > 128:
                    raise BackupError('The backup creation time is invalid.')
                files = manifest.get("files")
                if not isinstance(files, dict) or set(files) | {"manifest.json"} != {i.filename for i in infos}:
                    raise BackupError("The archive does not match its manifest.")
                if not {"database.sqlite3", "memories.json", "preferences/config.yaml"} <= set(files):
                    raise BackupError("The backup is missing its database, memories or preferences.")
                needed = sum(i.file_size for i in infos)
                if shutil.disk_usage(self.work).free < needed * 2 + 64 * 1024 ** 2:
                    raise BackupError("There is not enough free disk space to prepare this restore.")
                for name, expected in files.items():
                    if not _allowed_member(name) or not isinstance(expected, dict):
                        raise BackupError("The backup contains an unsupported file.")
                    if name.startswith("files/data/") and not self._include_data(self.root / "data" / PurePosixPath(name).parts[2]):
                        raise BackupError("The backup attempts to replace protected runtime data.")
                    info = archive.getinfo(name)
                    if name == "preferences/browser.json" and info.file_size > MAX_BROWSER_STATE_BYTES:
                        raise BackupError("The browser draft snapshot is too large.")
                    if info.file_size != expected.get("bytes"):
                        raise BackupError("A backup file has an unexpected size.")
                    target = destination.joinpath(*_safe_name(name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with (_owned_snapshot_stream(archive.open(info), self.gate) as src,
                          _owned_snapshot_stream(target.open("xb"), self.gate) as dst):
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    if _sha(target, self.gate) != expected.get("sha256"):
                        raise BackupError("A backup file failed its integrity check.")
                if (destination / "memories.json").stat().st_size > MAX_JSON_BYTES:
                    raise BackupError("The memory snapshot is too large.")
                memories = json.loads(_read_snapshot_text(destination / "memories.json", self.gate))
                MemoryStore.validate_snapshot(memories)
                if (destination / "preferences/browser.json").exists():
                    validate_browser_state(json.loads(_read_snapshot_text(destination / "preferences/browser.json", self.gate)))
                for path in (destination / "preferences").glob("*.yaml"):
                    if path.stat().st_size > 8 * 1024 ** 2:
                        raise BackupError("A preference file is too large.")
                    _read_yaml(path, gate=self.gate)
                from core import safety
                for card in (destination / "files/data/avatars").glob("*/avatar.json"):
                    record = json.loads(_read_snapshot_text(card, self.gate))
                    if not isinstance(record, dict) or not isinstance(record.get("card"), dict) or safety.card_problem(record["card"]):
                        raise BackupError("A companion card does not meet Altera's avatar rules.")
                # Never import credentials even if someone recomputes the archive's own hashes.
                _, secrets = _split_settings(_read_yaml(destination / "preferences/config.yaml", gate=self.gate))
                if secrets:
                    raise BackupError("Service credentials and remote access policy cannot be imported.")
                settings = _read_yaml(destination / "preferences/config.yaml", gate=self.gate)
                for key in ("app", "llm", "voice", "imagegen", "forge", "memory", "companion", "tools", "phase4", "privacy", "web_search"):
                    if key in settings and not isinstance(settings[key], dict):
                        raise BackupError("A preference section has an invalid shape.")
                return manifest, memories
        except (OSError, KeyError, TypeError, RecursionError, OverflowError, json.JSONDecodeError, yaml.YAMLError, zipfile.BadZipFile, sqlite3.Error) as e:
            raise BackupError("The backup could not be read or is not valid.") from e

    def _normalize_database(self, source, target):
        """Import table rows into our schema, never executing SQL schema supplied by an upload."""
        init_db(str(target))
        with (_owned_snapshot_stream(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True), self.gate) as src,
              _owned_snapshot_stream(sqlite3.connect(target), self.gate) as dst):
            _backup_write(src, "PRAGMA trusted_schema=OFF", self.gate)
            src.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64 * 1024 ** 2)
            check = _backup_query(src, "PRAGMA quick_check(10)", self.gate)
            if not check or check[0][0] != "ok":
                raise BackupError("The conversation database failed its integrity check.")
            expected = {r[0] for r in _backup_query(dst, "SELECT name FROM sqlite_master WHERE type='table'", self.gate)
                        if not r[0].startswith(("sqlite_", "messages_fts"))}
            tables = {r[0] for r in _backup_query(src, "SELECT name FROM sqlite_master WHERE type='table'", self.gate)
                      if not r[0].startswith(("sqlite_", "messages_fts"))}
            if not {"sessions", "messages", "user_profile", "life_events"} <= tables or not tables <= expected:
                raise BackupError("The conversation database has an unsupported schema.")
            with dst:
                for table in sorted(tables):
                    columns = [r[1] for r in _backup_query(src, f'PRAGMA table_info("{table}")', self.gate)]
                    allowed = {r[1] for r in _backup_query(dst, f'PRAGMA table_info("{table}")', self.gate)}
                    if not set(columns) <= allowed:
                        raise BackupError("The conversation database has unsupported columns.")
                    quoted = ','.join(f'"{c}"' for c in columns)
                    with _owned_snapshot_stream(src.execute(f'SELECT {quoted} FROM "{table}"'), self.gate) as reader:
                        while True:
                            rows = reader.fetchmany(500)
                            if not rows:
                                break
                            with _owned_snapshot_stream(dst.executemany(f'INSERT OR REPLACE INTO "{table}" ({quoted}) VALUES ({",".join("?" for _ in columns)})', rows), self.gate):
                                pass
                if _backup_query(src, "SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'", self.gate):
                    with _owned_snapshot_stream(src.execute("SELECT name,seq FROM sqlite_sequence"), self.gate) as sequences:
                        for name, seq in sequences:
                            if name in expected:
                                _backup_write(dst, "UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?", self.gate, (seq, name))
                _backup_write(dst, "UPDATE chat_turns SET status='interrupted',error='Altera was restored before this reply finished.' WHERE status IN ('sending','streaming')", self.gate)
                _backup_write(dst, 'UPDATE chat_turns SET expression_pending=NULL,memory_pending=NULL', self.gate)
                _backup_write(dst, "UPDATE voice_notes SET status='stale',error_stage='context',error='Restored recording. Record a new voice note before sending.' WHERE error_stage!='deleted'", self.gate)
                _backup_write(dst, "UPDATE presence_preferences SET enabled=0,needs_review=1,revision=revision+1", self.gate)
                _backup_write(dst, "UPDATE presence_jobs SET state='stale' WHERE state IN ('pending','running')", self.gate)
                _backup_write(dst, "UPDATE presence_inbox SET invalidated=1,error='Restored presence requires review.'", self.gate)
                _backup_write(dst, 'DELETE FROM companion_expression', self.gate)
                from core.episodes import invalidate_all as invalidate_episodes
                from core.reflection import invalidate_all as invalidate_reflection
                writes = _CheckedRestoreWrites(dst, self.gate)
                invalidate_episodes(writes)
                invalidate_reflection(writes)
        # Search schema/triggers are application-owned and rebuilt from the imported rows.
        from core.datastore import DataStore
        from core.constants import FTS_TABLE
        DataStore(str(target)).ensure_fts_table(FTS_TABLE)
        return self._counts(target, 0, {})

    def preview(self, source, restore_connections=False, *, password=None, recovery_key=None, require_healthy=False):
        self.cleanup_staging()
        with self._staging_operation():
            with self.gate.lock:
                if require_healthy and (self.gate.fault or self.gate.draining):
                    raise BackupError('Recover the interrupted restore before previewing another backup.')
            return self._preview(source, restore_connections, password=password, recovery_key=recovery_key)

    def _preview(self, source, restore_connections=False, *, password=None, recovery_key=None):
        token = uuid.uuid4().hex
        folder = self.staging / token
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            raise BackupError('A preview directory already exists. Retry with a new preview.') from None
        try:
            (folder / STAGING_OWNER).write_text(json.dumps({'format': 'altera-preview-stage', 'version': 1,
                'token': token, 'created_at': time.time()}, allow_nan=False), 'utf-8')
            upload = folder / "upload.zip"
            if hasattr(source, "save"):
                source.save(upload)
            else:
                shutil.copyfile(source, upload)
            from core.backup_crypto import MAX_CIPHERTEXT_BYTES
            encrypted = self._encrypted(upload)
            if upload.stat().st_size > (MAX_CIPHERTEXT_BYTES if encrypted else MAX_ARCHIVE_BYTES):
                raise BackupError("The uploaded backup is too large.")
            with _owned_backup_stage(self.work, self.gate) as stage:
                with self._plain_archive(upload, stage / 'unlocked.zip', password=password, recovery_key=recovery_key) as plain:
                    manifest, memories = self._unpack(plain, stage)
                self._normalize_database(stage / "database.sqlite3", stage / "validated.sqlite3")
                counts = self._counts(stage / "validated.sqlite3", len(memories["records"]), manifest["files"])
                browser = stage / "preferences/browser.json"
                counts["unsent_drafts"] = len(json.loads(_read_snapshot_text(browser, self.gate)).get("drafts", [])) if browser.exists() else 0
                settings = _read_yaml(stage / "preferences/config.yaml", gate=self.gate)
            description = {"token": token, "expires_at": time.time() + PREVIEW_TTL, "sha256": _sha(upload, self.gate),
                           "restore_connections": bool(restore_connections), "counts": counts,
                           "created_at": manifest.get("created_at"), "version": VERSION, 'encrypted': encrypted}
            (folder / "preview.json").write_text(json.dumps(description), "utf-8")
            connections = None
            if restore_connections:
                from core.providers import ProviderRegistry
                registry = ProviderRegistry(settings)
                provider = registry.get_active_provider()
                voice = settings.get("voice") or {}
                connections = {"provider": provider.name if provider else registry.get_active_provider_key(),
                               "model": registry.get_active_model(), "model_host": urlsplit(provider.base_url).hostname if provider else None,
                               "voice_engine": voice.get("tts_engine", "edge"), "cloud_voice_fallback": bool(voice.get("cloud_fallback"))}
            return {**{k: v for k, v in description.items() if k != "sha256"},
                    "connections": connections,
                    "model": ((settings.get("llm") or {}).get("model") if restore_connections else None),
                    "notes": ["Current API keys, remote access and workspace-tool permissions are preserved.",
                              "A recovery backup is created before replacement.", "Adult mode is reset to off.",
                              "Old tabs and unsent messages must be reviewed after restore."]}
        except (OSError, sqlite3.Error, ValueError, TypeError) as error:
            try:
                self._remove_preview(folder)
            except (OSError, BackupError):
                pass
            if isinstance(error, BackupError):
                raise
            raise BackupError("The backup could not be validated. No application data was replaced.") from error
        except BaseException:
            try:
                self._remove_preview(folder)
            except (OSError, BackupError):
                pass
            raise

    def _prepared_preferences(self, stage, restore_connections, current=None):
        current = self._settings() if current is None else current
        restored = _read_yaml(stage / "preferences/config.yaml", gate=self.gate)
        if not restore_connections:
            for key in CONNECTION_FIELDS:
                if key in current:
                    restored[key] = current[key]
                else:
                    restored.pop(key, None)
        for key in ("app", "tools", "remote_access"):
            if key in current:
                restored[key] = current[key]
            else:
                restored.pop(key, None)
        _, credentials = _split_settings(current)
        restored = _merge(restored, credentials)
        public, private = _split_settings(restored)
        ready = stage / "ready-preferences"
        ready.mkdir(mode=0o700)
        (ready / "config.yaml").write_text(yaml.safe_dump(public, allow_unicode=True), "utf-8")
        (ready / "config.local.yaml").write_text(yaml.safe_dump(private, allow_unicode=True), "utf-8")
        mapping = {"personality.yaml": "personality.yaml", "user_profile.yaml": "user_profile.yaml", "content/adult_tiers.yaml": "adult_tiers.yaml"}
        for target, source in mapping.items():
            path = stage / "preferences" / source
            if path.exists():
                self._copy_tree(path, ready / target)
        return ready

    def _recovery_record(self):
        try:
            record = _json_file(self.journal)
            if (not isinstance(record.get('stage'), str) or not re.fullmatch(r"restore-[0-9a-f]{32}", record['stage'])
                    or not isinstance(record.get('token'), str) or not TOKEN.fullmatch(record['token'])):
                raise ValueError()
            if 'protected_settings_sha256' in record and (not isinstance(record['protected_settings_sha256'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', record['protected_settings_sha256'])):
                raise ValueError()
            self.archive_path(record["recovery_backup"])
            return record
        except (OSError, KeyError, ValueError, TypeError) as e:
            raise BackupError("The interrupted restore journal needs manual recovery from a saved backup.") from e

    def _protected_settings(self, record):
        """Missing protection is an error, never an empty current configuration."""
        try:
            stage = self.work / record['stage']
            if stage.resolve().parent != self.work.resolve():
                raise BackupError('Unsafe recovery staging directory.')
            _private_directory(stage)
            raw = _regular_bytes(stage / 'protected-settings.yaml', 8 * 1024 ** 2)
            expected = record.get('protected_settings_sha256')
            if expected is not None and not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected):
                raise BackupError('Recovery protection failed its integrity check.')
            value = yaml.safe_load(raw)
            if not isinstance(value, dict):
                raise BackupError('Recovery protection must contain a configuration mapping.')
            return value
        except (OSError, ValueError, TypeError, yaml.YAMLError, RecursionError):
            raise BackupError('Recovery protection is missing, changed or unreadable. Saved data remains locked; retain the recovery archive and staging files for manual recovery.') from None

    def _cleanup_restore_stage(self, stage):
        shutil.rmtree(stage, ignore_errors=True)

    def apply(self, token, current_browser_state=None, *, password=None, recovery_key=None, recovery_password=None):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise BackupError('Unknown restore preview.')
        self.cleanup_staging(keep_token=token)
        try:
            with self._staging_operation():
                return self._apply(token, current_browser_state, password=password, recovery_key=recovery_key,
                                   recovery_password=recovery_password)
        finally:
            self.cleanup_staging()

    def _apply(self, token, current_browser_state=None, *, password=None, recovery_key=None, recovery_password=None):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise BackupError("Unknown restore preview.")
        result_path = self.results / f"{token}.json"
        if result_path.exists() and not self.gate.fault:
            return _json_file(result_path, 16 * 1024 ** 2)
        folder = self.staging / token
        try:
            self._preview_folder(folder)
            preview = _json_file(folder / "preview.json")
            if not _preview_metadata(preview, token):
                raise ValueError()
        except (OSError, ValueError) as e:
            raise BackupError("Unknown restore preview.") from e
        upload = folder / "upload.zip"
        if preview["expires_at"] < time.time() or _sha(upload, self.gate) != preview["sha256"]:
            raise BackupError("This preview expired or its archive changed. Preview it again.")
        if recovery_password is not None:
            self._validate_password(recovery_password)
        if preview.get('encrypted') and recovery_password is None and not self.gate.fault:
            raise BackupError('Choose a password for the encrypted recovery backup before restoring.')
        with self.gate.exclusive(allow_lease=request_lease(self.gate)):
            repairing = bool(self.gate.fault)
            prior = self._recovery_record() if repairing else None
            if repairing and _sha(upload, self.gate) != _sha(self.archive_path(prior["recovery_backup"]), self.gate):
                raise BackupError("Recover the previous stable backup first, then restore a different archive.")
            protected = self._protected_settings(prior) if repairing else self._settings()
            stage = self.work / ("restore-" + uuid.uuid4().hex)
            stage.mkdir(mode=0o700)
            batch = FileBatch(stage / "rollback")
            batch.rollback.mkdir(mode=0o700)
            changed_database = changed_memories = False
            recovery = None
            try:
                with self._plain_archive(upload, stage / 'unlocked.zip', password=password, recovery_key=recovery_key) as plain:
                    manifest, memories = self._unpack(plain, stage)
                self._normalize_database(stage / "database.sqlite3", stage / "ready.sqlite3")
                ready_prefs = self._prepared_preferences(stage, preview["restore_connections"], protected)
                # A damaged live database must not cause us to replace the only
                # recovery journal, or silently lose request replay barriers.
                epoch = uuid.uuid4().hex
                self._prepare_restored_requests(stage / "ready.sqlite3", epoch)
                previous_memory = None if repairing else self.memories.snapshot()
                if not repairing and current_browser_state is not None:
                    validate_browser_state(current_browser_state)
                    if current_browser_state.get("dataset_epoch") != self.store.dataset_epoch()["value"]:
                        raise BackupError("Reload Altera before restoring from an older data session.")
                recovery = ({"id": prior["recovery_backup"], "download_url": f"/api/backups/{prior['recovery_backup']}/download"}
                            if repairing else self._create(current_browser_state, password=recovery_password))
                if not repairing:
                    self._sqlite_copy(self.store.db_path, stage / "previous.sqlite3")
                (stage / "protected-settings.yaml").write_text(yaml.safe_dump(protected, allow_unicode=True), "utf-8")
                record = {"stage": stage.name, "recovery_backup": recovery["id"], "token": token,
                          'protected_settings_sha256': _sha(stage / 'protected-settings.yaml', self.gate)}
                temporary_journal = self.journal.with_suffix(".tmp")
                temporary_journal.write_text(json.dumps(record), "utf-8")
                os.replace(temporary_journal, self.journal)
                changed_memories = True
                self.memories.restore_snapshot(memories)
                for target_name in sorted(PREFERENCE_TARGETS):
                    source = ready_prefs / target_name
                    if source.exists() or target_name == "user_profile.yaml":
                        batch.replace(self.root / target_name, source if source.exists() else None)
                incoming = stage / "files/data"
                names = {p.name for p in self._data_roots()} | ({p.name for p in incoming.iterdir()} if incoming.exists() else set())
                for name in sorted(names):
                    candidate = self.root / "data" / name
                    if not self._include_data(candidate):
                        raise BackupError("The archive attempts to replace protected runtime data.")
                    source = incoming / name
                    batch.replace(candidate, source if source.exists() else None)
                source = stage / "files/workspace"
                batch.replace(self.root / "workspace", source if source.exists() else None)
                changed_database = True
                self._sqlite_copy(stage / "ready.sqlite3", self.store.db_path)
                self.store._schema_for = None
                self.on_restored()
                browser = stage / "preferences/browser.json"
                browser_state = validate_browser_state(json.loads(_read_snapshot_text(browser, self.gate))) if browser.exists() else None
                result = {"status": "restored", "recovery_backup": recovery, "dataset_epoch": epoch, "counts": preview["counts"], "browser_state": browser_state}
                result_tmp = result_path.with_suffix(".tmp")
                result_tmp.write_text(json.dumps(result), "utf-8")
                os.replace(result_tmp, result_path)
                self.journal.unlink()
            except BaseException as error:
                if recovery is None:
                    if isinstance(error, BackupError):
                        raise
                    raise BackupError("Restore could not be prepared. No application data was replaced.") from error
                try:
                    if repairing:
                        raise BackupError("Recovery did not finish; the original recovery archive is still available.")
                    if changed_database:
                        self._sqlite_copy(stage / "previous.sqlite3", self.store.db_path)
                        self.store._schema_for = None
                    batch.undo()
                    if changed_memories and previous_memory is not None:
                        self.memories.restore_snapshot(previous_memory)
                    self.on_restored()
                    self.journal.unlink(missing_ok=True)
                    result_path.unlink(missing_ok=True)
                except BaseException as rollback_error:
                    self.gate.fail_closed("Restore recovery needs attention. Use the saved recovery backup before resuming Altera.")
                    raise BackupError("Restore and automatic rollback failed. The recovery archive and staged files have been kept.") from rollback_error
                raise BackupError("Restore failed; the previous data was recovered. The recovery backup has been kept.") from error
            else:
                # Publishing the receipt and removing the journal commits the
                # restore. Cleanup must never start a rollback after that point.
                self.gate.recovered()
                if prior is not None:
                    self._cleanup_restore_stage(self.work / prior["stage"])
                try:
                    self._remove_preview(folder)
                except (OSError, BackupError):
                    # Keep the owned marker for bounded expiry cleanup on a
                    # later operation; the completed receipt remains retryable.
                    pass
                return result
            finally:
                # A failed preflight has not published this stage in the
                # recovery journal, even when an older fault remains active.
                if recovery is None or not self.gate.fault:
                    self._cleanup_restore_stage(stage)

    def _prepare_restored_requests(self, ready, epoch):
        """Retire current requests in staging without migrating the live database."""
        try:
            source = Path(self.store.db_path).resolve().as_uri() + '?mode=ro'
            with (_owned_snapshot_stream(sqlite3.connect(source, uri=True), self.gate) as current,
                  _owned_snapshot_stream(sqlite3.connect(ready), self.gate) as restored):
                _backup_write(current, 'PRAGMA trusted_schema=OFF', self.gate)
                current.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64 * 1024 ** 2)
                _backup_write(current, 'BEGIN', self.gate)
                if _backup_query(current, 'PRAGMA quick_check(10)', self.gate) != [('ok',)]:
                    raise BackupError('Current reply history could not be verified. No application data was replaced.')
                with _owned_snapshot_stream(current.execute(
                    'SELECT request_id FROM chat_turns UNION SELECT request_id FROM chat_request_tombstones'
                ), self.gate) as requests:
                    with restored:
                        _backup_write(restored, "INSERT OR REPLACE INTO user_profile(key,value) VALUES('dataset_epoch',?)", self.gate,
                                      (json.dumps({'value': epoch, 'required': True}),))
                        _backup_write(restored, "INSERT OR REPLACE INTO user_profile(key,value) VALUES('adult_mode',?)", self.gate,
                                      (json.dumps({'enabled': False}),))
                        while True:
                            rows = requests.fetchmany(500)
                            if not rows:
                                break
                            with _owned_snapshot_stream(restored.executemany(
                                'INSERT OR IGNORE INTO chat_request_tombstones(request_id) VALUES(?)', rows
                            ), self.gate):
                                pass
        except (OSError, sqlite3.Error) as error:
            raise BackupError('Current reply history could not be verified or prepared. No application data was replaced.') from error


def get_backup_service():
    from core import companion, config, memory, notes, chat
    from core.datastore import get_datastore
    with _BACKUP_SERVICE_LOCK:
        store = get_datastore()
        if not hasattr(store, "_backup_service"):
            def reset():
                config.reset_cache()
                companion.set_services(None)
                notes.set_notes_service(None)
                chat.reset_chat_engine()
                from core.llm import reset_llm_instance
                reset_llm_instance()
            store._backup_service = BackupService(config.ROOT, store, memory.get_memory_manager().store, on_restored=reset)
        return store._backup_service
