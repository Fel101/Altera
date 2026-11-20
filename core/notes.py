"""
Altera — What a companion knows about you, written down.

Every companion keeps two plain-text notes of her own in data/avatars/<id>/notes/:

    about_you.md   the person: name, life, people, likes, plans, how they like to be spoken to
    our_story.md   what happened between them: moments that mattered, running jokes, promises, how she feels, what happened lately

They are part of her prompt on every turn, so nothing depends on a search finding the right memory, and the person can read and
correct them (the Notes button). She writes them herself, with the chat model, from the conversation:

  * every `every_messages` messages she updates them (so closing the tab loses nothing), and
  * when the chat no longer fits comfortably in the model's context she updates them once more and the older part of the chat
    is left out of the prompt from then on ("compaction"). The messages stay in the database (search, export, timeline).

A bad rewrite must never cost her what she knew: the model's answer is checked (both notes present, not much shorter than
before), the previous version of each note is kept in notes/history/, and a failed attempt leaves everything as it was.
"""
import logging
import os
import re
import tempfile
import threading
import time
import urllib.request
import json
from datetime import datetime, UTC
from pathlib import Path

LOGGER = logging.getLogger(__name__)

NOTE_FILES = {"about_you": "about_you.md", "our_story": "our_story.md"}
HISTORY_KEEP = 8                       # old versions kept per note
CHARS_PER_TOKEN = 3.2                  # a cautious estimate for English text
FALLBACK_CONTEXT = 8192                # when neither config.yaml nor LM Studio says how long the context is
MAX_INPUT_CHARS = 14000                # conversation text handed to the model in one rewrite (about 4k tokens)
NOTE_MAX_TOKENS = 6000                 # room for a reasoning model to think before it writes the notes

DEFAULTS = {
    "enabled": True,
    "context_tokens": 0,               # 0 = ask LM Studio for the loaded model's context length; else the value to assume
    "trigger_ratio": 0.6,              # compact when prompt + reply would use more than this share of the context
    "keep_recent": 6,                  # messages that stay in the prompt word for word after a compaction
    "every_messages": 12,              # update the notes after this many new messages from the person
    "max_words": 450,                  # per note
    "history_messages": 40,            # never send more than this many messages, however large the context is
}

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_BLOCK = re.compile(r"<(about_you|our_story)>(.*?)</\1>", re.S | re.I)
_FENCE = re.compile(r"^```[a-z]*\s*|\s*```$", re.I)
_PHOTO = re.compile(r"\n?\[\[photo:[^\]]*\]\]")


def settings(config=None) -> dict:
    cfg = dict(DEFAULTS)
    user = (((config or {}).get("companion") or {}).get("notes") or {})
    for key, default in DEFAULTS.items():
        if key in user:
            try:
                cfg[key] = type(default)(user[key]) if not isinstance(default, bool) else bool(user[key])
            except (TypeError, ValueError):
                pass
    cfg["trigger_ratio"] = min(0.9, max(0.3, cfg["trigger_ratio"]))
    cfg["keep_recent"] = max(2, min(20, cfg["keep_recent"]))
    cfg["every_messages"] = max(3, cfg["every_messages"])
    cfg["max_words"] = max(150, cfg["max_words"])
    cfg["history_messages"] = max(6, cfg["history_messages"])
    return cfg


# ---- sizes ------------------------------------------------------------------------------------------------------------
def estimate_tokens(text) -> int:
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


def messages_tokens(messages) -> int:
    return sum(estimate_tokens(m.get("content")) + 4 for m in messages)


_ctx_cache = {"key": None, "at": 0.0, "value": None}


def context_tokens(config=None, base_url=None, model=None) -> int:
    """How many tokens the chat model can hold: config.yaml (companion.notes.context_tokens) if set, else what LM Studio reports
    for the loaded model, else a cautious default. (A wrong small guess only means more frequent notes updates.)"""
    fixed = settings(config)["context_tokens"]
    if fixed > 0:
        return fixed
    key = (base_url, model)
    now = time.time()
    if _ctx_cache["key"] == key and now - _ctx_cache["at"] < 60 and _ctx_cache["value"]:
        return _ctx_cache["value"]
    value = None
    if base_url:
        try:
            root = base_url.rstrip("/")
            root = root[:-3] if root.endswith("/v1") else root
            with urllib.request.urlopen(root + "/api/v0/models", timeout=1.5) as response:
                metadata = json.loads(response.read())
            for item in metadata.get("data", []):
                if item.get("id") == model and item.get("state") == "loaded" and item.get("loaded_context_length"):
                    value = int(item["loaded_context_length"])
                    break
        except Exception:
            value = None
    _ctx_cache.update(key=key, at=now, value=value or FALLBACK_CONTEXT, source='provider metadata' if value else 'fallback assumption')
    return value or FALLBACK_CONTEXT


