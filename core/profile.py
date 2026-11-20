"""
Altera — User Profile Persistence (Session 5 of Architecture Refactor)

Owns all user_profile.yaml read/write operations and onboarding state management.
altera.py routes call these functions instead of inline YAML + DB logic.
"""

import logging
import os
import yaml
from copy import deepcopy
from pathlib import Path
from core.paths import root

from core.datastore import get_datastore

LOGGER = logging.getLogger(__name__)


# ============================================================
# YAML Profile Persistence
# ============================================================

PROFILE_PATH = root() / 'user_profile.yaml'
DEFAULT_PROFILE = {
    "name": "",
    "hobbies": [],
    "preferences": {},
    "mood_history": [],
    "facts": []
}


def load_user_profile() -> dict:
    """Load the user profile from YAML or return defaults."""
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, encoding='utf-8') as f:
            data = yaml.safe_load(f)
            if isinstance(data, dict):
                # Merge with defaults to ensure all keys exist
                merged = deepcopy(DEFAULT_PROFILE)
                for key in DEFAULT_PROFILE:
                    if key in data and data[key] is not None:
                        merged[key] = data[key]
                # Settings saved from the app (config: phase4: ...) live in this file too. They used to be dropped here, so a
                # saved setting could never be read back and the next save wiped it.
                merged.update({key: value for key, value in data.items() if key not in DEFAULT_PROFILE})
                return merged
    return deepcopy(DEFAULT_PROFILE)


def save_user_profile(profile: dict) -> None:
    """Save the user profile to YAML."""
    from core.config import _write_yaml
    path = Path(PROFILE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml(path, profile)


def proactive_settings() -> dict:
    """The check-in settings, {'enabled': bool, 'interval_hours': int}: what was saved from Settings, else config.yaml's phase4 block.

    The Settings page and the check-in code both read this, so the switch does what it says."""
    from core.config import get_config
    defaults = get_config().get("phase4") or {}
    saved = ((load_user_profile().get("config") or {}).get("phase4")) or {}
    enabled = saved.get("proactive_enabled", defaults.get("proactive_enabled", True))
    hours = saved.get("proactive_interval_hours", defaults.get("proactive_interval_hours", 6))
    try:
        hours = max(1, min(48, int(hours)))
    except (TypeError, ValueError):
        hours = 6
    return {"enabled": bool(enabled), "interval_hours": hours}


def living_portrait_enabled() -> bool:
    """Whether her picture comes alive (blinks, talks, changes expression) when she has a living portrait. On unless it was
    switched off in Settings."""
    saved = (load_user_profile().get("config") or {}).get("companion") or {}
    return bool(saved.get("living_portrait", True))


def set_living_portrait(enabled: bool) -> None:
    profile = load_user_profile()
    profile.setdefault("config", {}).setdefault("companion", {})["living_portrait"] = bool(enabled)
    save_user_profile(profile)


# ============================================================
# Onboarding State (stored in SQLite user_profile table)
# ============================================================

ONBOARDING_KEY = "onboarding_complete"


def is_onboarding_complete() -> bool:
    """Check if user has completed onboarding."""
    try:
        store = get_datastore()
        row = store.get_key(ONBOARDING_KEY)
        return bool(row and str(row.get("value", "")) == "true")
    except Exception:
        return False


def complete_onboarding() -> None:
    """Mark onboarding as complete."""
    try:
        store = get_datastore()
        existing = store.get_key(ONBOARDING_KEY)
        if existing:
            store.set_key(ONBOARDING_KEY, {"value": "true"})
        else:
            store.set_key(ONBOARDING_KEY, {"key": ONBOARDING_KEY, "value": "true"})
    except Exception:
        LOGGER.exception("Error marking complete")
