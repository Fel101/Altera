"""Pals: light personas from a name, a vibe and a voice. What they keep (memory, notes, story, presence) starts off.
NPC Forge, the model and the memory store are fakes; nothing here touches the real data folder."""
import json

import pytest
import yaml

from core import avatars, companions, pals, tts
from core.presence import PresenceError, _pal_allows
from test_blueprints import create_app
from test_companion_api import FakeMemory, active_avatar, engine, tmp_store, world  # noqa: F401


def make(world, name="Pip", vibe="sunny", voice=""):
    return pals.create(world.avatars, name, vibe, voice, engine="kokoro")


# ---------------------------------------------------------------- vibes and voices

def test_the_shipped_vibes_load_in_picker_order_with_a_persona_each():
    found = pals.vibes()
    assert [v["id"] for v in found] == ["sunny", "chill", "witty", "thoughtful", "geeky"]
    for v in found:
        assert v["label"] and v["description"] and v["traits"] and v["speech_style"]["tone"]


def test_a_broken_or_oddly_named_vibe_file_is_skipped(tmp_path):
    (tmp_path / "good.yaml").write_text("label: Good\ntraits: [kind]\n", "utf-8")
    (tmp_path / "broken.yaml").write_text("label: [unclosed\n", "utf-8")
    (tmp_path / "Bad Name.yaml").write_text("label: Bad\n", "utf-8")
    assert [v["id"] for v in pals.vibes(tmp_path)] == ["good"]


def test_voices_match_the_engine_and_a_foreign_voice_is_ignored_when_speaking():
    assert pals.valid_voice("af_bella", "kokoro") and not pals.valid_voice("en-US-GuyNeural", "kokoro")
    assert pals.valid_voice("en-US-GuyNeural", "edge") and not pals.valid_voice("af_bella", "edge")
    assert pals.valid_voice("", "kokoro")
    cfg = {"engine": "kokoro", "kokoro": {"voice": "af_heart", "url": "x"}, "edge_voice": "en-US-AriaNeural"}
    assert tts.with_person_voice(cfg, "af_bella")["kokoro"] == {"voice": "af_bella", "url": "x"}
    assert tts.with_person_voice(cfg, "en-US-GuyNeural") is cfg and tts.with_person_voice(cfg, None) is cfg
    edge = {**cfg, "engine": "edge"}
    assert tts.with_person_voice(edge, "en-GB-RyanNeural")["edge_voice"] == "en-GB-RyanNeural"


# ---------------------------------------------------------------- making a pal

def test_making_a_pal_writes_its_yaml_and_a_pal_record_with_everything_kept_off(world):
    made = make(world, " Pip  the  Pal ", "witty", "af_bella")
    assert made["kind"] == "pal" and made["name"] == "Pip the Pal" and made["id"].startswith("pal-pip-the-pal-")
    folder = world.avatars.root / made["id"]
    data = yaml.safe_load((folder / "pal.yaml").read_text("utf-8"))
    assert data["name"] == "Pip the Pal" and data["vibe"] == "witty" and data["voice"] == "af_bella"
    assert data["features"] == {"memory": False, "notes": False, "story": False, "presence": False}
    assert data["traits"] == pals.vibe("witty")["traits"]
    record = json.loads((folder / "avatar.json").read_text("utf-8"))
    assert record["kind"] == "pal" and record["source"]["type"] == "pal" and record["voice"] == "af_bella"
    assert record["card"]["kind"] == "pal" and record["card"]["vibe"] == "Witty"
    assert made["features"] == dict.fromkeys(avatars.FEATURES, False)
    assert world.avatars.active_id() == made["id"]                                  # the first person is the active one
    assert not (folder / "portrait.png").exists()


@pytest.mark.parametrize("name, vibe, voice, message", [
    ("", "sunny", "", "Give your pal a name"),
    ("x" * 41, "sunny", "", "up to 40"),
    ("<script>", "sunny", "", "up to 40"),
    ("Pip", "nope", "", "Choose one of the vibes"),
    ("Pip", "sunny", "en-US-GuyNeural", "listed voices"),
])
def test_what_the_person_can_fix_is_refused_with_a_plain_message(world, name, vibe, voice, message):
    with pytest.raises(pals.PalError, match=message):
        make(world, name, vibe, voice)
    assert not world.avatars.list()


