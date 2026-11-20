"""A pal, a character or a written companion can take a portrait from a character already in NPC Forge's library, or have a
picture uploaded. Both go through the age check; a character's card picture is kept to go back to. NPC Forge is a folder."""
import pytest

from core import pals, portraits
from test_blueprints import create_app
from test_characters import png
from test_companion_api import tmp_store, world  # noqa: F401
from test_companion_images import PNG, FakeChecker, make_forge
from test_portraits import a_character

MARA = "mara-bell-47ab97"                 # in the library the world fixture makes


def a_pal(world):
    return pals.create(world.avatars, "Pip", "sunny", "", engine="kokoro")


@pytest.fixture
def client(world):
    return create_app("companions", "companion").test_client()


# ---------------------------------------------------------------- from NPC Forge's library

def test_a_pal_takes_a_library_portrait_age_checked_and_can_be_brought_to_life(world):
    pal = a_pal(world)
    shown = portraits.from_library(world.avatars, pal["id"], MARA, FakeChecker(ok=True))
    assert shown["from"] == "forge" and shown["can_bring_to_life"] and shown["age_checked"] == "passed"
    assert world.avatars.image_path(pal["id"], "portrait").read_bytes() == PNG + b"portrait"
    record = world.avatars.get(pal["id"])
    assert record["forge"]["id"] == MARA and record["forge"]["from"] == "library" and record["forge"]["age"] == 34
    assert (world.forge / MARA / "portrait.png").is_file()                     # the library is only read


def test_a_library_portrait_that_reads_as_under_age_is_refused_and_nothing_changes(world):
    pal = a_pal(world)
    with pytest.raises(portraits.PortraitError, match="younger than"):
        portraits.from_library(world.avatars, pal["id"], MARA, FakeChecker(ok=False, age=15))
    assert world.avatars.image_path(pal["id"], "portrait") is None
    assert portraits.summary(world.avatars, world.avatars.get(pal["id"]))["from"] == "none"


@pytest.mark.parametrize("make, npc, words", [
    (None, "nobody-here-000000", "not found"),
    (dict(npc_id="lee-bell-111111", portrait=False), "lee-bell-111111", "no portrait yet"),
    (dict(npc_id="kit-bell-222222", age=16), "kit-bell-222222", r"this one is 16"),
    (None, "../escape", "not found"),
])
def test_only_an_eligible_library_character_lends_a_portrait(world, make, npc, words):
    if make:
        make_forge(world.forge, **make)
    pal = a_pal(world)
    with pytest.raises(portraits.PortraitError, match=words):
        portraits.from_library(world.avatars, pal["id"], npc, FakeChecker(ok=True))
    assert world.avatars.image_path(pal["id"], "portrait") is None


def test_a_character_s_card_picture_comes_back_after_a_library_portrait(world):
    made = a_character(world)
    card = world.avatars.image_path(made["id"], "portrait").read_bytes()
    portraits.from_library(world.avatars, made["id"], MARA, FakeChecker(ok=True))
    assert portraits.stop_using(world.avatars, made["id"])["from"] == "card"
    assert world.avatars.image_path(made["id"], "portrait").read_bytes() == card


def test_the_library_route_checks_its_body_and_the_origin(client, world):
    pal = a_pal(world)
    url = f"/api/portraits/{pal['id']}/library"
    assert client.post(url, json={"npc_id": MARA, "extra": 1}).status_code == 400
    assert client.post(url, json={"npc_id": 5}).status_code == 400
    cross = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    assert client.post(url, json={"npc_id": MARA}, headers=cross).status_code == 403
    answer = client.post(url, json={"npc_id": MARA})
    assert answer.status_code == 200 and answer.get_json()["from"] == "forge"


# ---------------------------------------------------------------- an uploaded picture

def upload(client, avatar_id, data):
    import io
    return client.post(f"/api/avatars/{avatar_id}/portrait", data={"file": (io.BytesIO(data), "me.png")},
                       content_type="multipart/form-data")


def test_a_pal_can_have_a_picture_uploaded_and_removed(client, world):
    pal = a_pal(world)
    assert client.get(f"/api/portraits/{pal['id']}").get_json()["can_upload"] is True
    picture = png(width=4, height=4)
    assert upload(client, pal["id"], picture).status_code == 200
    shown = client.get(f"/api/portraits/{pal['id']}").get_json()
    assert shown["from"] == "upload" and shown["age_checked"] == "passed"
    assert client.get(f"/api/avatars/{pal['id']}/portrait").data == picture
    assert client.delete(f"/api/avatars/{pal['id']}/portrait").get_json()["from"] == "none"


def test_an_upload_that_reads_as_under_age_is_refused(client, world):
    world.checker.result = {"ok": False, "apparent_age": 14, "model": "fake", "error": None}
    pal = a_pal(world)
    answer = upload(client, pal["id"], png(width=4, height=4))
    assert answer.status_code == 422 and "younger than" in answer.get_json()["error"]
    assert world.avatars.image_path(pal["id"], "portrait") is None


def test_a_character_s_upload_keeps_the_card_picture_to_go_back_to(client, world):
    made = a_character(world)
    card = world.avatars.image_path(made["id"], "portrait").read_bytes()
    picture = png(width=5, height=5)
    assert upload(client, made["id"], picture).status_code == 200
    shown = client.get(f"/api/portraits/{made['id']}").get_json()
    assert shown["from"] == "upload" and shown["card_picture"] is True
    back = client.delete(f"/api/avatars/{made['id']}/portrait").get_json()
    assert back["from"] == "card"
    assert world.avatars.image_path(made["id"], "portrait").read_bytes() == card


def test_a_companion_from_npc_forge_still_cannot_have_one_uploaded(client, world):
    world.avatars.import_from_forge(MARA)
    assert upload(client, MARA, png(width=4, height=4)).status_code == 422
