"""Each companion's own notes, compaction, memories and prompt. A fake model writes the notes; nothing here reaches a real one."""
import json


from core import companions, notes
from core.avatars import card_from_npc
from core.memory import MemoryDeduplicator, MemoryStore
from test_companion_api import FakeLLM, FakeMemory, active_avatar, drain, engine, system_prompt, tmp_store, world  # noqa: F401
from test_companion_images import make_forge


# ---------------------------------------------------------------- helpers

def answer(about="- Likes strong tea", story="- They met today"):
    return f"<about_you>\n{about}\n</about_you>\n<our_story>\n{story}\n</our_story>"


class NotesLLM:
    """Stands in for the model that writes the notes: replies in order (the last one repeats) and remembers what it was asked."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies) or [answer()], []

    def __call__(self, messages):
        self.calls.append(messages)
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return reply(messages) if callable(reply) else reply


def service(world, llm, **cfg):
    return notes.NotesService(notes.NotesStore(world.avatars), lambda: world.store, {"companion": {"notes": cfg}}, llm=llm, sync=True)


def chat_of(world, avatar_id, pairs):
    """A chat for the avatar with (user, assistant) message pairs; returns the session id."""
    sid = world.store.ensure_avatar_session(avatar_id, "chat")
    for user, reply in pairs:
        world.store.save_message(sid, "user", user)
        world.store.save_message(sid, "assistant", reply)
    return sid


def talk(n, prefix="message"):
    return [(f"{prefix} {i} from the person", f"her answer {i}") for i in range(n)]


# ---------------------------------------------------------------- settings, parsing, checks

def test_settings_have_defaults_and_bad_values_fall_back():
    assert notes.settings({})["every_messages"] == 12 and notes.settings({})["keep_recent"] == 6
    cfg = notes.settings({"companion": {"notes": {"every_messages": "x", "keep_recent": 999, "trigger_ratio": 5, "enabled": 0}}})
    assert cfg["every_messages"] == 12 and cfg["keep_recent"] == 20 and cfg["trigger_ratio"] == 0.9 and cfg["enabled"] is False


def test_the_context_length_comes_from_config_or_falls_back_without_asking_anyone():
    assert notes.context_tokens({"companion": {"notes": {"context_tokens": 32768}}}) == 32768
    assert notes.context_tokens({}, None, None) == notes.FALLBACK_CONTEXT              # no server known: no network call


def test_the_answer_is_parsed_even_with_reasoning_and_fences():
    text = "<think>hmm <about_you>not this</about_you></think>\n<about_you>\n```\n- A\n```\n</about_you>\n<our_story>- B</our_story>"
    assert notes.parse_notes(text) == {"about_you": "- A", "our_story": "- B"}
    assert notes.parse_notes("<about_you>- A</about_you>") is None and notes.parse_notes("nothing useful") is None


def test_a_rewrite_may_not_be_empty_or_much_shorter_but_an_oversized_note_may_shrink():
    old = {"about_you": "x" * 800, "our_story": "y" * 800}
    assert "empty" in notes.validate(old, {"about_you": "", "our_story": ""}, 450)
    assert "shorter" in notes.validate(old, {"about_you": "x" * 100, "our_story": "y" * 800}, 450)
    assert notes.validate(old, {"about_you": "x" * 700, "our_story": "y" * 600}, 450) is None
    bloated = {"about_you": "x" * 9000, "our_story": ""}                     # past its size: condensing is what we want
    assert notes.validate(bloated, {"about_you": "x" * 1500, "our_story": "z"}, 450) is None
    assert len(notes.fit("line\n" * 5000, 450)) <= int(450 * 7 * 1.5)


# ---------------------------------------------------------------- the files

def test_notes_are_written_atomically_and_the_old_text_is_kept(world):
    record = active_avatar(world)
    store = notes.NotesStore(world.avatars)
    assert store.read(record["id"]) == {"about_you": "", "our_story": ""}
    assert store.write(record["id"], about_you="- one", our_story="- story")
    assert store.read(record["id"]) == {"about_you": "- one", "our_story": "- story"}
    store.write(record["id"], about_you="- two")                              # the other note is left alone
    assert store.read(record["id"]) == {"about_you": "- two", "our_story": "- story"}
    history = sorted(p.name for p in (store.dir(record["id"]) / "history").glob("about_you-*.md"))
    assert len(history) == 1 and (store.dir(record["id"]) / "history" / history[0]).read_text("utf-8") == "- one\n"
    for i in range(12):
        store.write(record["id"], about_you=f"- version {i}")
    assert len(list((store.dir(record["id"]) / "history").glob("about_you-*.md"))) == notes.HISTORY_KEEP
    assert store.write("nobody-here-1", about_you="x") is False
    store.clear(record["id"])
    assert store.read(record["id"]) == {"about_you": "", "our_story": ""}


# ---------------------------------------------------------------- the service

def test_a_pass_folds_the_new_messages_in_and_leaves_the_chat_alone(world):
    record = active_avatar(world)
    sid = chat_of(world, record["id"], talk(3))
    llm = NotesLLM(answer("- Name is Sam", "- Just met"))
    svc = service(world, llm)
    assert svc.run_now(record["id"], sid) == {"ok": True, "chunks": 1}
    assert svc.store.read(record["id"]) == {"about_you": "- Name is Sam", "our_story": "- Just met"}
    state = svc.store.state(record["id"])
    assert state["noted_through"] == world.store.get_messages(sid)[-1].id and not state.get("compacted_through")
    assert state["updated_at"].endswith("+00:00")                              # UTC with its offset: the page says "3 min ago" in any zone
    asked = llm.calls[0][1]["content"]
    assert "message 2 from the person" in asked and "Them:" in asked and "Mara:" not in asked
    assert state['new_evidence_roles'] == ['user']
    assert svc.run_now(record["id"], sid) == {"ok": True, "chunks": 0}         # nothing new: no second call
    assert len(llm.calls) == 1


def test_the_current_notes_and_the_new_messages_go_to_the_model_and_photo_markers_are_hidden(world):
    record = active_avatar(world)
    svc = service(world, NotesLLM())
    svc.store.write(record["id"], about_you="- Loves hiking")
    sid = world.store.ensure_avatar_session(record["id"], "c")
    world.store.save_message(sid, "user", "hello")
    world.store.save_message(sid, "assistant", "Here you go\n[[photo:p-0123456789]]")
    svc.run_now(record["id"], sid)
    text = svc.store.avatars and svc._llm.calls[0][1]["content"]
    assert "- Loves hiking" in text and "hello" in text and "Here you go" not in text and "p-0123456789" not in text


def test_legacy_photo_marker_in_notebook_transcript_is_not_a_real_event():
    cleaned = notes._clean_message('Look\n[[photo:p-0123456789]]')
    assert 'imagined image was requested' in cleaned and 'not evidence of a real event' in cleaned
    assert 'p-0123456789' not in cleaned


def test_a_bad_rewrite_changes_nothing_and_waits_for_a_few_more_messages(world):
    record = active_avatar(world)
    sid = chat_of(world, record["id"], talk(4))
    svc = service(world, NotesLLM(answer("- " + "x" * 600, "- " + "y" * 600), answer("- tiny", "- tiny")), every_messages=3)
    svc.run_now(record["id"], sid)                                              # good: 1200 characters of notes
    before = svc.store.read(record["id"])
    for user, reply in talk(3, "later"):
        world.store.save_message(sid, "user", user)
        world.store.save_message(sid, "assistant", reply)
    result = svc.run_now(record["id"], sid)                                     # the model drops nearly everything
    assert result["ok"] is False and svc.store.read(record["id"]) == before
    state = svc.store.state(record["id"])
    assert state["failures"] == 1 and "shorter" in state["last_error"]
    assert svc.plan(record["id"], sid, 100, 8192, 500) is None                  # wait: it just failed
    for user, reply in talk(3, "even later"):
        world.store.save_message(sid, "user", user)
        world.store.save_message(sid, "assistant", reply)
    assert svc.plan(record["id"], sid, 100, 8192, 500) == "pass"                # three more messages: try again


def test_an_answer_without_both_notes_is_a_failure_not_a_rewrite(world):
    record = active_avatar(world)
    sid = chat_of(world, record["id"], talk(2))
    svc = service(world, NotesLLM("Sorry, I cannot do that."))
    assert svc.run_now(record["id"], sid)["ok"] is False
    assert svc.store.read(record["id"]) == {"about_you": "", "our_story": ""}
    assert svc.status(record["id"], sid)["failures"] == 1 and svc.status(record["id"], sid)["last_error"]


def test_the_plan_wants_a_pass_after_enough_messages_and_a_compaction_when_the_prompt_is_too_big(world):
    record = active_avatar(world)
    sid = chat_of(world, record["id"], talk(5))
    svc = service(world, NotesLLM(), every_messages=6, keep_recent=4, trigger_ratio=0.6)
    assert svc.plan(record["id"], sid, 500, 8192, 1024) is None                 # small chat, big context: nothing to do
    chat_of(world, record["id"], talk(2))
    assert svc.plan(record["id"], sid, 500, 8192, 1024) == "pass"               # six messages from the person
    assert svc.plan(record["id"], sid, 500, 900, 1024) == "compact"             # the same chat in a tiny context
    assert service(world, NotesLLM(), enabled=False).plan(record["id"], sid, 500, 900, 1024) is None
    assert svc.plan("nobody-here-1", sid, 500, 900, 1024) is None


def test_compaction_folds_everything_in_and_then_leaves_out_all_but_the_recent_messages(world):
    record = active_avatar(world)
    sid = chat_of(world, record["id"], talk(10))
    svc = service(world, NotesLLM(), keep_recent=4)
    assert svc.schedule(record["id"], sid, 500, 900, 1024) == "compact"          # runs inline in the test
    state = svc.store.state(record["id"])
    everything = world.store.get_messages(sid, limit=100)
    assert state["noted_through"] == everything[-1].id
    assert state["compacted_through"] == everything[-5].id                      # the last four stay
    assert [m.id for m in world.store.get_messages_after(sid, state["compacted_through"])] == [m.id for m in everything[-4:]]
    assert svc.history_after(record["id"], sid) == everything[-5].id
    assert svc.plan(record["id"], sid, 500, 900, 1024) is None                  # nothing left to fold or to trim


def test_a_long_backlog_is_folded_in_several_rewrites(world, monkeypatch):
    record = active_avatar(world)
    monkeypatch.setattr(notes, "MAX_INPUT_CHARS", 300)
    sid = chat_of(world, record["id"], [("word " * 30, "answer " * 20) for _ in range(6)])
    llm = NotesLLM()
    svc = service(world, llm)
    assert svc.run_now(record["id"], sid)["chunks"] == len(llm.calls) > 2
    assert svc.store.state(record["id"])["noted_through"] == world.store.get_messages(sid, limit=50)[-1].id


def test_only_one_update_runs_at_a_time_for_a_companion(world):
    record = active_avatar(world)
    sid = chat_of(world, record["id"], talk(3))
    svc = service(world, NotesLLM())
    svc._busy.add(record["id"])
    assert svc.busy(record["id"]) and svc.run_now(record["id"], sid)["ok"] is False
    assert svc.schedule(record["id"], sid, 500, 900, 1024) is None
    svc._busy.clear()
    assert svc.run_now(record["id"], sid)["ok"] is True and not svc.busy(record["id"])


# ---------------------------------------------------------------- what the chat engine does with them

def test_her_notes_are_in_her_prompt_and_another_companions_notes_are_not(engine, world):
    record = active_avatar(world)
    svc = service(world, NotesLLM())
    notes.set_notes_service(svc)
    svc.store.write(record["id"], about_you="- Their name is Sam", our_story="- We met on a rainy day")
    make_forge(world.forge, npc_id="ana-cruz-11aa22", first="Ana")
    other = world.avatars.import_from_forge("ana-cruz-11aa22")
    world.avatars.activate(record["id"])

    sid = world.store.ensure_avatar_session(record["id"], "Mara")
    engine.send_message("hi", sid)
    prompt = system_prompt(engine)
    assert "Their name is Sam" in prompt and "We met on a rainy day" in prompt and "Your Own Notes" in prompt

    other_sid = world.store.ensure_avatar_session(other["id"], "Ana")
    engine.send_message("hi", other_sid)
    prompt = system_prompt(engine)
    assert "You are Ana," in prompt and "Sam" not in prompt and "Your Own Notes" not in prompt


def test_a_new_companions_prompt_has_her_first_days_and_it_fades_after_a_while(engine, world):
    record = active_avatar(world)
    notes.set_notes_service(service(world, NotesLLM()))
    sid = world.store.ensure_avatar_session(record["id"], "Mara")
    engine.send_message("hi", sid)
    prompt = system_prompt(engine)
    assert "Your First Days" in prompt
    assert "Being new does not make you childlike, naive or helpless" in prompt          # an adult mind with no past, never a child
    for i in range(companions.NEWBORN_MESSAGES):
        world.store.save_message(sid, "user", f"message {i}")
    engine.send_message("still here", sid)
    assert "Your First Days" not in system_prompt(engine)


def test_the_prompt_uses_the_character_made_in_npc_forge(engine, world):
    record = active_avatar(world)
    world.avatars.update_state(record["id"], self_view="I see warm hazel eyes.")
    card = record and world.avatars.get(record["id"])["card"]
    card.update(speech="quick and chatty", demeanor="friendly and open", quirk="humming along to every song",
                goal="opening a flower shop", peeve="cold coffee", world="the late-1800s American Old West")
    world.avatars._dir(record["id"]).joinpath("avatar.json").write_text(
        json.dumps({**world.avatars.get(record["id"]), "card": card}), "utf-8")
    sid = world.store.ensure_avatar_session(record["id"], "Mara")
    engine.send_message("hi", sid)
    prompt = system_prompt(engine)
    for expected in ("You are Mara, an AI companion with a personality of your own", "- Personality: warm", "- Age: 34",
                     "The way you talk: quick and chatty", "How you come across: friendly and open",
                     "- Quirk: humming along to every song", "- What you dream of: opening a flower shop", "- Pet peeve: cold coffee",
                     "- How you see yourself: I see warm hazel eyes.", "Old West"):
        assert expected in prompt, expected
    assert "witty" not in prompt and "Traits:" not in prompt                     # the default persona's traits are not hers


def test_the_default_persona_is_unchanged_when_there_is_no_avatar(engine, world):
    sid = world.store.create_session("t")
    engine.send_message("hi", sid)
    prompt = system_prompt(engine)
    assert "You are Altera, an AI companion. You are NOT a generic assistant" in prompt and "- Traits:" in prompt
    assert "Your First Days" not in prompt and "Your Own Notes" not in prompt


def test_her_chat_after_a_compaction_starts_where_her_notes_left_off(engine, world):
    record = active_avatar(world)
    svc = service(world, NotesLLM(), keep_recent=4)
    notes.set_notes_service(svc)
    sid = chat_of(world, record["id"], talk(10))
    svc.run_now(record["id"], sid, trim=True)
    engine.send_message("what now?", sid)
    sent = [m["content"] for m in engine.llm.calls[-1][1:]]
    assert sent == ["message 8 from the person", "her answer 8", "message 9 from the person", "her answer 9", "what now?"]
    assert len(world.store.get_messages(sid, limit=100)) == 22                   # nothing was deleted (20, her question, her reply)


def test_the_history_that_is_sent_fits_the_context(engine, world, monkeypatch):
    record = active_avatar(world)
    sid = chat_of(world, record["id"], [("x" * 800, "y" * 800) for _ in range(20)])
    monkeypatch.setattr(engine, "_context_size", lambda: (3500, 500))
    engine.send_message("and now?", sid)
    sent = engine.llm.calls[-1][1:]
    assert 2 <= len(sent) < 41 and sent[-1]["content"] == "and now?"
    assert sum(len(m["content"]) for m in sent) / notes.CHARS_PER_TOKEN < 3500


def test_a_reply_starts_the_notes_when_they_are_due_and_the_page_is_told(engine, world):
    record = active_avatar(world)
    svc = service(world, NotesLLM(), every_messages=3)
    notes.set_notes_service(svc)
    sid = world.store.ensure_avatar_session(record["id"], "Mara")
    for i in range(2):
        engine.llm.chunks = ["ok"]
        text, photos = drain(engine.send_message(f"one {i}", sid, stream=True))
        assert not svc.store.read(record["id"])["about_you"]
    chunks = list(engine.send_message("third message", sid, stream=True))
    assert {"notes": "pass"} in chunks
    assert svc.store.read(record["id"])["about_you"] == "- Likes strong tea"


def test_a_reply_that_is_not_streamed_is_saved_too(engine, world):
    record = active_avatar(world)
    notes.set_notes_service(service(world, NotesLLM()))
    sid = world.store.ensure_avatar_session(record["id"], "Mara")
    engine.llm.reply = "Hello there"
    assert engine.send_message("hi", sid) == "Hello there"
    assert [(m.role, m.content) for m in world.store.get_messages(sid)] == [("user", "hi"), ("assistant", "Hello there")]


def test_she_speaks_first_and_nothing_is_saved_for_the_person(engine, world):
    record = active_avatar(world)
    notes.set_notes_service(service(world, NotesLLM()))
    sid = world.store.ensure_avatar_session(record["id"], "Mara")
    engine.llm.chunks = ["Oh! Hello... is someone there?"]
    text, photos = drain(engine.send_message("", sid, stream=True, greet=True))
    assert text == "Oh! Hello... is someone there?"
    assert [(m.role, m.content) for m in world.store.get_messages(sid)] == [("assistant", text)]
    sent = engine.llm.calls[-1]
    assert "Your First Days" in sent[0]["content"] and sent[-1]["role"] == "user" and "come into being" in sent[-1]["content"]
    assert not world.avatars.state(record["id"]).get("first_met")                # not met until the person speaks
    engine.send_message("hi Mara", sid)
    assert world.avatars.state(record["id"]).get("first_met")


# ---------------------------------------------------------------- each companion only remembers what was said to her

class FakeCollection:
    """Just enough of a ChromaDB collection to check who a memory belongs to."""

    def __init__(self):
        self.rows = {}

    def add(self, documents, metadatas, ids):
        for d, m, i in zip(documents, metadatas, ids):
            self.rows[i] = (d, dict(m))

    def _match(self, where):
        return [i for i, (_, m) in self.rows.items() if not where or all(m.get(k) == v for k, v in where.items())]

    def query(self, query_texts, n_results, where=None):
        ids = self._match(where)[:n_results]
        return {"ids": [ids], "documents": [[self.rows[i][0] for i in ids]], "metadatas": [[self.rows[i][1] for i in ids]],
                "distances": [[0.1 for _ in ids]]}

    def get(self, where=None, ids=None):
        found = [i for i in (ids if ids is not None else self._match(where)) if i in self.rows]
        return {"ids": list(found), "documents": [self.rows[i][0] for i in found], "metadatas": [self.rows[i][1] for i in found]}

    def update(self, ids, metadatas, documents=None):
        for n, (i, m) in enumerate(zip(ids, metadatas)):
            self.rows[i] = (documents[n] if documents else self.rows[i][0], {**self.rows[i][1], **m})

    def delete(self, ids):
        for i in ids:
            self.rows.pop(i, None)


def memory_store():
    store = MemoryStore.__new__(MemoryStore)
    store.persist_directory, store._client, store._collection = "unused", None, FakeCollection()
    return store


def test_memories_belong_to_one_companion(monkeypatch):
    store = memory_store()
    a = store.add_memory("Sam loves hiking", avatar_id="mara-1")
    b = store.add_memory("Sam hates hiking", avatar_id="ana-2")
    assert [m["id"] for m in store.search("hiking", avatar_id="mara-1")] == [a]
    assert [m["id"] for m in store.search("hiking", avatar_id="ana-2")] == [b]
    assert len(store.search("hiking")) == 2 and len(store.get_all()) == 2 and len(store.get_all(avatar_id="mara-1")) == 1
    store.collection.add(documents=["from before"], metadatas=[{"type": "fact"}], ids=["old"])
    assert store.assign_unowned("mara-1") == 1 and store.assign_unowned("mara-1") == 0
    assert {m["id"] for m in store.get_all(avatar_id="mara-1")} == {a, "old"}


def test_duplicates_are_only_looked_for_inside_one_companions_memories():
    def same(i, avatar):
        return {"id": i, "content": "Sam loves hiking in the mountains", "metadata": {"type": "fact", "avatar_id": avatar}}
    groups = MemoryDeduplicator().find_duplicates([same("1", "a"), same("2", "b")])
    assert groups == []
    assert len(MemoryDeduplicator().find_duplicates([same("1", "a"), same("2", "a")])) == 1


def test_the_writer_searches_and_stores_memories_for_the_chats_companion(world, monkeypatch, altera_config):
    import core.memory
    from core.memory import MemoryExtractor, MemoryManager, MemoryWriter
    seen = {}

    class Store:
        def search(self, content, limit=3, avatar_id=None):
            seen["search"] = avatar_id
            return []

        def add_memory(self, **kw):
            seen["added"] = kw
            return "mem"

    manager = MemoryManager.__new__(MemoryManager)
    manager.store, manager.extractor = Store(), MemoryExtractor()
    monkeypatch.setattr(core.memory, "memory_manager", manager, raising=False)
    record = active_avatar(world)
    sid = world.store.ensure_avatar_session(record["id"], "Mara")
    world.store.save_message(sid, "user", "I have a dog")

    def no_model(messages):
        raise RuntimeError('no chat model in this test')

    writer = MemoryWriter(core.memory.get_memory_manager, lambda: world.store, config_getter=dict,
                          llm=no_model, sync=True)
    writer.run(record["id"])
    assert seen["search"] == seen["added"]["avatar_id"] == record["id"]


# ---------------------------------------------------------------- the card carries what NPC Forge made

def test_the_card_carries_the_personality_from_npc_forge():
    npc = {"setting": "western", "style": "photoreal", "profile": {
        "first_name": "Hattie", "last_name": "Hollis", "age": 29, "gender": "female", "personality": ["dramatic"],
        "speech": "vivid and theatrical", "demeanor": "proud", "quirk": "a slow pocket watch", "goal": "opening a saloon",
        "peeve": "bankers", "occupation": "saloon performer", "hobbies": ["fishing"], "bio": "A performer."}, "appearance": {}}
    card = card_from_npc(npc)
    assert (card["speech"], card["demeanor"], card["quirk"], card["goal"], card["peeve"]) == (
        "vivid and theatrical", "proud", "a slow pocket watch", "opening a saloon", "bankers")
    assert "Old West" in card["world"]
    older = card_from_npc({"profile": {"first_name": "Old", "age": 30}, "appearance": {}})
    assert older["speech"] == older["demeanor"] == older["world"] == ""                  # an older character has none of it


def test_pronoun_placeholders_in_older_characters_are_filled_in():
    from core.avatars import fill_pronouns
    assert fill_pronouns("opening a place of {poss} own", "female") == "opening a place of her own"
    assert fill_pronouns("{Subj} walks {poss} dog", "male") == "He walks his dog"
    assert fill_pronouns("laughing at {poss} own jokes", "nonbinary") == "laughing at their own jokes"
    assert fill_pronouns("{unknown} stays", "female") == "{unknown} stays" and fill_pronouns(None, "female") == ""
    npc = {"profile": {"first_name": "Old", "age": 30, "gender": "female", "goal": "opening a place of {poss} own",
                       "quirk": "laughing at {poss} own jokes", "hobbies": ["taking {poss} time"]}, "appearance": {}}
    card = card_from_npc(npc)
    assert card["goal"] == "opening a place of her own" and card["quirk"] == "laughing at her own jokes"
    assert card["hobbies"] == ["taking her time"]


def test_edits_made_in_npc_forge_reach_the_card_but_not_her_name_or_age(world):
    record = active_avatar(world)
    npc_file = world.forge / record["id"] / "npc.json"
    npc = json.loads(npc_file.read_text("utf-8"))
    npc["updated"] = "2026-09-21T09:00:00"
    npc["profile"].update(first_name="Someone", age=50, speech="slow and deliberate", personality=["curious", "dry"])
    npc_file.write_text(json.dumps(npc), "utf-8")
    assert world.avatars.refresh_card(record["id"]) is True
    card = world.avatars.get(record["id"])["card"]
    assert card["speech"] == "slow and deliberate" and card["personality"] == ["curious", "dry"]
    assert card["name"] == "Mara" and card["age"] == 34                                  # as imported
    assert world.avatars.refresh_card(record["id"]) is False                             # nothing new the second time
    npc["profile"]["bio"] = "A minor, aged 15, in school."
    npc["updated"] = "2026-09-21T10:00:00"
    npc_file.write_text(json.dumps(npc), "utf-8")
    assert world.avatars.refresh_card(record["id"]) is False                             # a card that breaks the rules is not taken
    assert "minor" not in world.avatars.get(record["id"])["card"]["bio"]


def test_companion_state_is_kept_beside_the_avatar(world):
    record = active_avatar(world)
    assert world.avatars.state(record["id"]) == {}
    world.avatars.update_state(record["id"], first_met="now")
    world.avatars.update_state(record["id"], self_view="x")
    assert world.avatars.state(record["id"]) == {"first_met": "now", "self_view": "x"}
    assert world.avatars.update_state("nobody-here-1", a=1) == {}
