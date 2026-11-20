"""Tests for running a big-model workflow the careful way (Qwen-Image-Edit for the Suggestive and Explicit levels).

No GPU, ComfyUI or model is needed: a fake ComfyUI answers like the real one, and time only moves when the code sleeps.
Background: on a 32 GB GPU / 62 GB RAM PC, Qwen-Image-Edit left loaded between pictures made the RAM creep up until Windows paged
and the whole PC froze (twice). See core/imagegen.py, "big models: memory safety".
"""
import json
import os
import types
from pathlib import Path

import pytest

from core import imagegen
from core.imagegen import ImageGenError, WorkflowBackend

HERE = Path(__file__).resolve().parent.parent
WORKFLOW = HERE / "content" / "workflows" / "qwen_edit_2511_lightning.json"
PNG = b"\x89PNG\r\n\x1a\nfake-image"


class GuardComfy:
    """Answers like ComfyUI does for what the guard asks."""

    def __init__(self):
        self.files, self.requests, self.graphs, self.uploads = {}, [], [], []
        self.mb = {"vram_total": 32600, "vram_free": 20000, "ram_free": 45000}
        self.running_mb = {}                     # overrides of mb once a picture is running
        self.busy, self.finishes, self.outcome = False, True, "success"
        self.submitted, self.dead_when_running = False, False

    def get_json(self, path, timeout=30):
        if path.startswith("/models/"):
            return list(self.files.get(path.split("/")[2], []))
        if path == "/system_stats":
            if self.submitted and self.dead_when_running:
                raise ImageGenError("Cannot reach ComfyUI")
            m = {**self.mb, **(self.running_mb if self.submitted else {})}
            return {"devices": [{"vram_total": m["vram_total"] * 2 ** 20, "vram_free": m["vram_free"] * 2 ** 20}],
                    "system": {"ram_free": m["ram_free"] * 2 ** 20}}
        if path == "/queue":
            return {"queue_running": [1] if self.busy else [], "queue_pending": []}
        if path.startswith("/history/"):
            pid = path.split("/")[2]
            if not self.finishes:
                return {}
            return {pid: {"status": {"completed": True, "status_str": self.outcome, "messages": []},
                          "outputs": {"22": {"images": [{"filename": "q.png", "subfolder": "", "type": "temp"}]}}}}
        raise AssertionError(path)

    def _request(self, method, path, data=None, headers=None, timeout=30):
        self.requests.append((method, path))
        return b"{}"

    def submit(self, graph):
        self.graphs.append(graph)
        self.submitted = True
        return "job-1"

    def fetch_image(self, filename, subfolder="", image_type="temp"):
        return PNG + b"qwen"

    def upload_image(self, png, name, **kw):
        self.uploads.append((name, png))
        return f"altera/{name} [temp]"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(imagegen, "time", types.SimpleNamespace(monotonic=c, sleep=c.sleep))
    return c


@pytest.fixture
def comfy():
    return GuardComfy()


TEMPLATE = json.loads(WORKFLOW.read_text("utf-8"))


def give_the_qwen_files(comfy):
    for folder, name in imagegen.required_models(TEMPLATE):
        comfy.files.setdefault(folder, []).append(name)


# ---------------------------------------------------------------- the workflow file

def test_the_qwen_workflow_is_complete_and_says_which_files_it_needs():
    needs = {name: folder for folder, name in imagegen.required_models(TEMPLATE)}
    assert needs == {"Qwen-Image-Edit-2511-FP8_e4m3fn.safetensors": "diffusion_models", "qwen_2.5_vl_7b_fp8_scaled.safetensors": "text_encoders",
                     "qwen_image_vae.safetensors": "vae", "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors": "loras"}
    values = {"prompt": "a scene", "negative": "n", "seed": 42, "width": 832, "height": 1216, "steps": 30, "reference": "r.png", "reference_2": "r.png"}
    graph = imagegen.fill_workflow(TEMPLATE, values)
    assert "{{" not in json.dumps(graph) and "{{" in json.dumps(TEMPLATE)
    sampler = next(n for n in graph.values() if n["class_type"] == "KSampler")["inputs"]
    assert (sampler["steps"], sampler["cfg"], sampler["seed"]) == (4, 1.0, 42)                # the Lightning setting, whatever `steps` says
    latent = next(n for n in graph.values() if n["class_type"] == "EmptySD3LatentImage")["inputs"]
    assert (latent["width"], latent["height"]) == (832, 1216)


