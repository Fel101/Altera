"""The app lock: a parent PIN, a lock screen, and a kids view the server enforces.

The model is never called here. The kids-view tests only read lists and check refusals; the one chat refusal happens
before the chat engine is reached.
"""
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

from core import kids, lock, pals
from core.app import create_app
from test_backups import backup_world  # noqa: F401
from test_companion_api import tmp_store, world  # noqa: F401

PIN = "1234"
NEW_PIN = "5678"


def make_pal(world, name, kids_on=False):
    return pals.create(world.avatars, name, "sunny", "", engine="kokoro", kids=kids_on)


def reset_pin_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "reset_pin.py"
    spec = importlib.util.spec_from_file_location("altera_reset_pin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app(world):
    return create_app(autostart=False)


@pytest.fixture
def client(app):
    return app.test_client()


def enable_with_pin(client, pin=PIN):
    response = client.post("/api/kids", json={"enabled": True, "understood": True, "pin": pin})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


# ---------------------------------------------------------------- the PIN itself

def test_a_pin_is_stored_as_a_slow_salted_hash_and_never_in_the_clear(world):
    lock.set_pin(PIN, store=world.store)
    raw = world.store.get_key(lock.PIN_KEY)
    assert raw["hash"] and raw["salt"] and raw["hash"] != PIN
    assert PIN not in json.dumps(raw)
    assert lock.verify_pin(PIN, world.store) is True
    with pytest.raises(lock.LockError) as wrong:
        lock.verify_pin("0000", world.store)
    assert wrong.value.status == 403 and wrong.value.code == "bad_pin"


def test_a_pin_is_4_to_8_digits(world):
    for bad in ("123", "123456789", "12a4", "12 34", "", None, 1234):
        with pytest.raises(lock.LockError):
            lock.set_pin(bad, store=world.store)
    assert lock.has_pin(world.store) is False


def test_five_wrong_tries_lock_for_a_wait(world):
    lock.set_pin(PIN, store=world.store)
    for _ in range(lock.MAX_FAILS):
        with pytest.raises(lock.LockError):
            lock.verify_pin("0000", world.store)
    with pytest.raises(lock.LockError) as limited:
        lock.verify_pin(PIN, world.store)
    assert limited.value.status == 429 and limited.value.code == "rate_limited" and limited.value.retry_after


def test_setting_a_second_pin_needs_the_current_one(world):
    lock.set_pin(PIN, store=world.store)
    with pytest.raises(lock.LockError):
        lock.set_pin(NEW_PIN, current="0000", store=world.store)
    assert lock.verify_pin(PIN, world.store) is True
    lock.set_pin(NEW_PIN, current=PIN, store=world.store)
    assert lock.verify_pin(NEW_PIN, world.store) is True
    with pytest.raises(lock.LockError):
        lock.verify_pin(PIN, world.store)


# ---------------------------------------------------------------- the lock screen

def test_the_lock_screen_is_shown_and_the_pin_opens_the_app(client, world):
    assert client.get("/api/lock").get_json()["locked"] is False
    enable_with_pin(client)
    make_pal(world, "Pip", kids_on=True)

    assert client.post("/api/lock/lock").status_code == 200
    page = client.get("/")
    assert page.status_code == 302 and page.headers["Location"].endswith("/lock")
    refused = client.get("/api/people")
    assert refused.status_code == 423 and refused.get_json()["code"] == "locked"
    assert client.get("/lock").status_code == 200

    assert client.post("/api/lock/unlock", json={"pin": "0000"}).status_code == 403
    assert client.post("/api/lock/unlock", json={"pin": PIN}).status_code == 200
    assert client.get("/api/people").status_code == 200


def test_a_pin_that_cannot_be_read_keeps_a_locked_browser_locked(client, world, monkeypatch):
    enable_with_pin(client)
    make_pal(world, "Pip", kids_on=True)
    assert client.post("/api/lock/lock").status_code == 200

    def broken(store=None):
        raise OSError("the database is busy")
    monkeypatch.setattr(lock, "pin_state", broken)
    refused = client.get("/api/people")
    assert refused.status_code == 423 and refused.get_json()["code"] == "locked"   # not "no PIN, come in"
    with pytest.raises(OSError):
        lock.set_pin(NEW_PIN, store=world.store)                                    # nor a new PIN without the old one


def test_the_lock_status_never_carries_the_pin_or_its_hash(client, world):
    enable_with_pin(client)
    body = client.get("/api/lock").get_data(as_text=True)
    assert PIN not in body
    assert "app_pin" not in body and "salt" not in body and '"hash"' not in body


# ---------------------------------------------------------------- the kids view

def test_the_kids_view_only_reaches_kids_pals(client, world):
    enable_with_pin(client)
    pip = client.post("/api/pals", json={"name": "Pip", "vibe": "sunny", "kids": True}).get_json()
    sky = client.post("/api/pals", json={"name": "Sky", "vibe": "sunny"}).get_json()

    entered = client.post("/api/lock/kids")
    assert entered.status_code == 200 and entered.get_json()["kids_view"] is True
    kids_session = entered.get_json()["avatar"]["session_id"]

    page = client.get("/")
    assert page.status_code == 200 and b'class="kids-view"' in page.data

    # Lists are filtered on the server, not only in the page.
    assert [p["id"] for p in client.get("/api/companions").get_json()["companions"]] == [pip["id"]]
    assert [a["id"] for a in client.get("/api/avatars").get_json()["avatars"]] == [pip["id"]]
    assert client.get("/api/companions/current").get_json()["avatar"]["id"] == pip["id"]

    # Every other page and API is refused for this browser.
    for path in ("/api/people", "/api/search", "/api/memory", "/api/adult", "/api/knowledge/scopes",
                 "/api/profile", "/api/llm/active", "/api/tools/files"):
        assert client.get(path).status_code == 403, path
    for path in ("/home", "/memory", "/search", "/timeline", "/knowledge"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"].endswith("/"), path

    # A grown-up's conversation cannot be read, opened or sent to.
    adult = world.store.create_session("Grown-up chat")
    world.store.save_message(adult, "user", "Synthetic adult context")
    assert client.get(f"/api/messages/{adult}").status_code == 403
    assert client.post(f"/api/companions/{sky['id']}/open").status_code == 403
    assert client.post("/api/chat", json={"session_id": adult, "message": "hello"}).status_code == 403

    # The kids pal's own chat is reachable.
    assert client.get(f"/api/messages/{kids_session}").status_code == 200

    # Leaving needs the PIN, and only then does the rest of the app open again.
    assert client.post("/api/lock/leave", json={"pin": "0000"}).status_code == 403
    assert client.post("/api/lock/leave", json={"pin": PIN}).status_code == 200
    assert client.get("/api/people").status_code == 200


def test_a_kids_view_never_changes_who_another_browser_opens_with(app, world):
    """The household's current person is shared. A tablet in the kids view keeps its own pal in its own session."""
    grown_up, tablet = app.test_client(), app.test_client()
    enable_with_pin(grown_up)
    pip = grown_up.post("/api/pals", json={"name": "Pip", "vibe": "sunny", "kids": True}).get_json()
    sky = grown_up.post("/api/pals", json={"name": "Sky", "vibe": "sunny"}).get_json()
    assert grown_up.post(f"/api/companions/{sky['id']}/open").status_code == 200
    assert world.avatars.active_id() == sky["id"]

    assert tablet.post("/api/lock/kids").status_code == 200
    assert tablet.get("/api/companions/current").get_json()["avatar"]["id"] == pip["id"]
    assert tablet.get("/").status_code == 200
    opened = tablet.post(f"/api/companions/{pip['id']}/open")
    assert opened.status_code == 200, opened.get_json()
    assert tablet.get("/api/companions").get_json()["active"] == pip["id"]

    # The grown-up's browser still opens with Sky.
    assert world.avatars.active_id() == sky["id"]
    assert grown_up.get("/api/companions/current").get_json()["avatar"]["id"] == sky["id"]


def test_a_kids_chat_must_name_its_conversation(client, world):
    """With no session a chat starts a conversation that belongs to nobody, which falls back to the household's person."""
    enable_with_pin(client)
    make_pal(world, "Pip", kids_on=True)
    assert client.post("/api/lock/kids").status_code == 200
    assert client.post("/api/chat", json={"message": "hello"}).status_code == 403
    assert client.post("/api/chat/stream", json={"message": "hello"}).status_code == 403


def test_a_kids_chat_can_move_between_its_own_replies_only(client, world):
    enable_with_pin(client)
    make_pal(world, "Pip", kids_on=True)
    kids_session = client.post("/api/lock/kids").get_json()["avatar"]["session_id"]
    world.store.save_message(kids_session, "user", "Tell me a story")
    kids_reply = world.store.save_message(kids_session, "assistant", "Once upon a time")
    adult = world.store.create_session("Grown-up chat")
    world.store.save_message(adult, "user", "Synthetic adult context")
    adult_reply = world.store.save_message(adult, "assistant", "Synthetic adult reply")
    assert client.get(f"/api/messages/{kids_reply}/versions").status_code == 200
    assert client.get(f"/api/messages/{adult_reply}/versions").status_code == 403
    assert client.post(f"/api/messages/{adult_reply}/versions/{adult_reply}", json={"session_id": adult}).status_code == 403

def test_the_kids_view_needs_a_pin_and_a_kids_pal(client, world):
    # An older install can have kids mode on with no PIN: it is asked to set one first.
    kids.enable(True)
    assert client.post("/api/lock/kids").status_code == 409
    assert client.post("/api/lock/kids").get_json()["code"] == "no_pin"

    enable_with_pin(client)
    refused = client.post("/api/lock/kids")
    assert refused.status_code == 409 and refused.get_json()["code"] == "no_kids_pals"

    make_pal(world, "Pip", kids_on=True)
    assert client.post("/api/lock/kids").status_code == 200


def test_a_forgotten_pin_is_reset_by_the_file_script(tmp_path):
    from core.datastore import DataStore, init_db

    path = tmp_path / "data" / "conversations.db"
    path.parent.mkdir(parents=True)
    init_db(str(path))
    store = DataStore(str(path))
    lock.set_pin(PIN, store=store)
    store.set_key("kids_mode", {"enabled": True, "enabled_at": "2026-11-18T00:00:00"})

    removed = reset_pin_module().reset(tmp_path)

    assert removed == 2
    assert lock.has_pin(store) is False
    assert store.get_key("kids_mode")["enabled"] is True          # kids mode itself is left alone


def test_a_backup_carries_the_pin_hash(backup_world):
    lock.set_pin(PIN, store=backup_world.store)
    saved = backup_world.service.create()
    with zipfile.ZipFile(backup_world.service.archive_path(saved["id"])) as archive:
        database = archive.read("database.sqlite3")
    assert b"app_pin" in database
    assert backup_world.store.get_key(lock.PIN_KEY)["hash"] != PIN
