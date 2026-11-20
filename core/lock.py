"""The app lock: a parent PIN and a kids view a child cannot click out of.

Kids mode (core/kids.py) makes a pal fit for a child to talk to. This is the lock around it:

  * A parent PIN, set when kids mode is set up. It is kept as a salted scrypt hash in the database, so backups
    carry it, and never in the clear or in a log. Tries are rate-limited with a progressive wait.
  * A lock screen for a browser that has not entered the PIN, with two ways in: the PIN (the whole app) or Kids
    (only kids pals). The page is templates/lock.html.
  * A kids view for that browser: only kids pals are listed and reachable, and the server refuses every other
    page and API call for that browser (core/routes/lock.py), so typing a URL does not get around it.

The PIN is set with kids mode because the lock exists for the same reason: a child with a tablet must not be
able to open an adult-capable person, change the settings or turn adult mode on.

A forgotten PIN is reset by someone who can edit the PC's files (scripts/reset_pin.py). That person can already
read every conversation and change anything; the PIN is a guardrail against a child clicking, not against
someone with the files.
"""
import hashlib
import hmac
import logging
import re
import time

from core.datastore import get_datastore

LOGGER = logging.getLogger(__name__)

PIN_KEY = "app_pin"
KIDS_ACTIVE = "kids_active"          # in the browser's session: the kids pal this kids view has open"
ATTEMPTS_KEY = "app_pin_attempts"

ALGO = "scrypt"
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
PBKDF2_ROUNDS = 600_000

MIN_PIN = 4
MAX_PIN = 8
MAX_FAILS = 5
BASE_WAIT = 10
MAX_WAIT = 5 * 60

_PIN_RE = re.compile(rf"^[0-9]{{{MIN_PIN},{MAX_PIN}}}$")


class LockError(ValueError):
    """Something the person can fix: the message is safe to show. `code` is a stable machine-readable reason."""

    def __init__(self, message, status=403, code="bad_pin", retry_after=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


# ---- the PIN itself ------------------------------------------------------------------------------------------------
def valid_pin(pin) -> bool:
    """A PIN is 4 to 8 digits. Letters and spaces are turned away so a PIN is easy to type on a phone."""
    return isinstance(pin, str) and bool(_PIN_RE.match(pin))


def _derive(pin: str, salt: bytes, algo: str, n: int, r: int, p: int, dklen: int) -> bytes:
    if algo == "scrypt":
        return hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen)
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PBKDF2_ROUNDS, dklen=dklen)


def _new_record(pin: str) -> dict:
    import os

    salt = os.urandom(16)
    try:
        digest = _derive(pin, salt, ALGO, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)
        algo, n, r, p = ALGO, SCRYPT_N, SCRYPT_R, SCRYPT_P
    except (AttributeError, ValueError):
        # A FIPS build without scrypt still gets a slow salted hash.
        algo = "pbkdf2"
        n = r = p = 0
        digest = _derive(pin, salt, algo, n, r, p, SCRYPT_DKLEN)
    return {"algo": algo, "n": n, "r": r, "p": p, "salt": salt.hex(), "hash": digest.hex(),
            "updated_at": _now()}


def _matches(pin: str, record: dict) -> bool:
    try:
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["hash"])
        algo = record.get("algo") or ALGO
        n, r, p = int(record.get("n") or 0), int(record.get("r") or 0), int(record.get("p") or 0)
        if algo == "scrypt" and n <= 0:
            n, r, p = SCRYPT_N, SCRYPT_R, SCRYPT_P
        digest = _derive(pin, salt, algo, n, r, p, len(expected) or SCRYPT_DKLEN)
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(digest, expected)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _store(store=None):
    return store or get_datastore()


def pin_state(store=None) -> dict:
    """{set, updated_at}. The hash itself is never returned."""
    raw = _store(store).get_key(PIN_KEY) or {}
    return {"set": bool(raw.get("hash") and raw.get("salt")), "updated_at": raw.get("updated_at") if raw.get("hash") else None}


