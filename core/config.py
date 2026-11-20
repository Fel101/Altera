"""
Altera — the one place config.yaml is read and written.

config.yaml used to be opened in thirteen places: some at import time (so they froze whatever was on disk when the
module first loaded), some on every request, one on every streamed chat turn, and three of them with a bare relative
path, which only works when Altera is started from its own directory. Because the provider switcher rewrites the file at
runtime, the import-time copies then disagreed with it for the rest of the process.

Everything now goes through get_config(). It reads the file once, re-reads it when the file changes on disk, and
save_config() writes it atomically and refreshes the cache — so a change made through the UI is visible everywhere at
once, whoever asks next.

This module deliberately imports nothing from the rest of `core`: it sits at the bottom of the dependency graph so
anything may use it without a circular import.
"""

import os
import threading
import time
from pathlib import Path

import yaml
from core import env
from core.paths import DEFAULT_ROOT

ROOT = DEFAULT_ROOT
CONFIG_PATH = ROOT / "config.yaml"
# Machine-specific overrides and API keys, merged over config.yaml by get_config(). Both instance files are ignored;
# config.example.yaml is the portable starter used only when config.yaml is missing.
LOCAL_CONFIG_PATH = ROOT / "config.local.yaml"
PERSONALITY_PATH = ROOT / "personality.yaml"

# Field name that holds a credential wherever it appears in the tree.
SECRET_FIELD = "api_key"
# Values of that field which are not credentials: a local server ignores the key but the OpenAI client insists on
# one, so these are placeholders. Moving them out of config.yaml would leave a fresh clone unable to start.
PLACEHOLDER_KEYS = {"lm-studio", "ollama", "vllm", "none", "local", "test", "key", "sk-none", "not-needed"}

_lock = threading.RLock()
_cache = {}          # path -> {"mtime": int, "size": int, "data": dict}

# A file modified this recently is always read again rather than trusted to the mtime check (see _read).
RACY_WINDOW_NS = 2_000_000_000


# Default LLM settings (mirrors altera.py defaults; overridden by config.yaml at startup)
LLM_BASE_URL = "http://localhost:1234/v1"
LLM_API_KEY = "lm-studio"
# There is deliberately no MODEL here. LM Studio loads whatever model a request names (and unloads the one that was loaded), so
# the model a request names must be the one that is answering chat: use core.llm.get_client(), or core.llm.ask() for a one-off
# question, and never a name of your own.


def _read(path: Path) -> dict:
    """The parsed file, re-read when it has changed. A missing or unreadable file is an empty dict."""
    key = str(path)
    try:
        stat = path.stat()
        mtime, size = stat.st_mtime_ns, stat.st_size
    except OSError:
        with _lock:
            _cache.pop(key, None)
        return {}

    with _lock:
        cached = _cache.get(key)
        # A file whose mtime has not moved is usually unchanged -- but a filesystem timestamp is only as fine as the
        # clock behind it, so a write that lands in the same tick as the previous read is invisible to that check.
        # Anything modified in the last few seconds is therefore read again regardless; config.yaml changes rarely,
        # so the cache still does its job the rest of the time.
        if (cached and cached["mtime"] == mtime and cached["size"] == size
                and time.time_ns() - mtime > RACY_WINDOW_NS):
            return cached["data"]

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        data = {}

    with _lock:
        _cache[key] = {"mtime": mtime, "size": size, "data": data}
    return data


def _deep_merge(base: dict, override: dict) -> dict:
    """`override` laid over `base`, a level at a time. Neither input is modified."""
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _env_name(section: str) -> str:
    """ALTERA_OPENROUTER_API_KEY for the provider named 'openrouter'."""
    safe = "".join(ch if ch.isalnum() else "_" for ch in str(section)).upper()
    return f"{env.PREFIX}{safe}_API_KEY"


def _apply_key_env(data: dict) -> dict:
    """Let the environment supply any api_key, so a credential need never be written to a file at all.

    Each provider's key is read from ALTERA_<PROVIDER>_API_KEY -- ALTERA_OPENROUTER_API_KEY, ALTERA_SERPAPI_API_KEY and so on
    -- which .env supplies (altera.py calls load_dotenv). An unset variable changes nothing.
    """
    def walk(node):
        if not isinstance(node, dict):
            return node
        result = {}
        for key, value in node.items():
            if isinstance(value, dict):
                if SECRET_FIELD in value:
                    from_env = env.get(_env_name(key).removeprefix(env.PREFIX))
                    value = {**value, SECRET_FIELD: from_env} if from_env else value
                result[key] = walk(value)
            else:
                result[key] = value
        return result

    return walk(data)


