"""Altera has one version number, shown in Settings and in the log, with a changelog section for it."""
import re
from pathlib import Path

from core.version import VERSION
from test_companion_api import tmp_store, world  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent


def test_the_version_is_a_plain_three_part_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION)


def test_the_changelog_has_a_section_for_this_version():
    headings = re.findall(r"^## (\d+\.\d+\.\d+) ", (ROOT / "CHANGELOG.md").read_text("utf-8"), flags=re.MULTILINE)
    assert headings and headings[0] == VERSION                    # the newest section is this version


def test_settings_shows_the_version(world):
    from core.app import create_app
    page = create_app(autostart=False).test_client().get("/")
    assert page.status_code == 200
    assert f'id="appVersion">Altera {VERSION}</p>' in page.get_data(as_text=True)