def has_pin(store=None) -> bool:
    """Is a PIN set? A read failure is raised, not taken as "no PIN": that would open a locked browser, and let a new
    PIN be set without the current one."""
    return pin_state(store)["set"]


# ---- tries are rate-limited ----------------------------------------------------------------------------------------
def _attempts(store=None) -> dict:
    raw = _store(store).get_key(ATTEMPTS_KEY) or {}
    return raw if isinstance(raw, dict) else {}


def _wait_seconds(attempts: dict) -> int:
    until = attempts.get("locked_until")
    if isinstance(until, (int, float)):
        remaining = until - time.time()
        if remaining > 0:
            return int(remaining) + 1
    return 0


def _record_failure(store) -> None:
    attempts = _attempts(store)
    fails = int(attempts.get("fails") or 0) + 1
    wait = min(MAX_WAIT, BASE_WAIT * 2 ** min(10, max(0, fails - MAX_FAILS))) if fails >= MAX_FAILS else 0
    store.set_key(ATTEMPTS_KEY, {"fails": fails, "locked_until": (time.time() + wait) if wait else None})


def verify_pin(pin, store=None) -> bool:
    """Check a PIN. Raises LockError with a safe message: no_pin (409), bad_pin (403), rate_limited (429)."""
    store = _store(store)
    attempts = _attempts(store)
    wait = _wait_seconds(attempts)
    if wait:
        raise LockError(f"Too many tries. Wait {wait} second{'s' if wait != 1 else ''} and try again.",
                        429, "rate_limited", retry_after=wait)
    record = store.get_key(PIN_KEY) or {}
    if not record.get("hash") or not record.get("salt"):
        raise LockError("A grown-up needs to set a PIN first.", 409, "no_pin")
    if not valid_pin(pin) or not _matches(str(pin), record):
        _record_failure(store)
        raise LockError("That PIN is not right.", 403, "bad_pin")
    store.set_key(ATTEMPTS_KEY, {})
    return True


def set_pin(pin, current=None, store=None, *, verified=False) -> dict:
    """Set a new PIN. When one already exists, `current` must be right unless the caller already verified it."""
    if not valid_pin(pin):
        raise LockError(f"Choose {MIN_PIN} to {MAX_PIN} digits for the PIN.", 400, "bad_pin")
    store = _store(store)
    if not verified and has_pin(store):
        verify_pin(current, store)
    store.set_key(PIN_KEY, _new_record(str(pin)))
    store.set_key(ATTEMPTS_KEY, {})
    return pin_state(store)


def clear_pin(store=None) -> None:
    """For scripts/reset_pin.py: remove the PIN and the try counter, leaving kids mode's household switch alone."""
    store = _store(store)
    store.delete_key(PIN_KEY)
    store.delete_key(ATTEMPTS_KEY)


# ---- what this browser is allowed to see ---------------------------------------------------------------------------
def in_kids_view() -> bool:
    try:
        from flask import session
        return session.get("kids_view") is True
    except RuntimeError:
        return False


def is_unlocked() -> bool:
    try:
        from flask import session
        return session.get("app_unlocked") is True
    except RuntimeError:
        return False


def is_locked() -> bool:
    """A browser with no way in: a PIN exists and this browser has neither entered it nor chosen Kids.

    When the PIN cannot be read, a browser that has not been let in stays locked."""
    if in_kids_view() or is_unlocked():
        return False
    try:
        return has_pin()
    except Exception:
        LOGGER.exception("The PIN could not be read, so this browser stays locked")
        return True


def enter_kids_view() -> None:
    from flask import session
    session["kids_view"] = True
    session.pop("app_unlocked", None)


def leave_kids_view() -> None:
    from flask import session
    session.pop("kids_view", None)
    session.pop(KIDS_ACTIVE, None)


def unlock() -> None:
    from flask import session
    session["app_unlocked"] = True
    session.pop("kids_view", None)
    session.pop(KIDS_ACTIVE, None)


