"""A character card's lorebook (character_book): read and checked at import, entries called up into the system prompt by the
keys in the last few messages, within a token budget, and switched on or off per character. The model is never called."""
import json

import pytest

from core import characters, charcard, companions
from test_blueprints import create_app
from test_characters import V2, png, text_chunk
from test_companion_api import engine, tmp_store, world  # noqa: F401

BOOK = {"name": "Kestrel lore", "scan_depth": 3, "token_budget": 400, "entries": [
    {"keys": ["Kestrel"], "content": "The Kestrel is {{char}}'s airship: three masts and a cracked hull.", "enabled": True,
     "insertion_order": 20, "name": "The ship"},
    {"keys": ["storm wall"], "content": "The storm wall is a ring of lightning around the north.", "enabled": True,
     "insertion_order": 10},
    {"keys": ["Mira"], "secondary_keys": ["sister"], "selective": True, "enabled": True, "insertion_order": 30,
     "content": "Mira is Vale's sister, lost in the storm."},
    {"keys": [], "constant": True, "enabled": True, "insertion_order": 99, "content": "Airships run on sky-coal.",
     "comment": "World"},
    {"keys": ["gold"], "content": "There is no gold aboard.", "enabled": False, "insertion_order": 1},
    {"keys": ["/drag(on|oons)/i"], "content": "Dragons nest in the storm.", "enabled": True, "insertion_order": 2},
    {"keys": ["CAPTAIN"], "case_sensitive": True, "content": "Only the crew shout CAPTAIN.", "enabled": True,
     "insertion_order": 3},
    {"keys": ["empty"], "content": "   ", "enabled": True},
]}


def card(book=BOOK):
    return {**V2, "data": {**V2["data"], "character_book": book}}


@pytest.fixture
def vale(world, monkeypatch):
    monkeypatch.setattr(characters, "user_name", lambda: "Rook")
    made = characters.import_card(world.avatars, "vale.png", png(text_chunk("chara", card())))
    return made, companions.open_companion(made["id"])["session_id"]


def prompt_after(engine, sid, *messages):
    for role, text in messages:
        engine._save_message(sid, role, text)
    return engine.build_context(sid).system_prompt


# ---------------------------------------------------------------- reading the book

def test_a_book_is_read_into_bounded_entries_and_empty_ones_are_dropped():
    book = charcard.normalize(card())["book"]
    assert book["name"] == "Kestrel lore" and book["scan_depth"] == 3 and book["token_budget"] == 400
    assert len(book["entries"]) == 7                                                          # the blank entry is gone
    ship, wall, mira, world_entry, gold, dragons, shout = book["entries"]
    assert ship == {"name": "The ship", "keys": ["Kestrel"], "secondary_keys": [], "enabled": True, "constant": False,
                    "selective": False, "case_sensitive": False, "regex": False, "order": 20, "position": "before_char",
                    "content": "The Kestrel is {{char}}'s airship: three masts and a cracked hull."}
    assert mira["selective"] and mira["secondary_keys"] == ["sister"] and world_entry["name"] == "World"
    assert world_entry["constant"] and gold["enabled"] is False and dragons["regex"] is True and shout["case_sensitive"]
    assert charcard.normalize(V2)["book"] is None                                             # {"entries": []}
    odd = charcard.read_book({"entries": [{"keys": "not a list", "content": "x", "insertion_order": "high",
                                           "position": "top", "enabled": "yes"}], "scan_depth": -4})
    assert odd["entries"][0]["keys"] == [] and odd["entries"][0]["order"] == 100 and odd["scan_depth"] == 0
    assert odd["entries"][0]["position"] == "before_char" and odd["entries"][0]["enabled"] is True
    assert charcard.read_book({"entries": [{"content": "x", "use_regex": True}]})["entries"][0]["regex"] is True


def test_importing_keeps_the_book_and_a_book_that_breaks_the_hard_rules_is_refused(world):
    made = characters.import_card(world.avatars, "vale.png", png(text_chunk("chara", card())))
    record = world.avatars.get(made["id"])
    assert len(record["book"]["entries"]) == 7 and record["lorebook"] is True
    bad = {"entries": [{"keys": ["school"], "content": "Vale is a 14 year old girl.", "enabled": True}]}
    with pytest.raises(characters.CharacterError, match="cannot be imported"):
        characters.import_card(world.avatars, "bad.png", png(text_chunk("chara", card(bad))))


# ---------------------------------------------------------------- calling entries up

