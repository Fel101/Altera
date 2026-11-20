"""
Altera — Adult mode: the opt-in, the four levels and the editable tier tables (content/adult_tiers.yaml).

The checkbox is off by default. Turning it on needs an explicit "I am 18 or older" confirmation, which is stored with a
timestamp. What each level allows in chat and in pictures comes from the YAML file; what is never allowed lives in
core/safety.py and is not affected by anything here.
"""
from core.paths import DEFAULT_ROOT
import datetime
import random
import re
import threading
from typing import Optional

import yaml

from core.datastore import get_datastore

LEVELS = ("off", "romantic", "suggestive", "explicit")
STATE_KEY = "adult_mode"
TIERS_PATH = DEFAULT_ROOT / "content" / "adult_tiers.yaml"

HARD_RULES = (
    "Rules that never change, whatever the user asks: you are an adult; never sexualise anyone under 18 or anyone who is "
    "described or implied to be young, childlike or at school; never involve a real person (a celebrity, a public figure "
    "or someone the user knows); nothing non-consensual, coerced or violent. If the user steers there, decline in your own "
    "voice and change direction."
)


# ============================================================
# Tier tables (content/adult_tiers.yaml, re-read when the file changes)
# ============================================================

_cache = {"mtime": None, "data": {}, "needs": {}}
_cache_lock = threading.Lock()


def _compile_terms(terms) -> Optional["re.Pattern"]:
    alternatives = []
    for term in terms or []:
        words = str(term).strip().split()
        if words:
            alternatives.append(r"\s+".join(re.escape(w) for w in words))
    if not alternatives:
        return None
    return re.compile(r"\b(?:" + "|".join(alternatives) + r")(?:s|es)?\b", re.I)


def tiers() -> dict:
    """The parsed tier tables. Raises FileNotFoundError if content/adult_tiers.yaml is missing."""
    with _cache_lock:
        mtime = TIERS_PATH.stat().st_mtime
        if _cache["mtime"] != mtime:
            with open(TIERS_PATH, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            needs = (data.get("photo") or {}).get("needs") or {}
            _cache.update(mtime=mtime, data=data, needs={t: _compile_terms(needs.get(t)) for t in LEVELS[1:]})
        return _cache["data"]


def level_index(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


def level_at_least(level: str, minimum: str) -> bool:
    return level_index(level) >= level_index(minimum)


def label(level: str) -> str:
    return ((tiers().get("levels") or {}).get(level) or {}).get("label", level.title())


def describe_levels() -> list:
    info = tiers().get("levels") or {}
    return [{"id": lv, "label": (info.get(lv) or {}).get("label", lv.title()), "summary": (info.get(lv) or {}).get("summary", "")}
            for lv in LEVELS]


# ============================================================
# Opt-in state (stored in the private SQLite key-value table, never in a file that could be shared)
# ============================================================

def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def get_state(store=None) -> dict:
    """{enabled, level, confirmed_at, privacy_blur, effective_level}. 'enabled' is false until 18+ was confirmed."""
    store = store or get_datastore()
    raw = store.get_key(STATE_KEY) or {}
    enabled = bool(raw.get("enabled")) and bool(raw.get("confirmed_at"))
    level = raw.get("level") if raw.get("level") in LEVELS[1:] else "romantic"
    return {
        "enabled": enabled,
        "level": level,
        "confirmed_at": raw.get("confirmed_at"),
        "privacy_blur": raw.get("privacy_blur", True) is not False,
        "effective_level": level if enabled else "off",
    }


def effective_level(store=None) -> str:
    return get_state(store)["effective_level"]


def update_state(changes: dict, store=None):
    """Apply {enabled, level, confirm_adult, privacy_blur}. Returns (state, error_message_or_None)."""
    store = store or get_datastore()
    raw = dict(store.get_key(STATE_KEY) or {})
    changes = changes or {}

    if "level" in changes:
        if changes["level"] not in LEVELS[1:]:
            return get_state(store), f"Unknown level: {changes['level']!r}. Choose one of: {', '.join(LEVELS[1:])}."
        raw["level"] = changes["level"]
    if "privacy_blur" in changes:
        raw["privacy_blur"] = bool(changes["privacy_blur"])
    if "enabled" in changes:
        if changes["enabled"] is True:
            if not raw.get("confirmed_at"):
                if changes.get("confirm_adult") is not True:
                    return get_state(store), "Confirm that you are 18 or older to turn adult mode on."
                raw["confirmed_at"] = _now()
            raw["enabled"] = True
        else:
            raw["enabled"] = False

    store.set_key(STATE_KEY, raw)
    return get_state(store), None


# ============================================================
# What each level means
# ============================================================

def chat_instruction(level: str) -> str:
    """The system-prompt paragraph for a level. Every level above 'off' also carries the hard rules."""
    text = ((tiers().get("chat") or {}).get(level) or "").strip()
    if level != "off":
        text = f"{text}\n{HARD_RULES}"
    return text


def photo_rule(level: str) -> str:
    return (((tiers().get("photo") or {}).get("rule") or {}).get(level) or "").strip()


def photo_mood(level: str) -> str:
    return (((tiers().get("photo") or {}).get("mood") or {}).get(level) or "").strip()


def default_scenes(level: str) -> list:
    """Scenes for 'send me a pic'. An empty explicit list falls back to the suggestive one, then to the level below."""
    scenes = (tiers().get("photo") or {}).get("scenes") or {}
    idx = level_index(level)
    while idx > 0 and not scenes.get(LEVELS[idx]):
        idx -= 1
    return list(scenes.get(LEVELS[idx]) or [])


def pick_scene(level: str, rng: random.Random | None = None) -> str:
    scenes = default_scenes(level)
    return (rng or random).choice(scenes) if scenes else "a casual selfie, smiling at the camera"


def tier_problem(text: str, level: str) -> dict | None:
    """A request that uses words from a higher level than the current one: {required, term, message}, else None. While
    adult mode is off the message does not say that it exists: it stays out of sight until someone goes looking."""
    tiers()
    have = level_index(level)
    for tier in reversed(LEVELS[1:]):               # report the highest level the text needs
        if have >= level_index(tier):
            break
        pattern = _cache["needs"].get(tier)
        match = pattern.search(text or "") if pattern else None
        if match:
            message = (f'"{match.group(0)}" needs adult mode set to {label(tier)} or higher.' if have
                       else f'Altera does not make pictures of "{match.group(0)}".')
            return {"required": tier, "term": match.group(0), "message": message}
    return None