def lock_browser() -> None:
    from flask import session
    session.pop("kids_view", None)
    session.pop("app_unlocked", None)
    session.pop(KIDS_ACTIVE, None)


def open_kids_pal(avatar_id) -> dict:
    """Open a kids pal for this browser's kids view only: it becomes this browser's current pal, and the household's
    current person (what every other browser opens with) is left alone."""
    from flask import session
    from core import companions
    if not avatar_is_kids(avatar_id):
        raise companions.CompanionError("That pal is not in kids mode.")
    opened = companions.open_companion(avatar_id, activate=False)
    session[KIDS_ACTIVE] = avatar_id
    return opened


def state() -> dict:
    """What /api/lock reports. Never the PIN, its hash or the try counter."""
    from core import kids

    return {"pin_set": has_pin(), "kids_enabled": kids.enabled(),
            "kids_view": in_kids_view(), "unlocked": is_unlocked(), "locked": is_locked(),
            "kids_pals": [{"id": p["id"], "name": p.get("name") or p["id"]} for p in kids_pals()]}


# ---- who a kids view may reach -------------------------------------------------------------------------------------
def kids_pals() -> list:
    """Every pal in kids mode, most recently added last. A read failure means none, which fails closed."""
    from core import kids
    from core.companion import get_services
    try:
        records = get_services().avatars.list()
    except Exception:
        LOGGER.exception("The people could not be read, so the kids view reaches no pal")
        return []
    return [r for r in records if kids.is_on(r)]


def avatar_is_kids(avatar_id) -> bool:
    from core import kids
    from core.companion import get_services
    try:
        return kids.is_on(get_services().avatars.get(avatar_id))
    except Exception:
        LOGGER.exception("A person could not be read, so the kids view may not reach them")
        return False


def ensure_kids_active() -> dict:
    """The kids pal this browser's kids view opens with, without opening an adult to check it.

    It is kept in the browser's own session, never in the household's current person: a tablet in the kids view must
    not change who a grown-up's phone opens with. Returns the same shape as core.companions.current(): {avatar,
    session_id}. With no kids pal it returns nobody, so the chat page shows its empty state rather than an adult.
    """
    from flask import session
    from core.companion import get_services
    pals = kids_pals()
    if not pals:
        return {"avatar": None, "session_id": None}
    ids = [p["id"] for p in pals]
    chosen = session.get(KIDS_ACTIVE)
    if chosen not in ids:
        try:
            active_id = get_services().avatars.active_id()
        except Exception:
            active_id = None
        chosen = active_id if active_id in ids else ids[0]
    try:
        opened = open_kids_pal(chosen)
    except Exception:
        LOGGER.exception("The kids pal could not be opened")
        return {"avatar": None, "session_id": None}
    return {"avatar": opened, "session_id": opened.get("session_id")}


def session_is_kids(session_id) -> bool:
    """True when this chat belongs to a pal in kids mode.

    The chat's own avatar is used, not `companions.avatar_for_session`: an older chat with no avatar falls back to
    whoever is active, and a child must not be handed an adult's old conversation because a kids pal happens to be open.
    """
    try:
        session_id = int(session_id)
        info = get_datastore().get_session(session_id)
    except (TypeError, ValueError, AttributeError):
        return False
    except Exception:
        LOGGER.exception("A chat could not be read, so the kids view may not reach it")
        return False
    if not info or not getattr(info, "avatar_id", None):
        return False
    return avatar_is_kids(info.avatar_id)


def request_is_kids(request_id) -> bool:
    """True when a durable chat request belongs to a kids chat."""
    from core.routes.core_api import get_turn_service
    try:
        row = get_turn_service().get(request_id)
    except Exception:
        LOGGER.exception("A chat request could not be read, so the kids view may not reach it")
        return False
    return bool(row) and session_is_kids(row.get("session_id"))


def message_is_kids(message_id) -> bool:
    try:
        record = get_datastore().get_message(message_id)
    except Exception:
        LOGGER.exception("A message could not be read, so the kids view may not reach it")
        return False
    return record is not None and session_is_kids(record.session_id)
