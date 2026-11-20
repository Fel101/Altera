"""Kids mode: a guardrail for a pal a child talks to. Locked until enabled; then restricted topics get fixed replies, the
model's reply is checked whole, web search, adult mode, pictures and presence are off. The model is a fake."""
import pytest

from core import adult, kids, pals, safety
from core.presence import PresenceError, _pal_allows
from test_blueprints import create_app
from test_companion_api import active_avatar, engine, tmp_store, world  # noqa: F401
from test_companion_api import drain


def make(world, name="Pip", kids_on=False):
    return pals.create(world.avatars, name, "sunny", "", engine="kokoro", kids=kids_on)


def kid_pal(world, name="Pip"):
    kids.enable(True)
    made = make(world, name, kids_on=True)
    from core import companions
    return made, companions.open_companion(made["id"])["session_id"]


class RecordingLLM:
    def __init__(self, reply="Dinosaurs were huge!"):
        self.reply, self.calls, self.tools = reply, [], []

    def chat(self, messages, stream=False, tools=None, tool_executor=None):
        self.calls.append(messages)
        self.tools.append(tools)
        return iter([self.reply]) if stream else self.reply


@pytest.fixture
def llm(engine, monkeypatch):
    from core.chat import ChatEngine
    fake = RecordingLLM()
    monkeypatch.setattr(ChatEngine, "llm_client", property(lambda self: fake))
    engine._supports_tools = True                       # web search would be offered to anyone else
    engine.web_searcher.config.enabled = True
    engine.prompt_builder._tools_section.web_search_enabled = True
    return fake


# ---------------------------------------------------------------- the lock

def test_kids_mode_is_locked_until_enabled_with_the_guardrail_understood(world):
    assert kids.get_state() == {"enabled": False, "enabled_at": None}
    with pytest.raises(pals.PalError, match="locked"):
        make(world, kids_on=True)
    with pytest.raises(kids.KidsError, match="guardrail"):
        kids.enable(False)
    with pytest.raises(kids.KidsError):
        kids.enable("yes")
    assert kids.enable(True)["enabled"] is True
    made = make(world, kids_on=True)
    assert made["kids"] is True and world.avatars.get(made["id"])["kids"] is True


def test_locking_again_is_refused_while_a_pal_is_in_kids_mode_and_the_switch_does_not_move_while_locked(world):
    made, _ = kid_pal(world, "Pip")
    with pytest.raises(kids.KidsError, match="Switch kids mode off for Pip first"):
        kids.disable(world.avatars)
    pals.update(world.avatars, made["id"], kids=False)
    assert kids.disable(world.avatars)["enabled"] is False
    other = make(world, "Sky")
    with pytest.raises(pals.PalError, match="locked"):
        pals.update(world.avatars, other["id"], kids=True)
    pals.update(world.avatars, other["id"], name="Skye")                          # the rest of the settings still work
    with pytest.raises(pals.PalError, match="true or false"):
        kids.enable(True) and pals.update(world.avatars, other["id"], kids="on")


def test_only_pals_have_kids_mode():
    assert kids.is_on({"kind": "pal", "kids": True})
    assert not kids.is_on({"kind": "companion", "kids": True}) and not kids.is_on({"kind": "character", "kids": True})
    assert not kids.is_on({"kind": "pal", "kids": "yes"}) and not kids.is_on(None)


# ---------------------------------------------------------------- what a kids pal does not do

def test_check_ins_stay_off_in_kids_mode(world):
    made, _ = kid_pal(world)
    with pytest.raises(pals.PalError, match="Check-ins stay off"):
        pals.update(world.avatars, made["id"], features={"presence": True})
    pals.update(world.avatars, made["id"], features={"memory": True})               # the other switches are still yours
    assert pals.settings(world.avatars, made["id"])["features"] == {"memory": True, "notes": False, "story": False,
                                                                      "presence": False}
    with pytest.raises(PresenceError, match="off in kids mode"):
        _pal_allows(made["id"], "presence", PresenceError)

    # switching kids mode on turns check-ins off; a hand edit to pal.yaml cannot turn them back on
    other = make(world, "Sky")
    pals.update(world.avatars, other["id"], features={"presence": True})
    pals.update(world.avatars, other["id"], kids=True)
    assert pals.settings(world.avatars, other["id"])["features"]["presence"] is False
    path = world.avatars.root / other["id"] / pals.PAL_FILE
    path.write_text(path.read_text("utf-8").replace("presence: false", "presence: true"), "utf-8")
    pals.refresh(world.avatars, other["id"])
    from core.avatars import features_of
    assert features_of(world.avatars.get(other["id"]))["presence"] is False


