"""Companions written in Altera itself, without NPC Forge.

NPC Forge needs ComfyUI and a capable GPU. This is the other way to make a companion: a name, an age, a gender and the
same card fields an import fills (bio, hobbies, speech, demeanor, quirk, goal, peeve). The record is the same shape as an
imported companion's, so the chat, notes, memories, story, voice and pictures work exactly the same.

The age rule and `safety.card_problem` apply exactly as they do to an import: 18 or older, and no text describing a minor
or a real person. The portrait is the initial until a picture is uploaded (age-checked, `AvatarStore.set_uploaded_portrait`)
or NPC Forge paints one through the ordinary portrait dialog (`core/portraits.py`).

A written companion can be edited later: the same fields, through `update`.
"""
import re
import secrets

from core import pals, safety
from core.avatars import (FEATURES, HOBBIES_MAX, HOBBY_MAX, WRITTEN_PREFIX, WRITTEN_SOURCE, WRITTEN_TEXT_LIMITS,
                          _now, _write_json, card_from_written, features_of, valid_id, written_here)

GENDERS = ("female", "male", "nonbinary")
_GENDER_ALIASES = {"non-binary": "nonbinary", "non binary": "nonbinary", "enby": "nonbinary", "nb": "nonbinary"}


class WrittenError(ValueError):
    """Something the person can fix: the message is safe to show."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---- what the person typed ------------------------------------------------------------------------------------------
def _name(value) -> str:
    try:
        return pals._check_name(value)                     # the same name rules as a pal
    except pals.PalError as error:
        raise WrittenError(str(error)) from None


def _age(value) -> int:
    if type(value) is bool:
        raise WrittenError("The age must be a number.")
    try:
        age = int(value) if isinstance(value, (int, float)) else int(str(value).strip())
    except (TypeError, ValueError):
        raise WrittenError("The age must be a number.") from None
    problem = safety.age_problem(age)
    if problem:
        raise WrittenError(problem)
    return age


def _gender(value) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    text = _GENDER_ALIASES.get(text, text)
    if text and text not in GENDERS:
        raise WrittenError("Choose female, male, non-binary, or leave gender blank.")
    return text


def _text(value, limit, label) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise WrittenError(f"The {label} must be text.")
    text = value.strip()
    if len(text) > limit:
        raise WrittenError(f"Keep the {label} under {limit} characters.")
    return text


def _hobbies(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        items = re.split(r"[,\n]", value)
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise WrittenError("Hobbies must be a list of short phrases.")
    out = []
    for item in items:
        if not isinstance(item, str):
            raise WrittenError("Hobbies must be text.")
        text = " ".join(item.split())[:HOBBY_MAX]
        if text and text not in out:
            out.append(text)
    return out[:HOBBIES_MAX]


def _voice(value) -> str:
    voice = str(value or "").strip()
    if voice:
        from core import tts
        try:
            engine = tts.settings()["engine"]
        except Exception:
            engine = None
        if not pals.valid_voice(voice, engine):
            raise WrittenError("Choose one of the listed voices, or the default voice.")
    return voice


def _card(name, age, gender, bio, hobbies, speech, demeanor, quirk, goal, peeve) -> dict:
    card = card_from_written(name=name, age=age, gender=gender, bio=bio, hobbies=hobbies, speech=speech,
                             demeanor=demeanor, quirk=quirk, goal=goal, peeve=peeve)
    problem = safety.card_problem(card)
    if problem:
        raise WrittenError(problem)
    return card


def _new_id(store, name) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24].strip("-") or "companion"
    for _ in range(20):
        avatar_id = f"{WRITTEN_PREFIX}{slug}-{secrets.token_hex(3)}"
        if valid_id(avatar_id) and not (store.root / avatar_id).exists():
            return avatar_id
    raise WrittenError("Could not choose a folder for the companion. Try again.", 500)


def _written(store, avatar_id) -> dict:
    record = store.get(avatar_id) if valid_id(avatar_id) else None
    if not written_here(record):
        raise WrittenError("That companion was not found.", 404)
    return record


# ---- making one and changing it -------------------------------------------------------------------------------------
def create(store, *, name, age, gender="", bio="", hobbies=(), speech="", demeanor="", quirk="", goal="", peeve="",
           voice="") -> dict:
    """Write a companion. Returns its public record. Raises WrittenError for anything the person can fix."""
    name = _name(name)
    card = _card(name, _age(age), _gender(gender), _text(bio, WRITTEN_TEXT_LIMITS["bio"], "bio"), _hobbies(hobbies),
                 _text(speech, WRITTEN_TEXT_LIMITS["speech"], "way of talking"),
                 _text(demeanor, WRITTEN_TEXT_LIMITS["demeanor"], "demeanour"),
                 _text(quirk, WRITTEN_TEXT_LIMITS["quirk"], "quirk"),
                 _text(goal, WRITTEN_TEXT_LIMITS["goal"], "goal"),
                 _text(peeve, WRITTEN_TEXT_LIMITS["peeve"], "pet peeve"))
    voice = _voice(voice)
    avatar_id = _new_id(store, name)
    folder = store.root / avatar_id
    folder.mkdir(parents=True)
    record = {
        "id": avatar_id, "name": name, "kind": "companion", "card": card,
        "source": {"type": WRITTEN_SOURCE},
        "voice": voice, "features": dict.fromkeys(FEATURES, True), "has_body": False,
        "age_check": {"ok": None, "apparent_age": None, "model": None},
        "imported": _now(),
    }
    _write_json(folder / "avatar.json", record)
    if store.active_id() is None:
        store.activate(avatar_id)
    return store.public(record)


def settings(store, avatar_id) -> dict:
    """What the write dialog shows when it opens again: the same fields, as they are now."""
    record = _written(store, avatar_id)
    card = record.get("card") or {}
    return {"id": avatar_id, "name": record.get("name") or card.get("name") or "",
            "age": card.get("age"), "age_source": card.get("age_source"), "gender": card.get("gender") or "",
            "voice": record.get("voice") or "", "features": features_of(record),
            "bio": card.get("bio") or "", "hobbies": list(card.get("hobbies") or []),
            "speech": card.get("speech") or "", "demeanor": card.get("demeanor") or "",
            "quirk": card.get("quirk") or "", "goal": card.get("goal") or "", "peeve": card.get("peeve") or ""}


def update(store, avatar_id, *, name=None, age=None, gender=None, bio=None, hobbies=None, speech=None, demeanor=None,
           quirk=None, goal=None, peeve=None, voice=None) -> dict:
    """Change a written companion's card. Only the fields that are sent change. Returns its settings."""
    record = _written(store, avatar_id)
    card = dict(record.get("card") or {})
    if name is not None:
        card["name"] = _name(name)
    if age is not None:
        card["age"] = _age(age)
    if gender is not None:
        card["gender"] = _gender(gender)
    if bio is not None:
        card["bio"] = _text(bio, WRITTEN_TEXT_LIMITS["bio"], "bio")
    if hobbies is not None:
        card["hobbies"] = _hobbies(hobbies)
    if speech is not None:
        card["speech"] = _text(speech, WRITTEN_TEXT_LIMITS["speech"], "way of talking")
    if demeanor is not None:
        card["demeanor"] = _text(demeanor, WRITTEN_TEXT_LIMITS["demeanor"], "demeanour")
    if quirk is not None:
        card["quirk"] = _text(quirk, WRITTEN_TEXT_LIMITS["quirk"], "quirk")
    if goal is not None:
        card["goal"] = _text(goal, WRITTEN_TEXT_LIMITS["goal"], "goal")
    if peeve is not None:
        card["peeve"] = _text(peeve, WRITTEN_TEXT_LIMITS["peeve"], "pet peeve")
    problem = safety.card_problem(card)
    if problem:
        raise WrittenError(problem)
    record["card"] = card
    record["name"] = card.get("name") or record.get("name")
    if voice is not None:
        record["voice"] = _voice(voice)
    _write_json(store.root / avatar_id / "avatar.json", record)
    return settings(store, avatar_id)
