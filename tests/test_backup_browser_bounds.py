"""A newly created browser snapshot must fit the restore-preview limit."""
import json

import pytest

from core import backups

pytest_plugins = ('test_backups',)


def test_browser_snapshot_at_utf8_limit_round_trips(backup_world, monkeypatch):
    service = backup_world.service
    browser = {'dataset_epoch': backup_world.store.dataset_epoch()['value'], 'preferences': {},
               'drafts': [{'session_id': backup_world.sid, 'message': 'Synthetic 🌿 draft'}]}
    normalized = backups.validate_browser_state(browser)
    size = len(json.dumps(normalized, ensure_ascii=False).encode('utf-8'))
    monkeypatch.setattr(backups, 'MAX_BROWSER_STATE_BYTES', size)
    saved = service.create(browser)
    preview = service.preview(service.archive_path(saved['id']))
    assert preview['counts']['unsent_drafts'] == 1


@pytest.mark.parametrize('invalid', ['oversize', 'surrogate'])
def test_invalid_browser_snapshot_fails_before_database_or_memory_snapshot(backup_world, monkeypatch, invalid):
    service = backup_world.service
    browser = {'dataset_epoch': backup_world.store.dataset_epoch()['value'], 'preferences': {},
               'drafts': [{'session_id': backup_world.sid, 'message': 'Synthetic 🌿 draft'}]}
    if invalid == 'oversize':
        size = len(json.dumps(backups.validate_browser_state(browser), ensure_ascii=False).encode('utf-8'))
        monkeypatch.setattr(backups, 'MAX_BROWSER_STATE_BYTES', size - 1)
    else:
        browser['drafts'][0]['message'] = '\ud800'
    monkeypatch.setattr(service.memories, 'snapshot', lambda: pytest.fail('Invalid browser data started a snapshot'))
    monkeypatch.setattr(service, '_sqlite_copy', lambda *_args: pytest.fail('Invalid browser data copied the database'))
    with pytest.raises(backups.BackupError, match='too large|encoded safely'):
        service.create(browser)
    assert not list(service.directory.glob('altera-*.zip')) and not list(service.directory.glob('altera-*.alterabak'))
