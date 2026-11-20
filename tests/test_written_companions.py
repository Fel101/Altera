"""A companion written in Altera, without NPC Forge: the card, the age and safety rules, the initial portrait, an uploaded
picture, and a portrait painted by NPC Forge through the ordinary dialog.

NPC Forge, the age check and the chat model are fakes: nothing here touches the real data folder or a running NPC Forge.
"""
import io
import struct
import zlib

import pytest

from core import avatars, companion, companions, portraits, written
from core.avatars import written_here
from core.config import get_personality
from core.prompts import PersonaSection
from test_blueprints import create_app
from test_companion_api import active_avatar, tmp_store, world  # noqa: F401
from test_companion_images import FakeChecker
from test_portraits import FakeForge, painted


def png(width=4, height=4) -> bytes:
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b""))


@pytest.fixture
def client(world):
    return create_app().test_client()


def a_companion(world, name="Mira", **kw):
    return written.create(world.avatars, name=name, age=kw.pop("age", 34), **kw)


# ---------------------------------------------------------------- the card

def test_writing_a_companion_makes_the_same_card_and_a_working_chat(world):
    made = a_companion(world, gender="female", bio="A cartographer who likes rain.", hobbies="maps, tea",
                       speech="dry and warm", demeanor="calm", quirk="names every houseplant",
                       goal="a bookshop by the sea", peeve="cruelty")
    assert made["kind"] == "companion" and made["written"] is True and made["age"] == 34
    assert made["active"] is True
    record = world.avatars.get(made["id"])
    assert record["source"] == {"type": "written"}
    assert record["features"] == {"memory": True, "notes": True, "story": True, "presence": True}
    assert record["card"]["hobbies"] == ["maps", "tea"]
    opened = companions.open_companion(made["id"])
    assert opened["session_id"] and opened["messages"] == 0
    persona = PersonaSection(get_personality(), {}).build(record)
    for text in ("Mira", "cartographer", "dry and warm", "names every houseplant", "bookshop", "cruelty"):
        assert text in persona


def test_a_forge_companion_is_not_written_here(world):
    mara = active_avatar(world)
    assert written_here(mara) is False and mara["written"] is False


def test_the_age_rule_and_card_problem_apply_exactly_as_an_import(world):
    with pytest.raises(written.WrittenError) as young:
        written.create(world.avatars, name="Pip", age=17)
    assert "18" in str(young.value)
    with pytest.raises(written.WrittenError):
        written.create(world.avatars, name="Pip", age="old")
    with pytest.raises(written.WrittenError) as minor:
        written.create(world.avatars, name="Pip", age=34, bio="She is 16 years old.")
    assert "minor" in str(minor.value)
    with pytest.raises(written.WrittenError):
        written.create(world.avatars, name="  ", age=34)
    with pytest.raises(written.WrittenError):
        written.create(world.avatars, name="Pip", age=34, gender="robot")


def test_a_written_companion_can_be_edited(world):
    made = a_companion(world, bio="A cartographer.")
    changed = written.update(world.avatars, made["id"], name="Mira the Elder", age=35, bio="A cartographer of the north.",
                             hobbies=["maps"], goal="a lighthouse")
    assert changed["name"] == "Mira the Elder" and changed["age"] == 35
    assert changed["hobbies"] == ["maps"] and changed["goal"] == "a lighthouse"
    record = world.avatars.get(made["id"])
    assert record["name"] == "Mira the Elder" and record["card"]["bio"] == "A cartographer of the north."
    assert record["id"] == made["id"] and record["kind"] == "companion"
    with pytest.raises(written.WrittenError):
        written.update(world.avatars, made["id"], age=12)
    with pytest.raises(written.WrittenError):
        written.settings(world.avatars, "nobody-here-1")


# ---------------------------------------------------------------- the routes

