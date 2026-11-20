"""Altera's living portrait: copying NPC Forge's frames, the age check on every frame, the portrait they must belong to, the
setting and the routes. No GPU, model or network needed; everything is in temporary folders."""
import hashlib
import json
import types

import pytest

from core import companion, profile
from core.avatars import AvatarStore
from core.living import LivingStore
from core.photos import PhotoService
from test_blueprints import create_app
from test_companion_images import FakeBackend, FakeChecker, PNG, make_forge

NPC = "mara-bell-47ab97"


def make_living(forge, frames=("blink", "talking", "happy"), portrait=None, npc_id=NPC):
    """NPC Forge's living/ folder for a character, made from its portrait (or from `portrait` bytes, to fake a mismatch)."""
    d = forge / npc_id / "living"
    d.mkdir(parents=True, exist_ok=True)
    base = portrait if portrait is not None else (forge / npc_id / "portrait.png").read_bytes()
    for f in frames:
        (d / f"{f}.png").write_bytes(PNG + f"frame-{f}".encode())
    manifest = {"version": 1, "portrait": {"created": "p1", "sha256": hashlib.sha256(base).hexdigest()},
                "frames": {f: {"file": f"living/{f}.png"} for f in frames}, "made": "now"}
    (d / "living.json").write_text(json.dumps(manifest), "utf-8")
    return d


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "PROFILE_PATH", str(tmp_path / "user_profile.yaml"))
    forge = tmp_path / "forge"
    forge.mkdir()
    make_forge(forge)
    avatars = AvatarStore(tmp_path / "avatars", forge)
    avatars.import_from_forge(NPC)
    checker = FakeChecker(ok=True)
    return types.SimpleNamespace(forge=forge, avatars=avatars, checker=checker,
                                 living=LivingStore(avatars, checker, sync=True), tmp=tmp_path)


def test_without_a_living_portrait_she_has_none_and_says_where_to_make_it(world):
    world.living.sync(NPC)
    status = world.living.status(NPC)
    assert status["available"] is False and "NPC Forge" in status["reason"]


def test_frames_are_copied_age_checked_and_served_only_while_they_match_her_portrait(world):
    make_living(world.forge)
    result = world.living.sync(NPC)
    assert result["imported"] == 3 and world.checker.calls == 3
    status = world.living.status(NPC)
    assert status["frames"] == ["blink", "happy", "talking"] and status["checked"] == 3
    assert world.living.frame_path(NPC, "blink").read_bytes() == PNG + b"frame-blink"
    assert world.living.sync(NPC)["kept"] == 3 and world.checker.calls == 3          # unchanged frames are not checked again
    # a new portrait in Altera (imported again) no longer matches the frames: they are not shown
    (world.avatars.image_path(NPC, "portrait")).write_bytes(PNG + b"another portrait")
    assert world.living.frame_path(NPC, "blink") is None and world.living.status(NPC)["available"] is False


def test_frames_made_from_another_portrait_are_not_taken(world):
    make_living(world.forge, portrait=PNG + b"an older portrait")
    world.living.sync(NPC)
    status = world.living.status(NPC)
    assert status["available"] is False and "different portrait" in status["reason"]


def test_a_frame_that_fails_the_age_check_is_refused_and_not_checked_again_until_it_changes(world):
    make_living(world.forge, frames=("blink",))
    world.checker.result["ok"], world.checker.result["apparent_age"] = False, 15
    assert world.living.sync(NPC)["refused"] == 1
    assert world.living.frame_path(NPC, "blink") is None and world.living.status(NPC)["refused"] == ["blink"]
    world.living.sync(NPC)
    assert world.checker.calls == 1
    world.checker.result["ok"] = True
    (world.forge / NPC / "living" / "blink.png").write_bytes(PNG + b"redone")      # redone in NPC Forge: checked again
    assert world.living.sync(NPC)["imported"] == 1 and world.living.frame_path(NPC, "blink")


def test_an_unchecked_frame_is_used_and_checked_again_on_the_next_sync(world):
    make_living(world.forge, frames=("blink", "happy"))
    world.checker.result["ok"] = None
    result = world.living.sync(NPC)
    assert result["unchecked"] == 2 and world.checker.calls == 1                   # once it has not answered, it is not asked again
    status = world.living.status(NPC)
    assert status["available"] and status["checked"] == 0
    world.checker.result["ok"] = True
    world.living.sync(NPC)
    assert world.living.status(NPC)["checked"] == 2


def test_a_frame_removed_in_npc_forge_is_removed_here_and_unknown_frames_are_ignored(world):
    make_living(world.forge, frames=("blink", "happy", "winking_hard"))
    world.living.sync(NPC)
    assert world.living.status(NPC)["frames"] == ["blink", "happy"]
    make_living(world.forge, frames=("blink",))
    assert world.living.sync(NPC)["removed"] == 1
    assert world.living.status(NPC)["frames"] == ["blink"]


def test_the_setting_is_on_by_default_and_saved_in_the_profile(world):
    assert profile.living_portrait_enabled() is True
    profile.set_living_portrait(False)
    assert profile.living_portrait_enabled() is False
    profile.save_user_profile({**profile.load_user_profile(), "name": "Sam"})       # another save keeps it
    assert profile.living_portrait_enabled() is False


def test_routes(world):
    make_living(world.forge)
    services = companion.Services({"imagegen": {}}, world.avatars,
                                  PhotoService(world.tmp / "photos", world.avatars, lambda level: FakeBackend(), world.checker, sync=True),
                                  world.checker, comfy=None, living_store=world.living)
    companion.set_services(services)
    try:
        client = create_app("companion").test_client()
        assert client.post(f"/api/avatars/{NPC}/living/sync").get_json()["started"] is True
        data = client.get(f"/api/avatars/{NPC}/living").get_json()
        assert data["enabled"] is True and data["frames"] == ["blink", "happy", "talking"]
        r = client.get(f"/api/avatars/{NPC}/living/blink")
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
        for bad in ("nosuch", "..%2Fportrait", "living.json"):
            assert client.get(f"/api/avatars/{NPC}/living/{bad}").status_code == 404
        assert client.get("/api/avatars/nobody-000000/living").status_code == 404
        assert client.post("/api/living", json={"enabled": "yes"}).status_code == 400
        assert client.post("/api/living", json={"enabled": False}).get_json()["enabled"] is False
        assert client.get("/api/living").get_json()["enabled"] is False
        assert client.post("/api/living", json={"enabled": True}, headers={"Origin": "http://evil.example"}).status_code == 403
        assert client.post(f"/api/avatars/{NPC}/living/sync", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    finally:
        companion.set_services(None)


def test_nothing_is_written_to_npc_forge(world):
    make_living(world.forge)
    before = {p: p.read_bytes() for p in world.forge.rglob("*") if p.is_file()}
    world.living.sync(NPC)
    assert {p: p.read_bytes() for p in world.forge.rglob("*") if p.is_file()} == before
