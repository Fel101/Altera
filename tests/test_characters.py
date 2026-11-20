"""Characters from imported character cards (PNG with a chara/ccv3 text chunk, or .json). The model, the age check and NPC
Forge are fakes; nothing here touches the real data folder."""
import base64
import io
import json
import struct
import zlib

import pytest

from core import avatars, characters, charcard, companion, companions
from test_blueprints import create_app
from test_companion_api import active_avatar, engine, tmp_store, world  # noqa: F401
from test_companion_images import FakeChecker


def chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def png(*extra, width=2, height=3) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b"".join(b"\0" + b"\x10\x20\x30\xff" * width for _ in range(height))
    return (charcard.PNG_SIGNATURE + chunk(b"IHDR", ihdr) + b"".join(extra) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def b64(card) -> bytes:
    return base64.b64encode(json.dumps(card).encode("utf-8"))


V2 = {"spec": "chara_card_v2", "spec_version": "2.0", "data": {
    "name": "Captain Vale", "description": "{{char}} commands the airship Kestrel.", "personality": "gruff, loyal, dry humour",
    "scenario": "{{user}} has just signed on as navigator.", "first_mes": "*{{char}} looks up.* You're {{user}}, then?",
    "mes_example": "<START>\n{{user}}: Where are we headed?\n{{char}}: North, past the storm wall.\n<START>\n{{user}}: Any rules?\n{{char}}: Don't touch my maps.",
    "creator_notes": "SECRET CREATOR NOTE: best with a 13B model.", "system_prompt": "Write vivid prose. {{original}}",
    "post_history_instructions": "Stay terse, at most three sentences.", "alternate_greetings": ["Welcome aboard, {{user}}."],
    "tags": ["airship", "adventure"], "creator": "someone", "character_version": "1.2", "character_book": {"entries": []},
}}


def text_chunk(keyword: str, card=V2) -> bytes:
    return chunk(b"tEXt", keyword.encode("latin-1") + b"\0" + b64(card))


# ---------------------------------------------------------------- reading cards

def test_a_v2_png_card_is_read_and_its_text_is_stripped_from_the_image():
    card, clean = charcard.parse_png(png(text_chunk("chara"), chunk(b"tEXt", b"Software\0Some editor")))
    assert card["name"] == "Captain Vale" and card["spec"] == "v2" and card["alternate_greetings"] == ["Welcome aboard, {{user}}."]
    assert card["tags"] == ["airship", "adventure"] and "character_book" not in card
    assert b"tEXt" not in clean and clean.startswith(charcard.PNG_SIGNATURE) and clean.endswith(chunk(b"IEND", b""))
    assert b"IDAT" in clean and b"IHDR" in clean


@pytest.mark.parametrize("make", [
    lambda: text_chunk("tavernCardV2"),
    lambda: chunk(b"zTXt", b"chara\0\0" + zlib.compress(b64(V2))),
    lambda: chunk(b"iTXt", b"chara\0\1\0en\0\0" + zlib.compress(b64(V2))),
    lambda: chunk(b"iTXt", b"chara\0\0\0\0\0" + b64(V2)),
    lambda: chunk(b"tEXt", b"chara\0" + json.dumps(V2).encode()),          # the JSON itself, not base64
])
def test_every_text_chunk_form_and_keyword_is_read(make):
    assert charcard.parse_png(png(make()))[0]["name"] == "Captain Vale"


def test_a_v3_card_is_preferred_to_the_older_copy_in_the_same_file():
    v3 = {"spec": "chara_card_v3", "data": {**V2["data"], "name": "Captain Vale III"}}
    card, _ = charcard.parse_png(png(text_chunk("chara"), text_chunk("ccv3", v3)))
    assert card["name"] == "Captain Vale III" and card["spec"] == "v3"


@pytest.mark.parametrize("data, message", [
    (b"GIF89a not a png", "not a PNG"),
    (png(), "no character card"),
    (png(text_chunk("chara"))[:-6], "cut short"),
    (png(text_chunk("chara"), width=9000), "at most 8192"),
    (png(text_chunk("chara", {"spec": "chara_card_v2", "data": {"name": "", "description": "x"}})), "no name"),
    (png(text_chunk("chara", {"spec": "chara_card_v2", "data": {"name": "Empty"}})), "no description"),
])
def test_files_that_are_not_usable_cards_are_refused_plainly(data, message):
    with pytest.raises(charcard.CardError, match=message):
        charcard.parse_png(data)


def test_a_damaged_chunk_is_refused():
    data = bytearray(png(text_chunk("chara")))
    data[40] ^= 0xFF                                                       # inside the first text chunk
    with pytest.raises(charcard.CardError, match="damaged"):
        charcard.parse_png(bytes(data))


def test_json_cards_in_v1_and_v2_shape_are_read():
    v1 = {"name": "Old Timer", "description": "A V1 card.", "first_mes": "Howdy.", "mes_example": ""}
    assert charcard.parse_json(json.dumps(v1).encode())["first_mes"] == "Howdy."
    assert charcard.parse_json(json.dumps(V2).encode("utf-8-sig"))["name"] == "Captain Vale"
    with pytest.raises(charcard.CardError, match="not valid JSON"):
        charcard.parse_json(b"{nope")


# ---------------------------------------------------------------- importing

def test_importing_a_png_card_keeps_the_original_and_shows_the_image(world):
    data = png(text_chunk("chara"))
    checker = FakeChecker(ok=True)
    made = characters.import_card(world.avatars, "vale.png", data, checker=checker)
    assert made["kind"] == "character" and made["name"] == "Captain Vale" and made["id"].startswith("chr-captain-vale-")
    assert (characters.originals_dir(world.avatars) / f"{made['id']}.png").read_bytes() == data      # exactly as it came
    portrait = (world.avatars.root / made["id"] / "portrait.png").read_bytes()
    assert b"tEXt" not in portrait and checker.calls == 1
    record = world.avatars.get(made["id"])
    assert record["creator_notes"].startswith("SECRET") and "creator_notes" not in record["card"]
    assert record["card_instructions"] is True and avatars.features_of(record) == dict.fromkeys(avatars.FEATURES, True)
    assert world.avatars.active_id() == made["id"]


def test_a_json_card_has_no_picture_and_gets_its_initial(world):
    made = characters.import_card(world.avatars, "vale.json", json.dumps(V2).encode())
    assert not (world.avatars.root / made["id"] / "portrait.png").exists()
    client = create_app("companion").test_client()
    response = client.get(f"/api/avatars/{made['id']}/portrait")
    assert response.mimetype == "image/svg+xml" and ">C</text>" in response.get_data(as_text=True)


def test_a_card_that_breaks_the_hard_rules_or_looks_underage_is_not_imported(world, monkeypatch):
    with pytest.raises(characters.CharacterError, match="younger than 18"):
        characters.import_card(world.avatars, "vale.png", png(text_chunk("chara")), checker=FakeChecker(ok=False, age=15))
    monkeypatch.setattr(characters.safety, "hard_problem", lambda text: "It mentions a minor." if "storm wall" in text else None)
    with pytest.raises(characters.CharacterError, match="cannot be imported"):
        characters.import_card(world.avatars, "vale.png", png(text_chunk("chara")))
    assert world.avatars.list() == [] and not list(characters.originals_dir(world.avatars).glob("*"))
    with pytest.raises(characters.CharacterError, match="Choose a character card"):
        characters.import_card(world.avatars, "vale.txt", b"hello")


def test_removing_a_character_removes_its_original_card_too(world):
    made = characters.import_card(world.avatars, "vale.png", png(text_chunk("chara")))

    class Memory:
        store = type("S", (), {"get_all": staticmethod(lambda avatar_id=None: []), "collection": None})()

    companions.remove(made["id"], memory_manager=Memory())
    assert not world.avatars.get(made["id"]) and not list(characters.originals_dir(world.avatars).glob("*"))


def test_characters_never_send_pictures(world):
    made = characters.import_card(world.avatars, "vale.png", png(text_chunk("chara")))
    assert world.avatars.image_path(made["id"], "portrait") and companion.photos_ready(made["id"]) is False
    assert companion.photos_ready(active_avatar(world)["id"]) is True


# ---------------------------------------------------------------- the card in the conversation

@pytest.fixture
def vale(world, monkeypatch):
    monkeypatch.setattr(characters, "user_name", lambda: "Rook")
    made = characters.import_card(world.avatars, "vale.png", png(text_chunk("chara")))
    return made, companions.open_companion(made["id"])["session_id"]


def test_the_card_becomes_the_persona_with_names_filled_in_and_no_creator_notes(world, engine, vale):
    made, sid = vale
    ctx = engine.build_context(sid)
    prompt = ctx.system_prompt
    assert prompt.startswith("Write vivid prose.") and "{{original}}" not in prompt
    assert "You are Captain Vale. Stay in character" in prompt and "talking with is Rook" in prompt
    assert "Captain Vale commands the airship Kestrel." in prompt and "Rook has just signed on as navigator." in prompt
    assert "gruff, loyal, dry humour" in prompt and "Rook: Where are we headed?" in prompt
    assert "SECRET CREATOR NOTE" not in prompt and "{{char}}" not in prompt and "{{user}}" not in prompt
    assert ctx.newborn is False and "Your First Days" not in prompt
    characters.update(world.avatars, made["id"], card_instructions=False)
    assert "Write vivid prose" not in engine.build_context(sid).system_prompt


def test_example_dialogue_is_cut_to_the_token_budget():
    card = V2["data"]
    full = characters.examples(card, "Vale", "Rook", 600)
    assert "Where are we headed?" in full and "Don't touch my maps." in full
    first_only = characters.examples(card, "Vale", "Rook", 25)
    assert "Where are we headed?" in first_only and "maps" not in first_only
    assert characters.examples(card, "Vale", "Rook", 0) == "" and characters.example_budget({"characters": {"example_tokens": "x"}}) == 600


def test_the_first_message_is_the_cards_own_words_without_a_model_call(world, engine, vale):
    made, sid = vale
    text = engine.send_message("", sid, greet=True)
    assert text == "*Captain Vale looks up.* You're Rook, then?"
    assert engine.llm.calls == []
    assert [m.content for m in world.store.get_messages(sid)] == [text]
    from core.chat import ChatEngine
    assert ChatEngine._character_greeting(world.avatars.get(made["id"]), regenerating=True) in {
        text, "Welcome aboard, Rook."}


def test_the_streamed_first_message_never_reaches_or_probes_the_model(world, engine, vale, monkeypatch):
    from core.chat import ChatEngine
    from test_companion_api import drain
    made, sid = vale

    def no_probe(self):
        raise AssertionError("a fixed reply must not probe the model for tool support")
    monkeypatch.setattr(ChatEngine, "_tools_for_this_backend", no_probe)
    text, _ = drain(engine.send_message("", sid, stream=True, greet=True))
    assert text == "*Captain Vale looks up.* You're Rook, then?"
    assert engine.llm.calls == []
    assert [m.content for m in world.store.get_messages(sid)] == [text]


def test_a_characters_summary_keeps_the_prose_personality_out_of_the_trait_chips(world, vale):
    made, _ = vale
    summary = world.avatars.public(world.avatars.get(made["id"]))
    assert summary["traits"] == [] and summary["kind"] == "character"


def test_post_history_instructions_follow_the_newest_message_until_switched_off(world, engine, vale):
    made, sid = vale
    engine.send_message("Morning, Captain.", sid)
    last = engine.llm.calls[-1][-1]
    assert last["role"] == "user" and last["content"] == "Morning, Captain.\n\n[Stay terse, at most three sentences.]"
    assert "[Stay terse" not in world.store.get_messages(sid)[0].content            # never saved
    characters.update(world.avatars, made["id"], card_instructions=False)
    engine.send_message("Still there?", sid)
    assert engine.llm.calls[-1][-1]["content"] == "Still there?"


def test_a_characters_switches_can_turn_memory_and_the_rest_off(world, vale):
    made, _ = vale
    characters.update(world.avatars, made["id"], features={"memory": False, "presence": False})
    keeps = avatars.features_of(world.avatars.get(made["id"]))
    assert keeps == {"memory": False, "notes": True, "story": True, "presence": False}
    with pytest.raises(characters.CharacterError):
        characters.update(world.avatars, made["id"], features={"memory": 1})


# ---------------------------------------------------------------- the routes

@pytest.fixture
def client(world):
    return create_app("companions", "companion").test_client()


def upload(client, data, name="vale.png", headers=None):
    return client.post("/api/characters/import", data={"file": (io.BytesIO(data), name)}, headers=headers or {},
                       content_type="multipart/form-data")


def test_the_character_routes_import_open_and_change_a_character(client, world):
    response = upload(client, png(text_chunk("chara")))
    assert response.status_code == 201
    opened = response.get_json()
    assert opened["kind"] == "character" and opened["session_id"] and world.avatars.active_id() == opened["id"]
    current = client.get("/api/characters/" + opened["id"]).get_json()
    assert current["card_instructions"] is True and current["has_instructions"] is True
    assert current["creator_notes"].startswith("SECRET") and current["creator"] == "someone"
    changed = client.put("/api/characters/" + opened["id"], json={"card_instructions": False, "voice": "af_bella"}).get_json()
    assert changed["card_instructions"] is False and changed["voice"] == "af_bella"
    assert client.put("/api/characters/" + opened["id"], json={"name": "X"}).status_code == 400
    assert client.get("/api/pals/" + opened["id"]).status_code == 404                # a character is not a pal
    assert upload(client, png()).status_code == 400
    assert client.post("/api/characters/import", data={}, content_type="multipart/form-data").status_code == 400


def test_the_character_routes_refuse_other_websites(client, world):
    cross = {"Sec-Fetch-Site": "cross-site"}
    assert upload(client, png(text_chunk("chara")), headers=cross).status_code == 403
    assert world.avatars.list() == []
    made = characters.import_card(world.avatars, "vale.png", png(text_chunk("chara")))
    assert client.put("/api/characters/" + made["id"], json={"card_instructions": False}, headers=cross).status_code == 403
    assert world.avatars.get(made["id"])["card_instructions"] is True