# ---- the files --------------------------------------------------------------------------------------------------------
def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class NotesStore:
    """The note files and the small state file of each companion (under her avatar folder)."""

    def __init__(self, avatars):
        self.avatars = avatars

    def dir(self, avatar_id) -> Path | None:
        d = self.avatars._dir(avatar_id)
        return d / "notes" if d and d.is_dir() else None

    def read(self, avatar_id) -> dict:
        d = self.dir(avatar_id)
        out = {}
        for key, name in NOTE_FILES.items():
            try:
                out[key] = (d / name).read_text("utf-8").strip() if d else ""
            except OSError:
                out[key] = ""
        return out

    def write(self, avatar_id, about_you=None, our_story=None) -> bool:
        """Replace one or both notes. The old text is kept in notes/history/. Returns False if the companion does not exist."""
        d = self.dir(avatar_id)
        if d is None:
            root = self.avatars._dir(avatar_id)
            if not root or not root.is_dir():
                return False
            d = root / "notes"
        (d / "history").mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        for key, text in (("about_you", about_you), ("our_story", our_story)):
            if text is None:
                continue
            path = d / NOTE_FILES[key]
            try:
                old = path.read_text("utf-8")
            except OSError:
                old = ""
            text = text.strip()
            if old.strip() == text:
                continue
            if old.strip():
                # The timestamp is only as fine as the clock, so two rewrites in the same microsecond used to land on
                # the same filename and the earlier version was overwritten -- losing exactly what this history is for.
                history_path = d / "history" / f"{key}-{stamp}.md"
                suffix = 1
                while history_path.exists():
                    history_path = d / "history" / f"{key}-{stamp}-{suffix}.md"
                    suffix += 1
                history_path.write_text(old, "utf-8")
                for extra in sorted((d / "history").glob(f"{key}-*.md"))[:-HISTORY_KEEP]:
                    extra.unlink(missing_ok=True)
            _write_atomic(path, text + "\n" if text else "")
        return True

    def state(self, avatar_id) -> dict:
        d = self.dir(avatar_id)
        try:
            data = json.loads((d / "state.json").read_text("utf-8")) if d else {}
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def update_state(self, avatar_id, **changes) -> dict:
        root = self.avatars._dir(avatar_id)
        if not root or not root.is_dir():
            return {}
        d = root / "notes"
        d.mkdir(exist_ok=True)
        data = {**self.state(avatar_id), **changes}
        _write_atomic(d / "state.json", json.dumps(data, indent=2))
        return data

    def clear(self, avatar_id) -> None:
        """Forget everything she wrote (the chat was deleted)."""
        avatar = self.avatars._dir(avatar_id)
        if avatar is not None:
            from core.notebook_cleanup import clear_notebook
            clear_notebook(self.avatars.root, avatar)

    def clear_all(self) -> None:
        """Clear every notebook, including ones whose avatar record is damaged or missing."""
        import shutil
        root = self.avatars.root
        if not root.exists():
            return
        for folder in root.iterdir():
            if not folder.is_dir():
                continue
            notebook = folder / 'notes'
            if notebook.exists():
                try:
                    notebook.resolve().relative_to(root.resolve())
                except ValueError:
                    raise OSError('A notebook points outside the avatar folder; cleanup stopped.') from None
                shutil.rmtree(notebook)


# ---- the prompt for the model that writes the notes -------------------------------------------------------------------------
def _clean_message(content: str) -> str:
    return _PHOTO.sub("\n(an imagined image was requested; this is not evidence of a real event)", content or "").strip()


def transcript(messages, name: str) -> str:
    lines = []
    for m in messages:
        text = _clean_message(m.content if hasattr(m, "content") else m.get("content"))
        role = m.role if hasattr(m, "role") else m.get("role")
        if text:
            lines.append(f"{name if role == 'assistant' else 'Them'}: {text}")
    return "\n\n".join(lines)


