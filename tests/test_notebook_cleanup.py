"""Notebook deletion refuses filesystem aliases and unverified cleanup."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import notebook_cleanup
from core.notes import NotesStore


@pytest.fixture
def notebook(tmp_path):
    root = tmp_path / 'avatars'
    avatar = root / 'synthetic-mara'
    folder = avatar / 'notes'
    (folder / 'history').mkdir(parents=True)
    (folder / 'about_you.md').write_text('synthetic notes', encoding='utf-8')
    (folder / 'history' / 'about_you-old.md').write_text('synthetic old notes', encoding='utf-8')
    avatars = SimpleNamespace(root=root, _dir=lambda _identifier: avatar)
    return SimpleNamespace(root=root, avatar=avatar, folder=folder, store=NotesStore(avatars))


def test_checked_notebook_cleanup_removes_only_owned_notes(notebook, tmp_path):
    neighbor = notebook.avatar / 'portrait.png'
    neighbor.write_bytes(b'synthetic portrait')
    outside = tmp_path / 'outside.txt'
    outside.write_text('keep', encoding='utf-8')
    notebook.store.clear('synthetic-mara')
    assert not notebook.folder.exists()
    assert neighbor.read_bytes() == b'synthetic portrait' and outside.read_text(encoding='utf-8') == 'keep'
    notebook.store.clear('synthetic-mara')  # Already absent is safe to retry.


def test_missing_companion_is_not_treated_as_confirmed_notebook_cleanup(notebook):
    notebook.store.avatars._dir = lambda _identifier: notebook.root / 'missing-companion'
    with pytest.raises(OSError, match='owner is unavailable'):
        notebook.store.clear('missing-companion')
    assert (notebook.folder / 'about_you.md').exists()


def test_hardlinked_note_refuses_before_deleting_anything(notebook, tmp_path):
    outside = tmp_path / 'outside.txt'
    outside.write_text('keep', encoding='utf-8')
    try:
        os.link(outside, notebook.folder / 'linked.txt')
    except OSError:
        pytest.skip('This filesystem cannot create a disposable hard link.')
    with pytest.raises(OSError, match='could not be cleared safely'):
        notebook.store.clear('synthetic-mara')
    assert outside.read_text(encoding='utf-8') == 'keep'
    assert (notebook.folder / 'about_you.md').read_text(encoding='utf-8') == 'synthetic notes'
    assert (notebook.folder / 'history' / 'about_you-old.md').exists()


@pytest.mark.parametrize('where', ['avatar', 'notebook', 'nested'])
def test_windows_reparse_attributes_are_refused_before_deletion(notebook, monkeypatch, where):
    paths = {'avatar': notebook.avatar, 'notebook': notebook.folder, 'nested': notebook.folder / 'history'}
    target, real_lstat = paths[where], Path.lstat
    class ReparseInfo:
        def __init__(self, info):
            self.info = info

        def __getattr__(self, name):
            if name == 'st_file_attributes':
                return getattr(self.info, name, 0) | 0x400
            return getattr(self.info, name)

    def marked(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        return ReparseInfo(info) if path == target else info

    monkeypatch.setattr(Path, 'lstat', marked)
    with pytest.raises(OSError, match='could not be cleared safely'):
        notebook.store.clear('synthetic-mara')
    assert (notebook.folder / 'about_you.md').exists()
    assert (notebook.folder / 'history' / 'about_you-old.md').exists()


@pytest.mark.parametrize('where', ['avatar', 'notebook', 'nested'])
def test_linked_directories_are_refused_without_touching_target(notebook, tmp_path, where):
    outside = tmp_path / 'external'
    outside.mkdir()
    canary = outside / 'canary'
    canary.write_text('keep', encoding='utf-8')
    if where == 'avatar':
        path = notebook.root / 'linked-avatar'
        notebook.store.avatars._dir = lambda _identifier: path
    elif where == 'notebook':
        path = notebook.avatar / 'linked-notes'
        notebook_cleanup_path = notebook.avatar / 'notes'
        # The empty real notebook is removed only within this disposable test fixture.
        (notebook_cleanup_path / 'history' / 'about_you-old.md').unlink()
        (notebook_cleanup_path / 'history').rmdir()
        (notebook_cleanup_path / 'about_you.md').unlink()
        notebook_cleanup_path.rmdir()
        path = notebook_cleanup_path
    else:
        path = notebook.folder / 'linked-directory'
    try:
        path.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('This account cannot create a disposable directory link.')
    with pytest.raises(OSError, match='could not be cleared safely'):
        notebook.store.clear('synthetic-mara')
    assert canary.read_text(encoding='utf-8') == 'keep'
    if where == 'nested':
        assert (notebook.folder / 'about_you.md').exists()


def test_changed_file_and_entry_limit_leave_notes_intact(notebook, monkeypatch):
    monkeypatch.setattr(notebook_cleanup, 'MAX_ENTRIES', 1)
    with pytest.raises(OSError, match='cleanup limit'):
        notebook.store.clear('synthetic-mara')
    assert (notebook.folder / 'about_you.md').exists()
    monkeypatch.setattr(notebook_cleanup, 'MAX_ENTRIES', 10000)
    target = notebook.folder / 'about_you.md'
    original, count = Path.lstat, [0]
    def changed(path, *args, **kwargs):
        if path == target:
            count[0] += 1
            if count[0] == 2:
                target.write_text('newer synthetic notes', encoding='utf-8')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', changed)
    with pytest.raises(OSError, match='changed during cleanup'):
        notebook.store.clear('synthetic-mara')
    assert target.read_text(encoding='utf-8') == 'newer synthetic notes'
    assert (notebook.folder / 'history' / 'about_you-old.md').exists()