def test_a_name_that_breaks_the_hard_rules_is_refused(world, monkeypatch):
    monkeypatch.setattr(pals.safety, "hard_problem", lambda text: "It mentions a minor." if text == "Banned" else None)
    with pytest.raises(pals.PalError, match="cannot be used"):
        make(world, "Banned")


# ---------------------------------------------------------------- editing a pal

def test_hand_edits_to_pal_yaml_are_picked_up_when_the_pal_is_opened(world):
    made = make(world)
    path = world.avatars.root / made["id"] / "pal.yaml"
    data = yaml.safe_load(path.read_text("utf-8"))
    data.update(name="Pippa", traits=["sleepy"], voice="en-US-GuyNeural")      # a voice for another engine is dropped
    path.write_text(yaml.safe_dump(data), "utf-8")
    opened = companions.open_companion(made["id"])
    assert opened["name"] == "Pippa" and opened["traits"] == ["sleepy"]
    assert world.avatars.get(made["id"])["voice"] == ""


def test_a_broken_pal_yaml_leaves_the_pal_as_it_was(world):
    made = make(world)
    (world.avatars.root / made["id"] / "pal.yaml").write_text("name: [broken\n", "utf-8")
    assert pals.refresh(world.avatars, made["id"]) is False
    assert companions.open_companion(made["id"])["name"] == "Pip"


def test_settings_switch_what_a_pal_keeps_and_change_its_name_and_voice(world):
    made = make(world)
    pals.update(world.avatars, made["id"], name="Pipster", voice="am_adam", features={"memory": True})
    current = pals.settings(world.avatars, made["id"])
    assert current["name"] == "Pipster" and current["voice"] == "am_adam"
    assert current["features"] == {"memory": True, "notes": False, "story": False, "presence": False}
    assert avatars.features_of(world.avatars.get(made["id"]))["memory"] is True
    for bad in ({"memory": "yes"}, {"photos": True}, ["memory"]):
        with pytest.raises(pals.PalError):
            pals.update(world.avatars, made["id"], features=bad)
    mara = active_avatar(world)
    with pytest.raises(pals.PalError):
        pals.update(world.avatars, mara["id"], name="Not a pal")                     # companions are not edited here


def test_companions_keep_everything_and_pals_only_what_was_switched_on():
    assert avatars.features_of({"kind": "companion"}) == dict.fromkeys(avatars.FEATURES, True)
    assert avatars.features_of({}) == dict.fromkeys(avatars.FEATURES, True)
    assert avatars.features_of({"kind": "pal"}) == dict.fromkeys(avatars.FEATURES, False)
    assert avatars.features_of({"kind": "pal", "features": {"story": True, "notes": "yes"}})["story"] is True
    assert avatars.features_of({"kind": "pal", "features": {"notes": "yes"}})["notes"] is False


# ---------------------------------------------------------------- the chat with a pal

class RecordingMemory(FakeMemory):
    def __init__(self):
        self.queries = []

    def get_relevant_memories(self, query, limit=5, avatar_id=None):
        self.queries.append(avatar_id)
        return [{"content": "likes tea", "metadata": {"type": "fact"}, "confidence": .9}]


