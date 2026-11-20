"""Bounded, ownership-checked removal of a companion's derived notebook."""
import os
from contextlib import contextmanager
from pathlib import Path
import stat

from core.filestat import ctime_ns


MAX_ENTRIES = 10000
MAX_DEPTH = 16


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _identity(info, *, file=False):
    value = (info.st_dev, info.st_ino, info.st_mode, getattr(info, 'st_file_attributes', 0))
    return value + (info.st_nlink, info.st_size, info.st_mtime_ns, ctime_ns(info)) if file else value


def _directory(path):
    info = path.lstat()
    if _linked(info) or not stat.S_ISDIR(info.st_mode):
        raise OSError('The companion notebook could not be cleared safely.')
    return _identity(info)


def _file(path):
    info = path.lstat()
    if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError('The companion notebook could not be cleared safely.')
    return _identity(info, file=True)


@contextmanager
def _entries(path):
    scan = os.scandir(path)
    try:
        yield (Path(entry.path) for entry in scan)
    finally:
        try:
            scan.close()
        except BaseException:
            raise OSError('The companion notebook inspection could not be closed safely.') from None


def clear_notebook(root, avatar):
    """Remove a checked notebook tree; refuse aliases and changed entries.

    This does not provide OS-level isolation from a concurrent same-account
    filesystem attacker, and it does not promise secure erasure.
    """
    root = Path(os.path.abspath(root))
    avatar = Path(os.path.abspath(avatar))
    if avatar.parent != root:
        raise OSError('The companion notebook could not be cleared safely.')
    notebook = avatar / 'notes'
    directories = {}
    for path in (root, avatar):
        try:
            directories[path] = _directory(path)
        except FileNotFoundError:
            raise OSError('The companion notebook owner is unavailable.') from None
    try:
        directories[notebook] = _directory(notebook)
    except FileNotFoundError:
        if _directory(root) != directories[root] or _directory(avatar) != directories[avatar]:
            raise OSError('The companion notebook changed during cleanup.') from None
        return

    def verify_chain(path):
        current = path
        while True:
            expected = directories.get(current)
            if expected is None or _directory(current) != expected:
                raise OSError('The companion notebook changed during cleanup.')
            if current == root:
                return
            current = current.parent

    stack, files, count = [(notebook, 0)], [], 0
    while stack:
        folder, depth = stack.pop()
        verify_chain(folder)
        with _entries(folder) as entries:
            for entry in entries:
                count += 1
                if count > MAX_ENTRIES:
                    raise OSError('The companion notebook exceeds the cleanup limit.')
                info = entry.lstat()
                if _linked(info):
                    raise OSError('The companion notebook could not be cleared safely.')
                if stat.S_ISDIR(info.st_mode):
                    if depth >= MAX_DEPTH:
                        raise OSError('The companion notebook exceeds the cleanup limit.')
                    directories[entry] = _identity(info)
                    stack.append((entry, depth + 1))
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    files.append((entry, _identity(info, file=True)))
                else:
                    raise OSError('The companion notebook could not be cleared safely.')

    # Verify every planned entry before removing any of them.
    for path, expected in files:
        verify_chain(path.parent)
        if _file(path) != expected:
            raise OSError('The companion notebook changed during cleanup.')
    for folder in directories:
        verify_chain(folder)
    for path, expected in files:
        verify_chain(path.parent)
        if _file(path) != expected:
            raise OSError('The companion notebook changed during cleanup.')
        path.unlink()
    for folder in sorted((path for path in directories if path not in {root, avatar}),
                         key=lambda path: len(path.parts), reverse=True):
        verify_chain(folder)
        folder.rmdir()