def entries(texts, budget=500, book=None):
    return [e["content"][:12] for e in characters.triggered(book or charcard.normalize(card())["book"], texts, budget)]


def test_an_entry_comes_in_when_a_key_is_mentioned_in_the_cards_order_with_always_on_entries():
    assert entries(["Let's fly the kestrel past the Storm Wall!"]) == ["The storm wa", "The Kestrel ", "Airships run"]
    assert entries(["Nothing to see."]) == ["Airships run"]                                   # always on
    assert entries(["Kestrels are birds."]) == ["Airships run"]                               # whole words only
    assert entries(["the kestrel's hull"])[0] == "The Kestrel "
    assert entries(["Mira!"]) == ["Airships run"]                                             # selective: needs 'sister' too
    assert entries(["Mira, your sister?"]) == ["Mira is Vale", "Airships run"]
    assert entries(["Captain, the gold!"]) == ["Airships run"]                                # case-sensitive; one switched off
    assert entries(["CAPTAIN!"]) == ["Only the cre", "Airships run"]
    assert entries(["dragons"]) == ["Airships run"]                                           # regex keys are not run


def test_the_token_budget_keeps_always_on_entries_first_then_the_cards_order():
    book = charcard.normalize(card())["book"]
    every = "kestrel storm wall Mira sister"
    # costs: always-on 8, storm wall 18, Kestrel 21, Mira 13
    assert entries([every], budget=8, book=book) == ["Airships run"]
    assert entries([every], budget=26, book=book) == ["The storm wa", "Airships run"]
    assert entries([every], budget=22, book=book) == ["Mira is Vale", "Airships run"]       # a smaller one still fits
    assert entries([every], budget=0, book=book) == []


def test_the_chat_brings_entries_into_the_system_prompt_only_while_they_are_mentioned(world, engine, vale):
    made, sid = vale
    prompt = prompt_after(engine, sid, ("user", "Tell me about the Kestrel."))
    assert "## What You Know (from the lorebook" in prompt
    assert "The Kestrel is Captain Vale's airship" in prompt and "{{char}}" not in prompt
    assert prompt.index("## Who You Are") < prompt.index("## What You Know")                  # right after who they are
    assert "storm wall is a ring" not in prompt
    prompt = prompt_after(engine, sid, ("assistant", "Aye."), ("user", "And the storm wall?"), ("assistant", "Later."),
                          ("user", "Hm."))
    assert "storm wall is a ring" in prompt and "The Kestrel is" not in prompt               # past the book's scan depth (3)
    characters.update(world.avatars, made["id"], lorebook=False)
    assert "## What You Know" not in prompt_after(engine, sid, ("user", "The Kestrel!"))


def test_a_character_imported_before_lorebooks_reads_its_book_from_the_saved_card_once(world, engine, vale):
    made, sid = vale
    path = world.avatars.root / made["id"] / "avatar.json"
    record = json.loads(path.read_text("utf-8"))
    record.pop("book")
    record.pop("lorebook")
    path.write_text(json.dumps(record), "utf-8")
    assert "The Kestrel is" in prompt_after(engine, sid, ("user", "The Kestrel!"))
    assert len(world.avatars.get(made["id"])["book"]["entries"]) == 7                        # saved now
    assert characters.settings(world.avatars, made["id"])["lorebook"] is True


def test_the_settings_list_the_entries_and_switch_the_book(world, vale):
    made, _ = vale
    shown = characters.settings(world.avatars, made["id"])
    assert shown["book"]["name"] == "Kestrel lore" and len(shown["book"]["entries"]) == 7
    assert shown["book"]["entries"][0] == {"name": "The ship", "keys": ["Kestrel"], "constant": False, "enabled": True,
                                           "regex": False}
    assert shown["book"]["entries"][1]["name"] == "storm wall"                                # named by its keys
    assert shown["book"]["entries"][3]["name"] == "World" and shown["book"]["entries"][3]["constant"]
    assert "content" not in shown["book"]["entries"][0]                                       # what it says stays out
    client = create_app("companions", "companion").test_client()
    assert client.put(f"/api/characters/{made['id']}", json={"lorebook": "no"}).status_code == 400
    assert client.put(f"/api/characters/{made['id']}", json={"lorebook": False}).get_json()["lorebook"] is False
    plain = characters.import_card(world.avatars, "plain.json", json.dumps(V2).encode())
    assert characters.settings(world.avatars, plain["id"])["book"] == {"name": "", "entries": []}