def test_a_pal_chat_uses_no_memory_notes_story_or_first_days_and_says_so(world, engine, monkeypatch):
    import core.memory
    from core import notes as notes_module
    memory = RecordingMemory()
    monkeypatch.setattr(core.memory, "memory_manager", memory, raising=False)
    made = make(world)
    sid = companions.open_companion(made["id"])["session_id"]
    world.store.save_message(sid, "user", "Do you remember what I drink?")
    reads = []
    service = notes_module.get_notes_service()
    monkeypatch.setattr(service.store, "read", lambda avatar_id: reads.append(avatar_id) or {"about_you": "drinks tea"})
    ctx = engine.build_context(sid)
    assert memory.queries == [] and ctx.relevant_memories == [] and reads == [] and ctx.notes == {}
    assert ctx.newborn is False and ctx.episodes == [] and ctx.reflection_notes == {}
    assert "You are Pip, a friendly AI pal" in ctx.system_prompt
    assert "You do not keep memories between conversations" in ctx.system_prompt
    assert "Your First Days" not in ctx.system_prompt

    pals.update(world.avatars, made["id"], features={"memory": True, "notes": True})
    ctx = engine.build_context(sid)
    assert memory.queries == [made["id"]] and reads == [made["id"]] and ctx.notes == {"about_you": "drinks tea"}
    assert "You do not keep memories" not in ctx.system_prompt


def test_a_pal_does_not_read_memories_unless_memory_is_on(world, monkeypatch, altera_config):
    import core.memory
    from core.memory import MemoryWriter
    calls = []

    class Extracting(FakeMemory):
        extractor = type("X", (), {"extract_from_text": staticmethod(lambda text: calls.append(text) or [])})()

        def store_extracted(self, found, **kw):
            return []

    monkeypatch.setattr(core.memory, "memory_manager", Extracting(), raising=False)
    made = make(world)
    sid = companions.open_companion(made["id"])["session_id"]
    world.store.save_message(sid, "user", "I like tea")

    def no_model(messages):
        raise RuntimeError('no chat model in this test')

    writer = MemoryWriter(core.memory.get_memory_manager, lambda: world.store, config_getter=dict,
                          llm=no_model, sync=True)
    writer.run(made["id"])
    assert calls == []
    pals.update(world.avatars, made["id"], features={"memory": True})
    writer.run(made["id"])
    assert calls == ["I like tea"]


def test_a_pal_greets_with_a_hello_instead_of_waking_up(world, engine):
    from core.chat import GREETING_CUE, PAL_GREETING_CUE
    made = make(world)
    sid = companions.open_companion(made["id"])["session_id"]
    engine.send_message("", sid, greet=True)
    assert engine.llm.calls[-1][-1]["content"] == PAL_GREETING_CUE != GREETING_CUE


def test_presence_and_reflection_refuse_a_pal_until_the_feature_is_on(world):
    from core.reflection import ReflectionError
    made = make(world)
    with pytest.raises(PresenceError, match="off for this pal"):
        _pal_allows(made["id"], "presence", PresenceError)
    with pytest.raises(ReflectionError, match="Story is off"):
        _pal_allows(made["id"], "story", ReflectionError)
    pals.update(world.avatars, made["id"], features={"presence": True, "story": True})
    _pal_allows(made["id"], "presence", PresenceError)
    _pal_allows(made["id"], "story", ReflectionError)
    _pal_allows(active_avatar(world)["id"], "presence", PresenceError)             # companions always may
    _pal_allows("", "presence", PresenceError)                                     # and so may the default persona


# ---------------------------------------------------------------- the routes

@pytest.fixture
def client(world):
    return create_app("companions", "companion", "engagement").test_client()


def test_the_pal_routes_offer_make_open_and_change_a_pal(client, world):
    options = client.get("/api/pals/options").get_json()
    assert [v["id"] for v in options["vibes"]][:2] == ["sunny", "chill"] and set(options["vibes"][0]) == {"id", "label", "description"}
    assert options["voices"]["options"] and "default" in options["voices"]
    response = client.post("/api/pals", json={"name": "Nova", "vibe": "geeky", "voice": options["voices"]["options"][0]["id"]})
    assert response.status_code == 201
    opened = response.get_json()
    assert opened["kind"] == "pal" and opened["session_id"] and world.avatars.active_id() == opened["id"]
    assert client.get("/api/pals/" + opened["id"]).get_json()["features"]["memory"] is False
    changed = client.put("/api/pals/" + opened["id"], json={"features": {"notes": True}}).get_json()
    assert changed["features"]["notes"] is True
    assert client.put("/api/pals/" + opened["id"], json={"vibe": "chill"}).status_code == 400
    assert client.post("/api/pals", json={"name": "", "vibe": "geeky"}).status_code == 400
    assert client.get("/api/pals/nobody-here-1").status_code == 404


