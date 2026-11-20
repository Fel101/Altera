"""Restore validation owns and fences its private filesystem resources."""
from pathlib import Path

import pytest

from core import backups

pytest_plugins = ('test_backups',)


class UncertainClose:
    def __init__(self, stream, closed, label):
        self.stream, self.closed, self.label = stream, closed, label

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def close(self):
        self.stream.close()
        self.closed.append(self.label)
        raise OSError('synthetic sensitive restore handle')










def test_restore_normalization_discards_expression_and_memory_recovery_intents(backup_world, tmp_path):
    store, service = backup_world.store, backup_world.service
    with store._connect() as conn:
        conn.execute("""INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status,expression_pending,memory_pending)
                        VALUES('pending-expression-123',?,'synthetic','old-process','saved','synthetic-intent','synthetic-memory')""",
                     (backup_world.sid,))
    target = tmp_path / 'normalized.sqlite3'
    service._normalize_database(Path(store.db_path), target)
    with backups.sqlite3.connect(target) as conn:
        assert conn.execute("SELECT expression_pending,memory_pending FROM chat_turns WHERE request_id='pending-expression-123'").fetchone() == (None, None)
    with store._connect() as conn:
        assert tuple(conn.execute("SELECT expression_pending,memory_pending FROM chat_turns WHERE request_id='pending-expression-123'").fetchone()) == ('synthetic-intent', 'synthetic-memory')








@pytest.mark.parametrize('created_at', [None, 'x' * 129])
def test_preview_rejects_unusable_creation_metadata(backup_world, tmp_path, created_at):
    import json
    from test_backups import altered
    service = backup_world.service
    saved = service.create()
    def change(entries):
        manifest = json.loads(entries['manifest.json'])
        manifest['created_at'] = created_at
        entries['manifest.json'] = json.dumps(manifest).encode('utf-8')
    archive = altered(service.archive_path(saved['id']), tmp_path / 'invalid-created-at.zip', change)
    with pytest.raises(backups.BackupError, match='creation time is invalid'):
        service.preview(archive)
