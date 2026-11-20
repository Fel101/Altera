"""A fresh checkout has no data/ folder. Starting Altera there used to crash at import time.

core/search.py opens the datastore while it is being imported (module-level ConversationSearch()), which runs
`SELECT COUNT(*) FROM messages`, long before altera.py's __main__ block calls init_db(). The DataStore now creates the schema
itself the first time it opens a database file, and leaves an existing database exactly as it was.

Everything here uses temporary directories and never touches the real data/ folder or user_profile.yaml.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import core.datastore as datastore_module
from core.datastore import DataStore, init_db

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TABLES = {"sessions", "messages", "life_events", "user_profile", "messages_fts"}


def table_names(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    finally:
        conn.close()


def schema_sql(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return sorted(row for row in conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    finally:
        conn.close()


def test_datastore_creates_its_schema_the_first_time_it_is_used(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = DataStore("data/conversations.db")
    assert not (tmp_path / "data").exists()                       # constructing a DataStore does no I/O

    store.ensure_fts_table("messages_fts")                        # what core/search.py does at import; used to raise

    assert TABLES <= table_names(tmp_path / "data" / "conversations.db")
    session_id = store.create_session("first chat")
    store.save_message(session_id, "user", "hello")
    assert [m.content for m in store.get_messages(session_id)] == ["hello"]


def test_an_existing_database_is_left_exactly_as_it_was(tmp_path):
    path = str(tmp_path / "old.db")
    init_db(path)
    seeded = DataStore(path)
    session_id = seeded.create_session("kept")
    seeded.save_message(session_id, "user", "still here")
    seeded.set_key("some_key", {"value": 42})
    before = schema_sql(path)

    fresh = DataStore(path)                                       # a new process opening the same file
    assert [m.content for m in fresh.get_messages(session_id)] == ["still here"]
    assert fresh.get_key("some_key") == {"value": 42}
    fresh.ensure_fts_table("messages_fts")
    assert schema_sql(path) == before


def test_a_database_left_half_created_by_the_old_crash_is_repaired(tmp_path):
    """The crashing start-up left data/conversations.db behind with only the FTS table in it."""
    path = tmp_path / "half.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content, session_id, role, mood, timestamp, "
                 "content='messages', content_rowid='rowid')")
    conn.commit()
    conn.close()
    assert "messages" not in table_names(path)

    store = DataStore(str(path))
    store.ensure_fts_table("messages_fts")

    assert TABLES <= table_names(path)
    session_id = store.create_session("after the repair")
    store.save_message(session_id, "user", "works")
    assert [m.content for m in store.get_messages(session_id)] == ["works"]


def test_the_schema_follows_the_working_directory(tmp_path, monkeypatch):
    """A relative db_path resolves against the current directory, so a new directory needs its own schema."""
    store = DataStore("data/conversations.db")
    for name in ("one", "two"):
        folder = tmp_path / name
        folder.mkdir()
        monkeypatch.chdir(folder)
        assert store.create_session(name)
        assert TABLES <= table_names(folder / "data" / "conversations.db")


def test_conversation_search_can_be_created_on_a_fresh_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(datastore_module, "_datastore_instance", None)     # a brand-new shared store, as in a new process
    from core.search import ConversationSearch

    search = ConversationSearch()                                 # the line that used to raise "no such table: messages"

    assert search.get_search_stats()["total_messages"] == 0
    from core.paths import data_path
    assert data_path('conversations.db').exists()
    assert not (tmp_path / 'data').exists()  # Launching from somewhere else must not create another instance.


def run_python(code, cwd, code_root=PROJECT_ROOT):
    env = {**os.environ, "PYTHONPATH": str(code_root), 'ALTERA_HOME': str(cwd), 'ALTERA_DISABLE_AUTOSTART': '1'}
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True, timeout=180)


def test_importing_core_search_in_an_empty_directory_no_longer_crashes(tmp_path):
    result = run_python("import core.search", tmp_path)
    assert result.returncode == 0, result.stderr[-1500:]
    assert not (tmp_path / 'data').exists()  # Imports are quiet; shared application setup owns initialization.


def test_importing_the_whole_app_in_an_empty_directory(tmp_path):
    """The reported failure: copy the project without data/ and start it."""
    shutil.copy(PROJECT_ROOT / 'config.example.yaml', tmp_path / 'config.yaml')
    shutil.copy(PROJECT_ROOT / 'personality.yaml', tmp_path / 'personality.yaml')
    result = run_python("import altera; print(sorted(altera.app.blueprints)[:2])", tmp_path)
    assert result.returncode == 0, result.stderr[-1500:]
    assert TABLES <= table_names(tmp_path / "data" / "conversations.db")


@pytest.mark.parametrize('argument, exit_code', [('--help', 0), ('--unknown-option', 2)])
def test_launcher_parses_arguments_before_opening_instance(tmp_path, argument, exit_code):
    home = tmp_path / 'instance'

    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / 'altera.py'), argument],
        cwd=tmp_path,
        env={**os.environ, 'ALTERA_HOME': str(home), 'ALTERA_DISABLE_AUTOSTART': '1'},
        capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == exit_code, result.stderr
    assert 'usage: altera.py' in result.stdout + result.stderr
    assert not home.exists()


def test_extracted_source_starts_without_an_existing_local_config(tmp_path):
    """A source archive contains the starter, with no developer instance configuration."""
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    shutil.copytree(PROJECT_ROOT / 'core', checkout / 'core', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree(PROJECT_ROOT / 'content', checkout / 'content')
    for name in ('altera.py', 'config.example.yaml', 'personality.yaml'):
        shutil.copy(PROJECT_ROOT / name, checkout / name)
    assert not (checkout / 'config.yaml').exists()
    starter = (checkout / 'config.example.yaml').read_bytes()
    code = """
import openai
def blocked(*args, **kwargs):
    raise RuntimeError('Inference is forbidden in this startup test')
openai.OpenAI = blocked
import altera
assert altera.app.test_client().get('/api/offline/status').json['online']
"""
    result = run_python(code, checkout, code_root=checkout)
    assert result.returncode == 0, result.stderr[-1500:]
    assert (checkout / 'config.yaml').read_bytes() == starter
    assert (checkout / 'config.example.yaml').read_bytes() == starter
    assert TABLES <= table_names(checkout / 'data/conversations.db')
