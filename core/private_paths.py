"""Confined instance temporary files, without encryption or automatic old-file cleanup.

Only files exclusively created by a context are removed. Existing data is never
truncated, swept, or repaired. Directory links/junctions fail closed. As with the
instance's other stores, concurrent external filesystem writers are unsupported.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import uuid


class PrivatePathError(OSError):
    """The instance temporary directory or owned temporary file is unsafe."""


def _directory(path):
    """Validate before descending; Windows junctions are reparse points too."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise PrivatePathError('Private temporary paths cannot use directory links or reparse points.')
    if not stat.S_ISDIR(info.st_mode):
        raise PrivatePathError('A private temporary path is not a directory.')
    return info


def _base(root=None):
    if root is None:
        from core.paths import root as instance_root
        root = instance_root()
    base = Path(root).expanduser().absolute()
    # A caller can explicitly choose an instance, but cannot make its final
    # directory a link and accidentally put private files in another instance.
    if base.exists() or base.is_symlink():
        _directory(base)
    else:
        base.mkdir(parents=True, mode=0o700, exist_ok=True)
        _directory(base)
    return base.resolve(strict=True)


def _check_temp(base, folder):
    _directory(base)
    _directory(base / 'data')
    info = _directory(folder)
    if folder.resolve(strict=True) != base / 'data' / 'tmp':
        raise PrivatePathError('Private temporary paths must remain inside the instance.')
    if os.name != 'nt' and info.st_mode & 0o077:
        raise PrivatePathError('The existing private temporary directory must be accessible only to its owner.')
    return info


def private_temp_dir(root=None):
    """Return instance/data/tmp; explicit root overrides never change process state."""
    base = _base(root)
    for folder in (base / 'data', base / 'data' / 'tmp'):
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _directory(folder)
    folder = base / 'data' / 'tmp'
    _check_temp(base, folder)
    return folder


def _same_file(info, identity):
    return stat.S_ISREG(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400 and (info.st_dev, info.st_ino) == identity


def _remove_created(base, folder, path, identity):
    _check_temp(base, folder)
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if not _same_file(current, identity):
        raise PrivatePathError('The owned temporary file changed; a replacement was left untouched.')
    path.unlink()


@contextmanager
def temporary_file(data=b'', *, suffix='', prefix='altera-', root=None):
    """Write an exclusive private file, yield its closed Path, then remove it.

    The descriptor is closed before yielding so Windows consumers can reopen it.
    Cleanup also runs after a partial write or consumer failure, and cleanup
    errors are surfaced. This cannot guarantee erasure after a process crash.
    """
    if not isinstance(prefix, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', prefix):
        raise ValueError('A temporary prefix must be a short plain filename prefix.')
    if not isinstance(suffix, str) or not re.fullmatch(r'(?:\.[A-Za-z0-9_-]{1,20})?', suffix):
        raise ValueError('A temporary suffix must be empty or a short plain extension.')
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError('Private temporary file data must be bytes.')
    remaining = memoryview(data).cast('B')
    folder = private_temp_dir(root)
    base = folder.parent.parent
    fd, path, identity = None, None, None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        for _ in range(16):
            _check_temp(base, folder)
            candidate = folder / (prefix + uuid.uuid4().hex + suffix)
            try:
                fd = os.open(candidate, flags, 0o600)
            except FileExistsError:
                continue
            path = candidate
            info = os.fstat(fd)
            identity = (info.st_dev, info.st_ino)
            if not _same_file(info, identity):
                raise PrivatePathError('A private temporary file could not be created safely.')
            break
        if fd is None:
            raise PrivatePathError('A unique private temporary filename could not be created.')
        _check_temp(base, folder)
        if not _same_file(path.lstat(), identity):
            raise PrivatePathError('The private temporary file changed before writing.')
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError('The private temporary write did not make progress.')
            remaining = remaining[written:]
        os.close(fd)
        fd = None
        yield path
    finally:
        try:
            if fd is not None:
                os.close(fd)
        finally:
            if path is not None and identity is not None:
                _remove_created(base, folder, path, identity)