# Point ALTERA_TEST_NPC_FORGE_WORKFLOW at NPC Forge's copy of this workflow to check the two are the same file.
NPC_FORGE_COPY = Path(os.environ.get("ALTERA_TEST_NPC_FORGE_WORKFLOW") or "missing-npc-forge-workflow.json")


@pytest.mark.skipif(not NPC_FORGE_COPY.exists(), reason="ALTERA_TEST_NPC_FORGE_WORKFLOW is not set to NPC Forge's workflow")
def test_the_workflow_is_the_same_file_the_npc_forge_studio_uses():
    assert json.loads(NPC_FORGE_COPY.read_text("utf-8")) == TEMPLATE


def test_which_model_files_are_missing(comfy):
    assert len(imagegen.missing_models(comfy, TEMPLATE)) == 4
    give_the_qwen_files(comfy)
    assert imagegen.missing_models(comfy, TEMPLATE) == []
    comfy.files["loras"] = []
    assert imagegen.missing_models(comfy, TEMPLATE) == ["loras/Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"]


# ---------------------------------------------------------------- the careful run

def test_a_careful_picture_is_flushed_before_and_after(comfy, clock):
    assert imagegen.run_guarded(comfy, {"1": {}}, 36) == PNG + b"qwen"
    assert [p for _, p in comfy.requests] == ["/interrupt", "/free", "/free"]


def test_a_workflow_that_is_not_a_big_model_is_only_watched(comfy, clock):
    assert imagegen.run_guarded(comfy, {"1": {}}, 0) == PNG + b"qwen"
    assert comfy.requests == []                                       # small models are not flushed: they would reload every time


@pytest.mark.parametrize("gb, setup, message", [
    (36, lambda c: c.mb.update(ram_free=20000), "Not enough free memory"),                        # would not fit: it never starts
    (36, lambda c: setattr(c, "busy", True), "did not go idle"),
    (36, lambda c: (c.running_mb.update(ram_free=3000), setattr(c, "finishes", False)), "ran short of memory"),
    (0, lambda c: (c.running_mb.update(ram_free=3000), setattr(c, "finishes", False)), "ran short of memory"),      # also when not careful
    (36, lambda c: (c.running_mb.update(vram_free=100), setattr(c, "finishes", False)), "GPU memory stayed full"),
    (36, lambda c: setattr(c, "finishes", False), "took more than"),
    (36, lambda c: setattr(c, "dead_when_running", True), "stopped answering"),
    (36, lambda c: setattr(c, "outcome", "error"), "failed run"),
])
def test_the_guard_cancels_and_frees_at_the_first_sign_of_trouble(comfy, clock, gb, setup, message):
    setup(comfy)
    with pytest.raises(ImageGenError, match=message):
        imagegen.run_guarded(comfy, {"1": {}}, gb)
    sent = [p for _, p in comfy.requests]
    if comfy.submitted:
        assert sent[-3:] == ["/interrupt", "/free", "/free"] or sent[-2:] == ["/interrupt", "/free"]      # told to stop and to free
    else:
        assert sent[:2] == ["/interrupt", "/free"]


@pytest.mark.parametrize('failure', ['ram', 'vram', 'timeout'])
def test_failed_interrupt_does_not_claim_the_picture_was_cancelled(clock, failure):
    class FailedInterrupt(GuardComfy):
        def _request(self, method, path, data=None, headers=None, timeout=30):
            self.requests.append((method, path))
            raise OSError('Synthetic ComfyUI control failure')

    comfy = FailedInterrupt()
    comfy.finishes = False
    if failure == 'ram':
        comfy.running_mb.update(ram_free=3000)
    elif failure == 'vram':
        comfy.running_mb.update(vram_free=100)
    with pytest.raises(ImageGenError) as error:
        imagegen.run_guarded(comfy, {'1': {}}, 0)
    assert '/interrupt' in [path for _, path in comfy.requests]
    assert 'attempted to ask ComfyUI to stop' in str(error.value)
    assert 'was cancelled' not in str(error.value)


# ---------------------------------------------------------------- the backend and the config