def build_request(name: str, notes: dict, conversation: str, max_words: int) -> list:
    system = (f"You keep the private notebook of {name}, an AI companion, about the person she talks to. You rewrite two notes so "
              "that useful, supported information is retained without inventing facts. You answer with the two notes and nothing else. "
              "The old notebook is a derived summary, possibly user-edited or legacy, not independent evidence. "
              "Never promote assistant guesses, hypotheticals or roleplay into facts about the real person.")
    user = f"""Here is {name}'s notebook as it stands now.

<about_you>
{notes.get('about_you') or '(empty)'}
</about_you>

<our_story>
{notes.get('our_story') or '(empty)'}
</our_story>

Here is what was said since (oldest first). "Them" is the person, {name} is the companion:

{conversation}

Write the updated notebook.
- Keep everything already in the notes that is still true, add what this conversation shows, merge duplicates. If something changed, keep the newest version and mention the change briefly.
- Preserve uncertainty in existing entries; repetition in an old note is not confirmation. Prefer the person's explicit current corrections over earlier summaries and companion assertions.
- about_you: facts about THEM: name, age, work, where they live, family, pets, friends, history, likes and dislikes, habits, goals, worries, what they asked to be called, how they like to be spoken to. Only what they said or clearly showed. Never guess, never invent, never put anything about {name} here.
- New evidence contains original user words only. Assistant replies and generated reflection are excluded. Existing notebook text is a derived aggregate, not new independent evidence; preserve uncertainty and user edits rather than turning an old guess into a fact.
- our_story: exchanges, plans and moments the user actually described, with imagined scenes clearly labelled as imagined. Include a short "Lately" paragraph grounded in the supplied user words. Do not invent missing assistant actions, needs or feelings.
- Distinguish an actual conversation from an imagined shared scene, story, roleplay or generated picture. Never describe those as a real-world event or invent missing personal details; omit unsupported biography instead of guessing.
- Short bullet lines, plain words, no filler. Intimate matters are noted briefly and matter-of-factly, never described.
- Each note at most about {max_words} words. If you must cut, drop the least important details first.

Answer in exactly this form:
<about_you>
...
</about_you>
<our_story>
...
</our_story>"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_notes(text: str) -> dict | None:
    """{'about_you': ..., 'our_story': ...} from the model's answer (reasoning text and code fences are ignored), or None."""
    text = _THINK.sub("", text or "")
    found = {}
    for tag, body in _BLOCK.findall(text):
        found[tag.lower()] = _FENCE.sub("", body.strip()).strip()
    if set(found) != set(NOTE_FILES):
        return None
    return found


def _limit(max_words: int) -> int:
    """The most characters a note may have: about 7 a word, plus half again."""
    return int(max_words * 7 * 1.5)


def validate(old: dict, new: dict, max_words: int, had_conversation: bool = True) -> str | None:
    """Why a rewrite must not replace the notes (None = fine). A note that had grown past its size may shrink a lot."""
    if had_conversation and not any(new.values()):
        return "the rewrite was empty"
    for key in NOTE_FILES:
        before, after = len(old.get(key) or ""), len(new.get(key) or "")
        if 400 <= before <= _limit(max_words) and after < before * 0.5:
            return f"the rewrite of {key} was much shorter than before ({after} against {before} characters)"
    return None


def fit(text: str, max_words: int) -> str:
    """Cut a note that ran far over its size at a line boundary."""
    limit = _limit(max_words)
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut[:cut.rfind("\n")].rstrip() if "\n" in cut else cut


