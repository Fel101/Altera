"""Synthetic source identity and cleanup checks for backup snapshots."""
import os
from pathlib import Path

import pytest

from core import backups

pytest_plugins = ('test_backups',)


def test_snapshot_copies_checked_source_with_existing_metadata(backup_world, tmp_path):
    source = backup_world.root / 'workspace' / 'note.txt'
    target = tmp_path / 'snapshot' / 'copy.txt'
    backup_world.service._copy_tree(source, target)
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mtime_ns == source.stat().st_mtime_ns


def test_snapshot_does_not_overwrite_an_existing_target(backup_world, tmp_path):
    source = backup_world.root / 'workspace' / 'note.txt'
    target = tmp_path / 'existing.txt'
    target.write_text('Pre-existing synthetic file', encoding='utf-8')
    with pytest.raises(FileExistsError):
        backup_world.service._copy_tree(source, target)
    assert target.read_text(encoding='utf-8') == 'Pre-existing synthetic file'


def test_snapshot_refuses_source_replacement_before_open(backup_world, tmp_path, monkeypatch):
    source = backup_world.root / 'workspace' / 'note.txt'
    replacement = source.with_name('replacement.txt')
    replacement.write_text('Synthetic replacement', encoding='utf-8')
    target = tmp_path / 'copy.txt'
    real_open = backups.os.open
    replaced = []

    def swap(path, flags, *args, **kwargs):
        if Path(path) == source and not replaced:
            os.replace(replacement, source)
            replaced.append(True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(backups.os, 'open', swap)
    with pytest.raises(backups.BackupError, match='changed outside Altera'):
        backup_world.service._copy_tree(source, target)
    assert replaced and not target.exists()
    assert source.read_text(encoding='utf-8') == 'Synthetic replacement'


def test_snapshot_refuses_source_changed_during_copy(backup_world, tmp_path, monkeypatch):
    source = backup_world.root / 'workspace' / 'note.txt'
    target = tmp_path / 'copy.txt'
    copy = backups.shutil.copyfileobj

    def mutate(incoming, output, length):
        copy(incoming, output, length)
        source.write_text('Synthetic changed source content', encoding='utf-8')

    monkeypatch.setattr(backups.shutil, 'copyfileobj', mutate)
    with pytest.raises(backups.BackupError, match='changed outside Altera'):
        backup_world.service._copy_tree(source, target)
    assert source.read_text(encoding='utf-8') == 'Synthetic changed source content'


def test_snapshot_refuses_directory_changed_during_copy(backup_world, tmp_path, monkeypatch):
    source = backup_world.root / 'workspace'
    copy = backups.shutil.copyfileobj

    def add_file(incoming, output, length):
        copy(incoming, output, length)
        (source / 'added.txt').write_text('Synthetic added file', encoding='utf-8')

    monkeypatch.setattr(backups.shutil, 'copyfileobj', add_file)
    with pytest.raises(backups.BackupError, match='directory changed outside Altera'):
        backup_world.service._copy_tree(source, tmp_path / 'snapshot')
    assert (source / 'added.txt').read_text(encoding='utf-8') == 'Synthetic added file'




@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='Named pipes are unavailable on this platform')
def test_snapshot_refuses_special_file_without_opening_it(backup_world, tmp_path):
    source = tmp_path / 'pipe'
    os.mkfifo(source)
    with pytest.raises(backups.BackupError, match='regular, unlinked'):
        backup_world.service._copy_tree(source, tmp_path / 'copy')