def test_a_big_model_workflow_backend_checks_the_files_then_runs_carefully(comfy, clock):
    backend = WorkflowBackend(comfy, WORKFLOW, min_free_ram_gb=36)
    with pytest.raises(ImageGenError, match="needs files ComfyUI does not have: diffusion_models/Qwen-Image-Edit-2511"):
        backend.render([PNG], "p", "n", 5, (832, 1216), 30)
    assert comfy.graphs == [] and comfy.uploads == []                     # nothing was sent before the check
    give_the_qwen_files(comfy)
    assert backend.render([PNG + b"portrait"], "a scene", "n", 5, (832, 1216), 30) == PNG + b"qwen"
    graph = comfy.graphs[0]
    assert next(n for n in graph.values() if n["class_type"] == "TextEncodeQwenImageEditPlus")["inputs"]["prompt"] == "a scene"
    assert comfy.uploads[0][1] == PNG + b"portrait" and [p for _, p in comfy.requests][-1] == "/free"


def test_the_config_takes_a_plain_path_or_a_big_model_entry(comfy, tmp_path):
    config = {"imagegen": {"workflow_fallback": False, "workflows": {"off": "", "romantic": "",
                                         "suggestive": {"path": "content/workflows/qwen_edit_2511_lightning.json", "min_free_ram_gb": 36},
                                         "explicit": {"path": "content/workflows/qwen_edit_2511_lightning.json", "min_free_ram_gb": 36}}}}
    for_level = imagegen.backend_factory(config, HERE, comfy=comfy)
    assert isinstance(for_level("off"), imagegen.MageFlowBackend) and for_level("romantic") is for_level("off")
    suggestive, explicit = for_level("suggestive"), for_level("explicit")
    assert isinstance(explicit, WorkflowBackend) and explicit.min_free_ram_gb == 36 and suggestive is explicit     # one instance per file
    plain = imagegen.backend_factory({"imagegen": {"workflow_fallback": False, "workflows": {"explicit": str(WORKFLOW)}}}, HERE, comfy=comfy)("explicit")
    assert isinstance(plain, WorkflowBackend) and plain.min_free_ram_gb == 0                                # the old plain-path form
    with pytest.raises(ImageGenError, match="Cannot read the workflow"):
        imagegen.backend_factory({"imagegen": {"workflows": {"explicit": str(tmp_path / "nope.json")}}}, HERE, comfy=comfy)("explicit")


class FakeMage:
    name = "mage-flow-edit"

    def __init__(self):
        self.calls = []

    def render(self, refs, prompt, negative, seed, size, steps):
        self.calls.append((prompt, size))
        return PNG + b"mage"


def test_a_workflow_that_cannot_run_falls_back_to_mage_flow_and_says_why(comfy, clock):
    config = {"imagegen": {"workflows": {"explicit": {"path": "content/workflows/qwen_edit_2511_lightning.json", "min_free_ram_gb": 36}}}}
    backend = imagegen.backend_factory(config, HERE, comfy=comfy)("explicit")
    assert isinstance(backend, imagegen.FallbackBackend) and isinstance(backend.primary, WorkflowBackend)     # the default is to fall back
    backend.fallback = FakeMage()
    assert backend.render([PNG], "a scene", "n", 5, (832, 1216), 30) == PNG + b"mage"                          # its files are missing
    assert "instead of workflow:qwen_edit_2511_lightning.json" in backend.last_note and "diffusion_models/Qwen" in backend.last_note
    give_the_qwen_files(comfy)
    comfy.mb["ram_free"] = 20000                                                                             # ... and now the PC is short
    assert backend.render([PNG], "a scene", "n", 5, (832, 1216), 30) == PNG + b"mage" and "Not enough free memory" in backend.last_note
    comfy.mb["ram_free"] = 45000
    assert backend.render([PNG], "a scene", "n", 5, (832, 1216), 30) == PNG + b"qwen" and backend.last_note is None   # and it works again
    assert len(backend.fallback.calls) == 2


def test_the_fallback_does_not_hide_an_unreachable_comfyui_and_can_be_switched_off(comfy, clock):
    class Down(WorkflowBackend):
        def render(self, *a):
            raise ImageGenError("Cannot reach ComfyUI at http://127.0.0.1:8188 (refused). Is it running?")
    mage = FakeMage()
    with pytest.raises(ImageGenError, match="Cannot reach ComfyUI"):
        imagegen.FallbackBackend(Down(comfy, WORKFLOW), mage).render([PNG], "p", "n", 1, (832, 1216), 30)
    assert mage.calls == []                                                                                   # Mage-Flow would fail too
    off = imagegen.backend_factory({"imagegen": {"workflow_fallback": False, "workflows": {"explicit": str(WORKFLOW)}}}, HERE, comfy=comfy)("explicit")
    with pytest.raises(ImageGenError, match="needs files ComfyUI does not have"):
        off.render([PNG], "p", "n", 1, (832, 1216), 30)