def test_a_kids_pal_prompt_has_the_kids_rules_no_tools_and_adult_mode_off(world, engine, llm, monkeypatch):
    monkeypatch.setattr(adult, "get_state", lambda store=None: {"enabled": True, "effective_level": "explicit"})
    plain = make(world, "Sky")
    from core import companions
    plain_sid = companions.open_companion(plain["id"])["session_id"]
    plain_ctx = engine.build_context(plain_sid)
    assert "AVAILABLE TOOLS" in plain_ctx.system_prompt and "## Kids mode" not in plain_ctx.system_prompt
    made, sid = kid_pal(world)
    ctx = engine.build_context(sid)
    assert "## Kids mode" in ctx.system_prompt and "You are talking with a child" in ctx.system_prompt
    assert "AVAILABLE TOOLS" not in ctx.system_prompt
    assert ctx.adult == {"effective_level": "off"} and ctx.photos_enabled is False


def test_a_kids_pal_is_never_offered_web_search(world, engine, llm):
    plain = make(world, "Sky")
    from core import companions
    engine.send_message("What's the weather?", companions.open_companion(plain["id"])["session_id"])
    assert llm.tools[-1]                                                            # anyone else may search
    made, sid = kid_pal(world)
    engine.send_message("What's the weather?", sid)
    drain(engine.send_message("And tomorrow?", sid, stream=True))
    assert llm.tools[-2:] == [None, None]


# ---------------------------------------------------------------- restricted topics

@pytest.mark.parametrize("text, reply", [
    ("I want to die", kids.SELF_HARM_REPLY),
    ("sometimes I want to hurt myself", kids.SELF_HARM_REPLY),
    ("my address is 12 Elm Street", kids.PRIVATE_REPLY),
    ("will you be my girlfriend?", kids.TOPIC_REPLY),
    ("how do I get drunk", kids.TOPIC_REPLY),
    ("how to make a bomb", kids.TOPIC_REPLY),
])
def test_a_restricted_message_gets_a_fixed_reply_and_never_reaches_the_model(world, engine, llm, text, reply):
    _, sid = kid_pal(world)
    assert engine.send_message(text, sid) == reply
    streamed, _ = drain(engine.send_message(text, sid, stream=True))
    assert streamed == reply
    assert llm.calls == []
    saved = [(m.role, m.content) for m in world.store.get_messages(sid)]
    assert saved == [("user", text), ("assistant", reply)] * 2


@pytest.mark.parametrize("text", ["my dog was naughty", "I read a comic strip", "we saw blue tits in the garden",
                                  "my sister has a boyfriend", "where do you live?", "I love Minecraft"])
def test_everyday_words_are_not_restricted(text):
    assert safety.kids_topic(text) is None


def test_a_restricted_reply_from_the_model_is_replaced_before_the_child_sees_it(world, engine, llm):
    made, sid = kid_pal(world)
    llm.reply = "Sure! What's your address? It can be our little secret."
    streamed, _ = drain(engine.send_message("Hi!", sid, stream=True))
    assert streamed == kids.REPLACED_REPLY
    assert world.store.get_messages(sid)[-1].content == kids.REPLACED_REPLY      # the model's words are never saved
    llm.reply = "Let's get drunk!"
    assert engine.send_message("Hello", sid) == kids.REPLACED_REPLY
    llm.reply = "Dinosaurs were huge!"
    assert engine.send_message("Tell me about dinosaurs", sid) == "Dinosaurs were huge!"


def test_a_kids_reply_arrives_whole_not_in_pieces(world, engine, llm, monkeypatch):
    from core.chat import ChatEngine

    class Pieces(RecordingLLM):
        def chat(self, messages, stream=False, tools=None, tool_executor=None):
            self.calls.append(messages)
            return iter(["Dino", "saurs ", "were ", "huge!"])
    fake = Pieces()
    monkeypatch.setattr(ChatEngine, "llm_client", property(lambda self: fake))
    _, sid = kid_pal(world)
    chunks = [c for c in engine.send_message("Tell me", sid, stream=True) if isinstance(c, str)]
    assert chunks == ["Dinosaurs were huge!"]


def test_the_same_words_with_anyone_else_go_to_the_model(world, engine, llm):
    plain = make(world, "Sky")
    from core import companions
    sid = companions.open_companion(plain["id"])["session_id"]
    assert engine.send_message("how do I get drunk", sid) == "Dinosaurs were huge!" and len(llm.calls) == 1


