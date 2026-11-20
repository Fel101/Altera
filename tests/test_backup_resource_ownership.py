"""Synthetic backup resource cleanup failures fence private work."""
import zipfile

import pytest

from core import backups

pytest_plugins = ('test_backups',)






def test_ordinary_sqlite_close_failure_propagates_without_fencing(backup_world, monkeypatch):
    service, closed = backup_world.service, []

    class Connection:
        def backup(self, _destination):
            pass

        def close(self):
            closed.append(True)
            raise OSError('synthetic ordinary close failure')

    monkeypatch.setattr(backups.sqlite3, 'connect', lambda *_args, **_kwargs: Connection())
    with pytest.raises(OSError, match='synthetic ordinary close failure'):
        service._sqlite_copy('synthetic source', 'synthetic destination')
    assert len(closed) == 2 and not service.gate.draining










def test_snapshot_archive_preserves_deflated_unicode_files(backup_world, tmp_path):
    stage = tmp_path / 'stage'
    stage.mkdir()
    (stage / '日本語.txt').write_text('Synthetic Unicode 🌿', encoding='utf-8')
    archive_path = tmp_path / 'snapshot.zip'
    backup_world.service._write_snapshot_archive(stage, archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        assert archive.getinfo('日本語.txt').compress_type == zipfile.ZIP_DEFLATED
        assert archive.read('日本語.txt').decode('utf-8') == 'Synthetic Unicode 🌿'
