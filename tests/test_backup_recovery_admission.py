"""Synthetic recovery-journal races must not admit new backup snapshots."""
import pytest

from core.backups import BackupError

pytest_plugins = ('test_backups',)


@pytest.mark.parametrize('journal_appears', ['before_sweep', 'after_sweep'])
def test_backup_creation_refuses_journal_discovered_during_staging_admission(backup_world, monkeypatch, journal_appears):
    service = backup_world.service
    journal = service.journal
    marker = b'Synthetic interrupted restore; preserve for review'
    original_cleanup = service.cleanup_staging

    if journal_appears == 'before_sweep':
        journal.write_bytes(marker)
    else:
        def cleanup_then_journal(*args, **kwargs):
            result = original_cleanup(*args, **kwargs)
            journal.write_bytes(marker)
            return result
        monkeypatch.setattr(service, 'cleanup_staging', cleanup_then_journal)

    monkeypatch.setattr(service.memories, 'snapshot', lambda: pytest.fail('A faulted snapshot must not start'))
    with pytest.raises(BackupError, match='Recover the interrupted restore'):
        service.create()
    assert service.gate.fault and journal.read_bytes() == marker
    assert not list(service.directory.glob('altera-*.zip')) and not list(service.directory.glob('altera-*.alterabak'))
    assert [message.content for message in backup_world.store.get_messages(backup_world.sid)] == ['Hello', 'Hi']


@pytest.mark.parametrize('journal_appears', ['before_sweep', 'after_sweep'])
def test_private_preview_refuses_journal_discovered_during_staging_admission(backup_world, monkeypatch, journal_appears):
    service = backup_world.service
    marker = b'Synthetic interrupted restore; preserve for review'
    original_cleanup = service.cleanup_staging
    if journal_appears == 'before_sweep':
        service.journal.write_bytes(marker)
    else:
        def cleanup_then_journal(*args, **kwargs):
            result = original_cleanup(*args, **kwargs)
            service.journal.write_bytes(marker)
            return result
        monkeypatch.setattr(service, 'cleanup_staging', cleanup_then_journal)
    monkeypatch.setattr(service, '_preview', lambda *args, **kwargs: pytest.fail('Faulted private preview must not begin'))
    with pytest.raises(BackupError, match='Recover the interrupted restore'):
        service.preview('synthetic archive', require_healthy=True)
    assert service.gate.fault and service.journal.read_bytes() == marker