def test_the_routes_write_open_edit_and_list_a_companion(client, world):
    body = {"name": "Mira", "age": 34, "gender": "female", "bio": "A cartographer.", "hobbies": "maps, tea",
            "speech": "dry", "demeanor": "calm", "quirk": "plants", "goal": "a bookshop", "peeve": "cruelty"}
    made = client.post("/api/companions", json=body)
    assert made.status_code == 201
    opened = made.get_json()
    assert opened["name"] == "Mira" and opened["session_id"] and opened["kind"] == "companion"

    settings = client.get(f"/api/companions/{opened['id']}")
    assert settings.status_code == 200
    assert settings.get_json()["bio"] == "A cartographer." and settings.get_json()["hobbies"] == ["maps", "tea"]

    changed = client.put(f"/api/companions/{opened['id']}", json={"bio": "A cartographer of the north."})
    assert changed.status_code == 200 and changed.get_json()["bio"] == "A cartographer of the north."

    people = client.get("/api/people").get_json()
    assert people["counts"]["companion"] == 1 and people["people"][0]["id"] == opened["id"]

    assert client.post("/api/companions", json={"name": "Pip", "age": 12}).status_code == 400
    assert client.post("/api/companions", json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.put(f"/api/companions/{opened['id']}", json={"unknown": 1}).status_code == 400
    assert client.get("/api/companions/nobody-here-1").status_code == 404
    assert client.put("/api/companions/nobody-here-1", json={"bio": "x"}).status_code == 404


def test_a_written_companion_starts_with_its_initial(client, world):
    made = client.post("/api/companions", json={"name": "Mira", "age": 34}).get_json()
    portrait = client.get(f"/api/avatars/{made['id']}/portrait")
    assert portrait.status_code == 200 and portrait.mimetype == "image/svg+xml"
    assert ">M</text>" in portrait.get_data(as_text=True)
    assert client.get(f"/api/companions/{made['id']}").status_code == 200


# ---------------------------------------------------------------- a picture

def test_an_uploaded_portrait_is_age_checked_and_can_be_removed(client, world):
    made = client.post("/api/companions", json={"name": "Mira", "age": 34}).get_json()
    avatar_id = made["id"]

    def upload(data, name="portrait.png"):
        return client.post(f"/api/avatars/{avatar_id}/portrait",
                           data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")

    assert upload(b"not a png").status_code == 422
    assert upload(png()).status_code == 200
    assert upload(png()).status_code == 200                       # a second picture replaces the first
    picture = client.get(f"/api/avatars/{avatar_id}/portrait")
    assert picture.status_code == 200 and picture.mimetype == "image/png"
    picture.close()
    record = world.avatars.get(avatar_id)
    assert record["portrait_from"] == "upload" and record["age_check"]["ok"] is True
    assert portraits.summary(world.avatars, record)["from"] == "upload"

    services = companion.get_services()
    services.checker = FakeChecker(ok=False)
    refused = upload(png())
    assert refused.status_code == 422 and "younger" in refused.get_json()["error"]
    assert portraits.summary(world.avatars, world.avatars.get(avatar_id))["from"] == "upload"
    services.checker = FakeChecker(ok=True)

    removed = client.delete(f"/api/avatars/{avatar_id}/portrait")
    assert removed.status_code == 200 and removed.get_json()["from"] == "none"
    back = client.get(f"/api/avatars/{avatar_id}/portrait")
    assert back.status_code == 200 and back.mimetype == "image/svg+xml"
    assert client.delete(f"/api/avatars/{avatar_id}/portrait").status_code == 409


def test_only_a_written_companion_can_have_a_picture_uploaded(client, world):
    mara = active_avatar(world)
    answer = client.post(f"/api/avatars/{mara['id']}/portrait",
                         data={"file": (io.BytesIO(png()), "portrait.png")}, content_type="multipart/form-data")
    assert answer.status_code == 422 and "written" in answer.get_json()["error"]


# ---------------------------------------------------------------- a portrait from NPC Forge

def test_npc_forge_can_paint_a_portrait_for_a_written_companion(world):
    made = a_companion(world, bio="A cartographer with short red hair.")
    summary = painted(world, made["id"])["portrait"]
    assert summary["from"] == "forge" and summary["can_bring_to_life"] is True
    record = world.avatars.get(made["id"])
    assert record["portrait_from"] == "forge" and record["age_check"]["ok"] is True
    assert world.avatars.image_path(made["id"], "portrait").is_file()


def test_a_written_companion_has_no_card_picture_to_redraw(world):
    made = a_companion(world)
    with pytest.raises(portraits.PortraitError):
        portraits.start(world.avatars, made["id"], FakeForge(), source="card")


def test_a_forge_companion_does_not_get_the_written_portrait_dialog(world):
    mara = active_avatar(world)
    with pytest.raises(portraits.PortraitError):
        portraits._person(world.avatars, mara["id"])
    assert avatars.written_here(mara) is False
