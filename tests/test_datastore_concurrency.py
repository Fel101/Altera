"""Real SQLite startup/concurrency contracts on isolated synthetic databases."""
import sqlite3

from core.datastore import DataStore


def test_initial_schema_publishes_persistent_wal_before_normal_queries(tmp_path):
    store = DataStore(tmp_path / 'fresh.db')
    store._ensure_schema()
    independent = sqlite3.connect(store.db_path)
    try:
        assert independent.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert independent.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        independent.close()


def test_first_application_read_can_overlap_an_uncommitted_background_write(tmp_path):
    store = DataStore(tmp_path / 'concurrent.db')
    store._ensure_schema()
    writer = sqlite3.connect(store.db_path)
    try:
        writer.execute("INSERT INTO sessions (name) VALUES ('synthetic pending write')")
        # Read the committed snapshot while an independent writer is active.
        # No journal transition should be attempted by an ordinary query.
        assert store.list_sessions() == []
        writer.commit()
        assert [row.name for row in store.list_sessions()] == ['synthetic pending write']
    finally:
        writer.close()