def test_the_pal_routes_refuse_other_websites(client, world):
    cross = {"Sec-Fetch-Site": "cross-site"}
    assert client.post("/api/pals", json={"name": "Nova", "vibe": "geeky"}, headers=cross).status_code == 403
    made = make(world)
    assert client.put("/api/pals/" + made["id"], json={"name": "X"}, headers=cross).status_code == 403
    assert not world.avatars.list() or [r["id"] for r in world.avatars.list()] == [made["id"]]


def test_a_pal_without_a_portrait_gets_its_initial_and_a_companion_does_not(client, world):
    made = make(world, "zed")
    response = client.get(f"/api/avatars/{made['id']}/portrait")
    assert response.status_code == 200 and response.mimetype == "image/svg+xml"
    assert response.headers["Cache-Control"] == "no-store" and "default-src 'none'" in response.headers["Content-Security-Policy"]
    body = response.get_data(as_text=True)
    assert ">Z</text>" in body and "<script" not in body
    assert ">&lt;</text>" in pals.portrait_svg({"id": "pal-x-000000", "name": "<b>"}).decode()
    mara = active_avatar(world)
    (world.avatars.root / mara["id"] / "portrait.png").unlink()
    assert client.get(f"/api/avatars/{mara['id']}/portrait").status_code == 404


def test_speaking_for_a_pal_chat_uses_the_pals_voice(client, world, monkeypatch):
    from core import voice_layer
    calls = []
    monkeypatch.setattr(voice_layer, "get_tts_audio", lambda text, person_voice=None, **k: calls.append(person_voice) or "/static/audio/tts/x.wav")
    made = make(world, voice="bf_emma")
    sid = companions.open_companion(made["id"])["session_id"]
    assert client.post("/api/voice/tts", json={"text": "Hello", "session_id": sid}).get_json()["audio_url"]
    assert client.post("/api/voice/tts", json={"text": "Hello"}).status_code == 200
    assert calls == ["bf_emma", None]


def test_old_chats_wait_for_a_companion_and_never_go_to_a_pal(world):
    make(world)
    old = world.store.create_session("chat from before companions")
    world.store.save_message(old, "user", "hello from long ago")
    assert companions.migrate_legacy(data_dir=world.tmp) == {"skipped": "no companion yet"}
    assert not world.store.get_session(old).avatar_id
    mara = active_avatar(world)
    result = companions.migrate_legacy(data_dir=world.tmp, assign_memories=lambda owner: 0)
    assert result["owner"] == mara["id"] and world.store.get_session(old).avatar_id == mara["id"]


def test_opening_a_pal_does_not_look_for_studio_pictures(world):
    made = make(world)
    assert world.photos.sync_premade(made["id"]) == {"imported": 0, "updated": 0, "removed": 0, "refused": 0, "waiting": 0, "problems": []}


# ---------------------------------------------------------------- who a pal is, changed in its settings

def test_a_pals_persona_can_be_changed_in_its_settings_and_the_chat_uses_it(world, engine):
    made = make(world)
    pals.update(world.avatars, made["id"], persona={
        "about": "  A night owl who loves  astronomy. ", "traits": ["curious", " ", "calm"],
        "speech_style": {"tone": "soft", "emojis": False}, "interests": ["stars, planets and comets"], "quirks": []})
    current = pals.settings(world.avatars, made["id"])
    assert current["about"] == "A night owl who loves astronomy."
    assert current["traits"] == ["curious", "calm"] and current["quirks"] == []
    assert current["speech_style"]["tone"] == "soft" and current["speech_style"]["emojis"] is False
    assert current["speech_style"]["formality"]                                       # what was not sent stays
    saved = yaml.safe_load((world.avatars.root / made["id"] / "pal.yaml").read_text("utf-8"))
    assert saved["traits"] == ["curious", "calm"] and saved["interests"] == ["stars, planets and comets"]
    sid = companions.open_companion(made["id"])["session_id"]
    prompt = engine.build_context(sid).system_prompt
    assert "curious" in prompt and "stars, planets and comets" in prompt and "cheerful" not in prompt