# ---- the service ------------------------------------------------------------------------------------------------------
class NotesService:
    """Decides when a companion should update her notes and does it (in the background, one at a time per companion).

    llm: a function(messages) -> text. Tests pass a fake; by default the chat model is used with a low temperature.
    sync: run the job inline (tests).
    """

    def __init__(self, store: NotesStore, datastore_getter, config=None, llm=None, sync=False, after_update=None):
        self.store = store
        self._ds = datastore_getter
        self.config = config or {}
        self._llm = llm
        self.sync = sync
        self._after_update = after_update
        self._busy = set()
        self._lock = threading.Lock()

    # ---- what to do -----------------------------------------------------------------------------------------------
    def cfg(self) -> dict:
        return settings(self.config)

    def busy(self, avatar_id) -> bool:
        with self._lock:
            return avatar_id in self._busy

    def reserve(self, avatar_id) -> bool:
        """Take this companion's one writer slot, shared with the memory writer, or say it is taken."""
        with self._lock:
            if avatar_id in self._busy:
                return False
            self._busy.add(avatar_id)
            return True

    def release(self, avatar_id) -> None:
        with self._lock:
            self._busy.discard(avatar_id)

    def history_after(self, avatar_id, session_id) -> int:
        """The id of the last message that is left out of her prompt (0 = none)."""
        from core.knowledge import dirty, floor
        if dirty(self._ds(), avatar_id):
            return floor(self._ds(), avatar_id)
        return max(floor(self._ds(), avatar_id), int(self.store.state(avatar_id).get("compacted_through") or 0))

    def plan(self, avatar_id, session_id, system_tokens: int, context: int, max_reply: int) -> str | None:
        """'compact', 'pass' or None."""
        cfg = self.cfg()
        from core.knowledge import dirty
        if dirty(self._ds(), avatar_id):
            return None
        if not cfg["enabled"] or not self.store.dir(avatar_id):
            return None
        state = self.store.state(avatar_id)
        ds = self._ds()
        history = ds.get_messages_after(session_id, self.history_after(avatar_id, session_id))
        used = system_tokens + messages_tokens([{"content": m.content} for m in history]) + min(max_reply, int(context * 0.2))
        unnoted = ds.count_user_messages(session_id, int(state.get("noted_through") or 0))
        if ds.count_user_messages(session_id) < int(state.get("blocked_until_users") or 0):
            return None                                      # a rewrite failed a moment ago: wait for a few more messages
        too_big = used > context * cfg["trigger_ratio"] or len(history) > cfg["history_messages"] * 1.5
        if too_big and len(history) > cfg["keep_recent"]:
            return "compact"
        if unnoted >= cfg["every_messages"]:
            return "pass"
        return None

    def schedule(self, avatar_id, session_id, system_tokens: int, context: int, max_reply: int) -> str | None:
        """Start whatever the notes need after a reply. Never raises, never blocks (unless sync)."""
        try:
            what = self.plan(avatar_id, session_id, system_tokens, context, max_reply)
            if not what:
                return None
            if not self.reserve(avatar_id):
                return None

        except Exception:
            LOGGER.exception("Could not plan a notes update")
            return None
        if self.sync:
            self._job(avatar_id, session_id, what == "compact")
        else:
            from core.state_gate import start_job
            try:
                start_job(self._job, (avatar_id, session_id, what == "compact"), name="altera-notes")
            except Exception:
                self.release(avatar_id)
                LOGGER.exception("Could not start a notes update")
                return None

        return what

    def run_now(self, avatar_id, session_id, trim=False) -> dict:
        """The Update now button: fold everything new into the notes. Returns {'ok', 'error'?}."""
        with self._lock:
            if avatar_id in self._busy:
                return {"ok": False, "error": "She is already writing things down."}
            self._busy.add(avatar_id)
        return self._job(avatar_id, session_id, trim)

    # ---- the job --------------------------------------------------------------------------------------------------
    def _job(self, avatar_id, session_id, trim: bool) -> dict:
        try:
            return self._update(avatar_id, session_id, trim)
        except Exception as e:
            LOGGER.exception("Notes update failed")
            self._failed(avatar_id, session_id, f"{type(e).__name__}: {e}")
            return {"ok": False, "error": "The notes could not be updated."}
        finally:
            if self._after_update is not None:
                try:
                    self._after_update(avatar_id, session_id)
                except Exception:
                    LOGGER.exception("Could not write memories after the notes update")
            self.release(avatar_id)

    def _failed(self, avatar_id, session_id, why) -> None:
        state = self.store.state(avatar_id)
        users = self._ds().count_user_messages(session_id)
        self.store.update_state(avatar_id, failures=int(state.get("failures") or 0) + 1, last_error=why[:300],
                                blocked_until_users=users + 3)

    def _call(self, messages) -> str:
        if self._llm:
            return self._llm(messages)
        from core.llm import LLMClient
        base = LLMClient.get_instance(self.config)
        client = LLMClient(base_url=base.base_url, api_key=base.api_key, model=base.model, temperature=0.2, max_tokens=NOTE_MAX_TOKENS)
        return client.chat(messages, stream=False)

    def _update(self, avatar_id, session_id, trim: bool) -> dict:
        cfg = self.cfg()
        avatar = self.store.avatars.get(avatar_id)
        if not avatar or self.store.dir(avatar_id) is None:
            return {"ok": False, "error": "That companion was not found."}
        name = avatar.get("name") or "Companion"
        ds = self._ds()
        from core.knowledge import dirty, floor, prompt_messages
        if dirty(ds, avatar_id):
            return {"ok": False, "error": "Repair memories from the current chat before writing new notes."}
        state = self.store.state(avatar_id)
        noted = max(floor(ds, avatar_id), int(state.get("noted_through") or 0))
        # Assistant wording may repeat a guess or generated reflection. Only
        # original user words become new evidence in the factual notebook.
        scanned = ds.get_messages_after(session_id, noted)
        pending = [m for m in prompt_messages(ds, session_id, scanned, allow_derived=False)
                   if m.role == 'user']
        notes = self.store.read(avatar_id)
        includes_user_edits = bool(state.get('includes_user_edits') or state.get('source_kind') == 'user-edited')
        includes_legacy_notes = bool(state.get('includes_legacy_notes') or (not state.get('source_kind') and any(notes.values())))

        # fold the new messages in, a chunk at a time (a long backlog needs several rewrites)
        chunk, size = [], 0
        chunks = []
        for m in pending:
            text = transcript([m], name)
            if not text:
                continue
            if chunk and size + len(text) > MAX_INPUT_CHARS:
                chunks.append(chunk)
                chunk, size = [], 0
            chunk.append(m)
            size += len(text)
        if chunk:
            chunks.append(chunk)

        for group in chunks:
            answer = self._call(build_request(name, notes, transcript(group, name), cfg["max_words"]))
            new = parse_notes(answer)
            if new is None:
                raise ValueError("the model did not answer with both notes")
            new = {k: fit(v, cfg["max_words"]) for k, v in new.items()}
            problem = validate(notes, new, cfg["max_words"])
            if problem:
                raise ValueError(problem)
            if dirty(ds, avatar_id):
                raise InterruptedError('The source conversation changed while writing notes.')
            self.store.write(avatar_id, about_you=new["about_you"], our_story=new["our_story"])
            notes = new
            noted = group[-1].id
            # UTC with its offset, so the page can say how long ago it was whatever the time zone
            self.store.update_state(avatar_id, noted_through=noted, updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
                                    failures=0, last_error="", blocked_until_users=0, source_session_id=session_id,
                                    source_through=noted, source_after=floor(ds, avatar_id), source_kind='conversation',
                                    new_evidence_roles=['user'],
                                    includes_user_edits=includes_user_edits, includes_legacy_notes=includes_legacy_notes)

        if scanned:
            # Scanner progress can pass excluded assistant rows; provenance
            # through the last user source above still describes actual input.
            self.store.update_state(avatar_id, noted_through=scanned[-1].id)
        if trim:
            everything = ds.get_messages_after(session_id, int(self.store.state(avatar_id).get("compacted_through") or 0))
            keep = cfg["keep_recent"]
            if len(everything) > keep:
                cutoff = everything[-keep - 1].id
                # only messages the notes have seen are left out of her prompt
                cutoff = min(cutoff, int(self.store.state(avatar_id).get("noted_through") or 0))
                self.store.update_state(avatar_id, compacted_through=cutoff)
        return {"ok": True, "chunks": len(chunks)}

    def status(self, avatar_id, session_id=None) -> dict:
        state = self.store.state(avatar_id)
        notes = self.store.read(avatar_id)
        ds = self._ds()
        return {
            "busy": self.busy(avatar_id), "updated_at": state.get("updated_at") or "", "failures": int(state.get("failures") or 0),
            "last_error": state.get("last_error") or "",
            "words": {k: len(v.split()) for k, v in notes.items()},
            "waiting": ds.count_user_messages(session_id, int(state.get("noted_through") or 0)) if session_id else 0,
            "compacted_through": int(state.get("compacted_through") or 0),
            "sources": {k: state.get(k) for k in ('source_kind', 'source_session_id', 'source_after', 'source_through', 'includes_user_edits', 'includes_legacy_notes')},
        }


# ---- shared instance --------------------------------------------------------------------------------------------------
_service: NotesService | None = None
_owner = None                          # the Services the shared service was built for
_pinned = False                        # set_notes_service() was used: keep that one
_service_lock = threading.Lock()


def _memory_after_notes(avatar_id, session_id) -> None:
    """The notebook pass is the natural moment to read the same messages for memories. One model call at a time."""
    try:
        from core.memory import get_memory_writer
        get_memory_writer().run(avatar_id)
    except Exception:
        LOGGER.exception("Could not write memories after the notes update")


def get_notes_service() -> NotesService:
    global _service, _owner
    with _service_lock:
        if _pinned and _service is not None:
            return _service
        from core import companion
        services = companion.get_services()
        if _service is None or _owner is not services:
            from core.datastore import get_datastore
            _service = NotesService(NotesStore(services.avatars), get_datastore, services.config,
                                    after_update=_memory_after_notes)
            _owner = services
        return _service


def set_notes_service(service: NotesService | None) -> None:
    """Pin the shared service (or, with None, forget it and build a fresh one on demand). Used by tests."""
    global _service, _owner, _pinned
    with _service_lock:
        _service, _owner, _pinned = service, None, service is not None
