"""Remove a forgotten parent PIN.

Standard library only, so it runs without Altera's virtual environment. It deletes the PIN and its try counter from
the instance database and leaves everything else alone: kids mode's household switch, every pal's own kids switch, and
all conversations.

Someone who can run this can already read every conversation and change any setting on this PC. The PIN is a guardrail
against a child clicking, not against someone with access to the files.

    python scripts/reset_pin.py --home "C:\\path\\to\\altera-instance"
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import sys

CODE_ROOT = Path(__file__).resolve().parents[1]
PIN_KEYS = ("app_pin", "app_pin_attempts")


def instance_home(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("ALTERA_HOME")
    return Path(env).expanduser().resolve() if env else CODE_ROOT


def reset(home: Path) -> int:
    """Delete the PIN keys from the instance database. Returns how many keys were removed."""
    database = home / "data" / "conversations.db"
    if not database.is_file():
        raise FileNotFoundError(f"No Altera database at {database}. Pass --home with the instance directory.")
    connection = sqlite3.connect(str(database))
    try:
        with connection:
            cursor = connection.execute(
                "DELETE FROM user_profile WHERE key IN (?, ?)", PIN_KEYS)
        return cursor.rowcount
    finally:
        connection.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Remove a forgotten parent PIN. Kids mode and every chat are left alone.")
    parser.add_argument("--home", help="Instance directory (otherwise ALTERA_HOME or the source checkout).")
    parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")
    args = parser.parse_args(argv)
    home = instance_home(args.home)
    if not args.yes:
        try:
            answer = input(f"Remove the parent PIN from {home}? This cannot be undone. Type yes to continue: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"yes", "y"}:
            print("Left the PIN alone.")
            return 1
    try:
        removed = reset(home)
    except (OSError, sqlite3.Error) as error:
        print(f"Could not remove the PIN: {error}", file=sys.stderr)
        return 1
    print(f"Removed {removed} PIN record(s) from {home}. Kids mode itself is unchanged.")
    print("Start Altera and set a new PIN in Home > Settings > About kids mode.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