@pytest.mark.parametrize("persona, message", [
    ({"traits": [f"t{n}" for n in range(9)]}, "Give up to 8 traits."),
    ({"quirks": ["x" * 141]}, "Keep each of the quirks under 140 characters."),
    ({"traits": "curious, calm"}, "Send the traits as a list of short phrases."),
    ({"about": "x" * 401}, "Keep About under 400 characters."),
    ({"speech_style": {"tone": 5}}, "The tone must be text."),
    ({"speech_style": {"emojis": "yes"}}, "emojis must be true or false."),
    ({"speech_style": {"accent": "posh"}}, "speech_style may hold tone, formality, humor and emojis only."),
    ({"vibe": "chill"}, "persona may hold about, traits, speech_style, interests and quirks only."),
])
def test_a_persona_that_breaks_a_limit_is_refused_whole_and_nothing_is_cut_short(world, persona, message):
    made = make(world)
    before = (world.avatars.root / made["id"] / "pal.yaml").read_text("utf-8")
    with pytest.raises(pals.PalError, match=message.replace(".", r"\.")):
        pals.update(world.avatars, made["id"], persona=persona)
    assert (world.avatars.root / made["id"] / "pal.yaml").read_text("utf-8") == before


def test_a_persona_is_held_to_the_same_hard_rules_as_making_a_pal(world):
    made = make(world)
    with pytest.raises(pals.PalError, match="cannot be used"):
        pals.update(world.avatars, made["id"], persona={"traits": ["looks like a schoolgirl"]})
    assert "schoolgirl" not in str(pals.settings(world.avatars, made["id"]))


def test_a_pal_in_kids_mode_keeps_who_it_is_to_child_friendly_topics(world):
    from core import kids
    kids.enable(True)
    made = pals.create(world.avatars, "Pip", "sunny", "", engine="kokoro", kids=True)
    with pytest.raises(pals.PalError, match="kids mode"):
        pals.update(world.avatars, made["id"], persona={"interests": ["poker and gambling"]})
    pals.update(world.avatars, made["id"], persona={"interests": ["dinosaurs", "space"]})
    assert pals.settings(world.avatars, made["id"])["interests"] == ["dinosaurs", "space"]


def test_the_pal_route_changes_who_a_pal_is(client, world):
    made = make(world)
    changed = client.put("/api/pals/" + made["id"], json={"persona": {"traits": ["dry", "kind"]}})
    assert changed.status_code == 200 and changed.get_json()["traits"] == ["dry", "kind"]
    refused = client.put("/api/pals/" + made["id"], json={"persona": {"traits": ["a"] * 9}})
    assert refused.status_code == 400 and refused.get_json()["error"] == "Give up to 8 traits."


def test_a_hand_edit_that_takes_a_kids_pal_off_child_friendly_topics_is_not_taken(world):
    from core import kids
    kids.enable(True)
    made = pals.create(world.avatars, "Pip", "sunny", "", engine="kokoro", kids=True)
    path = world.avatars.root / made["id"] / "pal.yaml"
    data = yaml.safe_load(path.read_text("utf-8"))
    data["interests"] = ["casinos"]
    path.write_text(yaml.safe_dump(data), "utf-8")
    assert pals.refresh(world.avatars, made["id"]) is False
    assert "casinos" not in str(world.avatars.get(made["id"])["card"])


def test_every_shipped_vibe_is_fit_for_a_pal_in_kids_mode():
    from core import safety
    for vibe in pals.vibes():
        assert not pals._kids_problem({"kids": True, "name": vibe["label"], "about": vibe["description"], **vibe}), vibe["id"]
        assert not any(safety.kids_topic(text) for text in pals._persona_texts(vibe)), vibe["id"]
