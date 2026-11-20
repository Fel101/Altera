"""
Altera — how replies are sampled: the app-wide settings and each person's own temperature and reply length.

Temperature and reply length (max_tokens) are always sent. The other sampler settings (top_p, min_p, repetition, presence
and frequency penalty) are optional: each is sent only when it has been set, because providers differ in what they accept.
They live in config.yaml under llm.sampling and are changed in Settings.

A person can have their own temperature and reply length, kept in companion.json in their folder (so a backup carries it
and removing them removes it). A blank value means the app-wide one. These settings are for chat replies only: the
background helpers (memories, notes, check-ins, the picture age check) keep their own fixed, low temperatures.
"""
import logging
import math

LOGGER = logging.getLogger(__name__)

# The settings that always have a value, and what is used when the config has none (core/llm.py's defaults).
TEMPERATURE_DEFAULT = 0.85
MAX_TOKENS_DEFAULT = 8192
PERSON_KEYS = ("temperature", "max_tokens")
OPTIONAL_KEYS = ("top_p", "min_p", "repetition_penalty", "presence_penalty", "frequency_penalty")

# (lowest, highest, whole number)
LIMITS = {
    "temperature": (0.0, 2.0, False),
    "max_tokens": (64, 32768, True),
    "top_p": (0.01, 1.0, False),
    "min_p": (0.0, 1.0, False),
    "repetition_penalty": (0.5, 2.0, False),
    "presence_penalty": (-2.0, 2.0, False),
    "frequency_penalty": (-2.0, 2.0, False),
}
LABELS = {"temperature": "Temperature", "max_tokens": "Reply length", "top_p": "Top P", "min_p": "Min P",
          "repetition_penalty": "Repetition penalty", "presence_penalty": "Presence penalty",
          "frequency_penalty": "Frequency penalty"}

# Settings outside the OpenAI API go in the request body as they are. OpenAI's own API refuses them, so they are not sent
# there. LM Studio (like llama.cpp) calls the repetition setting repeat_penalty; vLLM and OpenRouter call it
# repetition_penalty, and so does a custom server.
EXTRA_KEYS = ("min_p", "repetition_penalty")
NOT_SENT = {"openai": EXTRA_KEYS}
REPETITION_NAME = {"lmstudio": "repeat_penalty", "ollama": "repeat_penalty"}


