"""Altera's environment variables.

Each setting is read from ALTERA_<NAME>. Nothing else is imported here: core/paths.py reads the instance folder
through this module before anything else loads.
"""
import os
import re

PREFIX = 'ALTERA_'
# Provider keys: ALTERA_OPENROUTER_API_KEY and so on.
API_KEY_NAME = re.compile(r'ALTERA_[A-Z0-9_]+_API_KEY\Z')


def get(name, default=None, environ=None):
    """ALTERA_<name>, else default. An empty value counts as unset."""
    environ = os.environ if environ is None else environ
    return environ.get(PREFIX + name) or default


def is_api_key(name):
    """True for a provider key variable; these never reach child processes."""
    return bool(API_KEY_NAME.match(name))