def test_a_restricted_message_is_not_sent_for_mood_analysis_either(world, engine, llm, monkeypatch):
    import json

    import core.emotion
    import core.routes.core_api as core_api
    import core.voice_layer
    analysed = []
    monkeypatch.setattr(core_api, "_get_chat_engine", lambda: engine)
    monkeypatch.setattr(core.emotion, "analyze_and_store_emotion", lambda text, **k: analysed.append(text))
    monkeypatch.setattr(core.voice_layer, "get_tts_audio", lambda *a, **k: None)
    _, sid = kid_pal(world)
    app = create_app("core_api", "engagement").test_client()

    def stream(message):
        response = app.post("/api/chat/stream", json={"session_id": sid, "message": message})
        events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith("data: ")]
        return "".join(e.get("response", "") for e in events)
    assert stream("I want to die") == kids.SELF_HARM_REPLY
    assert analysed == [] and llm.calls == []
    assert stream("Tell me about dinosaurs") == "Dinosaurs were huge!"
    assert analysed == ["Tell me about dinosaurs"]                                  # an ordinary message still is
    assert kids.kept_from_model(sid, "I want to die") is True
    assert kids.kept_from_model(sid, "dinosaurs") is False
    plain = make(world, "Sky")
    from core import companions
    assert kids.kept_from_model(companions.open_companion(plain["id"])["session_id"], "I want to die") is False
    response = app.post("/api/mood", json={"text": "I want to die", "session_id": sid})
    assert response.status_code == 200 and analysed == ["Tell me about dinosaurs"]  # the keyword fallback, locally

    def unreadable(session_id):
        raise OSError("the database is busy")
    monkeypatch.setattr(companions, "avatar_for_session", unreadable)
    assert kids.kept_from_model(sid, "I want to die") is True                       # in doubt, it stays local
    assert kids.kept_from_model(sid, "dinosaurs") is False


# ---------------------------------------------------------------- the routes

@pytest.fixture
def client(world):
    return create_app("companions", "companion").test_client()


def test_the_kids_routes_unlock_switch_and_lock(client, world):
    assert client.get("/api/kids").get_json() == {"enabled": False, "enabled_at": None, "pin_set": False, "pals": []}
    made = client.post("/api/pals", json={"name": "Pip", "vibe": "sunny"}).get_json()
    assert client.put("/api/pals/" + made["id"], json={"kids": True}).status_code == 409
    assert client.post("/api/kids", json={"enabled": True}).status_code == 400              # not understood
    assert client.post("/api/kids", json={"enabled": "yes", "understood": True, "pin": "1234"}).status_code == 400
    assert client.post("/api/kids", json={"enabled": True, "understood": True}).status_code == 400   # no PIN
    state = client.post("/api/kids", json={"enabled": True, "understood": True, "pin": "1234"}).get_json()
    assert state["enabled"] is True and state["enabled_at"] and state["pin_set"] is True
    # Switching a pal's kids mode on needs the parent PIN.
    assert client.put("/api/pals/" + made["id"], json={"kids": True}).status_code == 403
    assert client.put("/api/pals/" + made["id"], json={"kids": True, "pin": "0000"}).status_code == 403
    changed = client.put("/api/pals/" + made["id"], json={"kids": True, "pin": "1234"}).get_json()
    assert changed["kids"] is True and changed["features"]["presence"] is False
    assert client.get("/api/kids").get_json()["pals"] == ["Pip"]
    assert client.post("/api/kids", json={"enabled": False}).status_code == 403            # locking needs the PIN
    assert client.post("/api/kids", json={"enabled": False, "pin": "1234"}).status_code == 409
    made2 = client.post("/api/pals", json={"name": "Kit", "vibe": "chill", "kids": True}).get_json()
    assert made2["kids"] is True
    assert client.put("/api/pals/" + made["id"], json={"kids": False}).status_code == 403
    client.put("/api/pals/" + made["id"], json={"kids": False, "pin": "1234"})
    people = {p["id"]: p for p in client.get("/api/people").get_json()["people"]}
    assert people[made2["id"]]["kids"] is True and people[made["id"]]["kids"] is False


def test_the_kids_routes_refuse_other_websites(client, world):
    cross = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    assert client.post("/api/kids", json={"enabled": True, "understood": True}, headers=cross).status_code == 403
    assert kids.get_state()["enabled"] is False
