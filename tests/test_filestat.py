"""A file's fingerprint must read the same from its path and from an open handle, on Python 3.11, 3.12 and 3.13.

From 3.12 on Windows, st_ctime from a path is the creation time but from an open file the last change, so backups,
encrypted archives, notebook clean-up and the MiniLM model check all refused unchanged files until core.filestat.
"""
import os
from types import SimpleNamespace as NS

from core import filestat


def test_a_real_file_has_the_same_change_stamp_from_its_path_and_from_an_open_handle(tmp_path):
    path = tmp_path / 'notes.txt'
    path.write_text('first', encoding='utf-8')
    with path.open('ab') as stream:
        stream.write(b' and more')          # a metadata change after creation
    with path.open('rb') as stream:
        assert filestat.ctime_ns(os.fstat(stream.fileno())) == filestat.ctime_ns(path.lstat())
        assert filestat.ctime_ns(os.fstat(stream.fileno())) == filestat.ctime_ns(os.stat(path))


def test_windows_uses_the_creation_time_and_everywhere_else_the_change_time(monkeypatch):
    info = NS(st_ctime_ns=2, st_birthtime_ns=1)
    monkeypatch.setattr(filestat.os, 'name', 'nt')
    assert filestat.ctime_ns(info) == 1
    assert filestat.ctime_ns(NS(st_ctime_ns=2)) == 2          # Python 3.11: st_ctime was the creation time there
    monkeypatch.setattr(filestat.os, 'name', 'posix')
    assert filestat.ctime_ns(info) == 2