class SamplingError(ValueError):
    """Something the person can fix: the message is safe to show."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _value(key, value):
    """The value checked against its limits. Raises SamplingError."""
    low, high, whole = LIMITS[key]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise SamplingError(f"{LABELS[key]} must be a number.")
    try:
        number = float(value)
    except ValueError:
        raise SamplingError(f"{LABELS[key]} must be a number.") from None
    if not math.isfinite(number):
        raise SamplingError(f"{LABELS[key]} must be a number.")
    if whole:
        if number != int(number):
            raise SamplingError(f"{LABELS[key]} must be a whole number.")
        number = int(number)
    if not low <= number <= high:
        raise SamplingError(f"{LABELS[key]} must be from {low:g} to {high:g}.")
    return number if whole else round(number, 3)


def _lenient(key, value):
    """A stored value, or None when it is missing or no longer valid (a hand-edited file must not break replies)."""
    if value is None or value == "":
        return None
    try:
        return _value(key, value)
    except SamplingError:
        return None


# ---- app-wide ---------------------------------------------------------------------------------------------------------
def app_settings(config: dict) -> dict:
    """{temperature, max_tokens, sampling: {only the optional settings that are set}} from the config."""
    llm = (config or {}).get("llm") or {}
    chosen = llm.get("sampling") if isinstance(llm.get("sampling"), dict) else {}
    optional = {key: _lenient(key, chosen.get(key)) for key in OPTIONAL_KEYS}
    temperature = _lenient("temperature", llm.get("temperature"))
    max_tokens = _lenient("max_tokens", llm.get("max_tokens"))
    return {"temperature": TEMPERATURE_DEFAULT if temperature is None else temperature,
            "max_tokens": MAX_TOKENS_DEFAULT if max_tokens is None else max_tokens,
            "sampling": {key: value for key, value in optional.items() if value is not None}}


def apply_app_changes(llm: dict, changes: dict) -> dict:
    """The llm block with these changes: temperature and max_tokens must be numbers; an optional setting sent as null or ''
    is cleared. Raises SamplingError, and then nothing changes."""
    if not isinstance(changes, dict) or not changes or not set(changes) <= set(PERSON_KEYS + OPTIONAL_KEYS):
        raise SamplingError("Send temperature, max_tokens or the optional sampler settings only.")
    fresh = dict(llm or {})
    sampling = dict(fresh.get("sampling") or {}) if isinstance(fresh.get("sampling"), dict) else {}
    for key, value in changes.items():
        if key in PERSON_KEYS:
            fresh[key] = _value(key, value)
        elif value is None or value == "":
            sampling.pop(key, None)
        else:
            sampling[key] = _value(key, value)
    fresh["sampling"] = {key: sampling[key] for key in OPTIONAL_KEYS if key in sampling}
    return fresh


def request_options(provider_key: str, sampling: dict) -> tuple:
    """(keyword arguments, extra_body) that send these optional settings to this provider. Only what is set is sent."""
    kwargs, extra = {}, {}
    skipped = NOT_SENT.get(provider_key or "", ())
    for key in OPTIONAL_KEYS:
        value = _lenient(key, (sampling or {}).get(key))
        if value is None or key in skipped:
            continue
        if key in EXTRA_KEYS:
            extra[REPETITION_NAME.get(provider_key, key) if key == "repetition_penalty" else key] = value
        else:
            kwargs[key] = value
    return kwargs, extra


def not_sent(provider_key: str) -> list:
    """The optional settings this provider is never sent."""
    return list(NOT_SENT.get(provider_key or "", ()))


def limits() -> dict:
    return {key: {"min": low, "max": high, "whole": whole, "label": LABELS[key]} for key, (low, high, whole) in LIMITS.items()}


# ---- a person's own ---------------------------------------------------------------------------------------------------
def person_settings(store, avatar_id) -> dict:
    """{temperature, max_tokens}: this person's own values, None where the app-wide one is used."""
    chosen = (store.state(avatar_id) or {}).get("replies") if avatar_id else None
    chosen = chosen if isinstance(chosen, dict) else {}
    return {key: _lenient(key, chosen.get(key)) for key in PERSON_KEYS}


def set_person(store, avatar_id, changes) -> dict:
    """Change this person's own temperature and reply length. null or '' means the app-wide value again."""
    if not store.get(avatar_id):
        raise SamplingError("That person was not found.", 404)
    if not isinstance(changes, dict) or not changes or not set(changes) <= set(PERSON_KEYS):
        raise SamplingError("Send temperature and max_tokens only.")
    current = person_settings(store, avatar_id)
    for key, value in changes.items():
        current[key] = None if value is None or value == "" else _value(key, value)
    store.update_state(avatar_id, replies={key: value for key, value in current.items() if value is not None})
    return person_settings(store, avatar_id)


def for_reply(store, avatar_id) -> dict:
    """The person's own values that are set, to lay over the app-wide ones for one reply ({} when there are none)."""
    if not avatar_id:
        return {}
    try:
        own = person_settings(store, avatar_id)
    except Exception:
        LOGGER.exception("%s's own reply settings could not be read, so the app's are used", avatar_id)
        return {}
    return {key: value for key, value in own.items() if value is not None}


def describe(config: dict, provider_key: str | None = None) -> dict:
    """What Settings shows."""
    return {**app_settings(config), "provider": provider_key or "", "not_sent": not_sent(provider_key), "limits": limits()}