def get_config() -> dict:
    """config.yaml, with config.local.yaml laid over it and any ALTERA_*_API_KEY from the environment on top.

    Cheap to call: each file is only re-read when it has changed on disk.
    """
    base = _read(CONFIG_PATH)
    local = _read(LOCAL_CONFIG_PATH)
    merged = _deep_merge(base, local) if local else base
    return _apply_key_env(merged)


def get_personality() -> dict:
    """The parsed personality.yaml."""
    return _read(PERSONALITY_PATH)


def reload_config() -> dict:
    """Forget what was cached and read the config files again."""
    with _lock:
        _cache.pop(str(CONFIG_PATH), None)
        _cache.pop(str(LOCAL_CONFIG_PATH), None)
    return get_config()


def _split_secrets(data: dict):
    """(public, secrets): the same tree twice, with every api_key blanked in the first and kept in the second."""
    public, secrets = {}, {}
    for key, value in (data or {}).items():
        if key == "remote_access":
            # The owner's login and this PC's hostname belong in the ignored machine configuration.
            secrets[key] = value
        elif isinstance(value, dict):
            sub_public, sub_secrets = _split_secrets(value)
            public[key] = sub_public
            if sub_secrets:
                secrets[key] = sub_secrets
        elif key == SECRET_FIELD and value and str(value).strip().lower() not in PLACEHOLDER_KEYS:
            public[key] = ""
            secrets[key] = value
        else:
            public[key] = value
    return public, secrets


def _write_yaml(path: Path, data: dict) -> None:
    """Write a YAML file atomically: an interrupted write must not leave a half-written config behind."""
    import tempfile

    fd, tmp_path = tempfile.mkstemp(prefix=".config.", suffix=".yaml.tmp", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def save_config(data: dict) -> None:
    """Persist local preferences separately from credentials and remote-access identity.

    Non-placeholder API keys go to config.local.yaml and are written blank to config.yaml. Both are git-ignored
    instance files; get_config() merges the overrides back in. The portable config.example.yaml is never rewritten.
    """
    public, secrets = _split_secrets(data)
    with _lock:
        _write_yaml(CONFIG_PATH, public)
        if secrets:
            existing = _read(LOCAL_CONFIG_PATH)
            _write_yaml(LOCAL_CONFIG_PATH, _deep_merge(existing, secrets))
            _cache.pop(str(LOCAL_CONFIG_PATH), None)
        _cache.pop(str(CONFIG_PATH), None)


def reset_cache() -> None:
    """Drop every cached file (tests, and anything that moves the files under us)."""
    with _lock:
        _cache.clear()


SECRET_KEY_PATH = ROOT / "data" / "secret_key"


def get_secret_key() -> str:
    """The key Flask signs session cookies with, stable across restarts.

    It used to be os.urandom() at import, so every restart invalidated every session cookie and quietly signed the
    person out of their own companion. ALTERA_SECRET_KEY wins if it is set; otherwise one is generated once and kept in
    data/ (git-ignored, alongside the conversations it protects).
    """
    from_env = env.get("SECRET_KEY")
    if from_env:
        return from_env

    with _lock:
        try:
            existing = SECRET_KEY_PATH.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass

        import secrets

        key = secrets.token_hex(32)
        try:
            SECRET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
            SECRET_KEY_PATH.write_text(key, encoding="utf-8")
            try:
                os.chmod(SECRET_KEY_PATH, 0o600)       # best effort; a no-op on Windows
            except OSError:
                pass
        except OSError as error:
            raise RuntimeError('Altera cannot save its session key. Choose a writable ALTERA_HOME or set ALTERA_SECRET_KEY.') from error
        return key


# LLM client factory - creates a fresh OpenAI-compatible client on demand
def get_llm_client():
    """Return an OpenAI-compatible client configured for LM Studio."""
    from openai import OpenAI
    return OpenAI(
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY,
    )
