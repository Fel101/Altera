"""
Altera — ComfyUI client and image backends (stdlib only).

The client and the Mage-Flow-Edit graph are ported from NPC Forge (npcgen/comfy.py) so that Altera has no import dependency on
that project. Pictures are edits of an avatar's reference images (its NPC Forge portrait and, when it has one, its
full-body view), so a picture keeps the avatar's face.

A backend turns (reference images, prompt, negative prompt, seed, size) into PNG bytes:

  MageFlowBackend   the built-in Mage-Flow-Edit graph.
  WorkflowBackend   any ComfyUI workflow saved in API format, with {{prompt}} {{negative}} {{seed}} {{width}} {{height}}
                    {{steps}} {{reference}} {{reference_2}} placeholders. Point a level at one in config.yaml
                    (imagegen.workflows) to use a different checkpoint for that level. A workflow for a BIG model (Qwen-Image-Edit
                    is 20 GB plus a 9 GB text encoder) is listed with `min_free_ram_gb`, which makes it run the careful way:
                    see run_guarded.
"""
import copy
import http.client
import json
import logging
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

LOGGER = logging.getLogger(__name__)

DEFAULT_MODELS = {
    "unet_t2i": "mage_flow_int8_convrot.safetensors",
    "unet_edit": "mage_flow_edit_int8_convrot.safetensors",
    "clip": "qwen3vl_4b_bf16.safetensors",
    "vae": "mage_flow_vae_bf16.safetensors",
}

# quality -> (width, height, steps)
QUALITY = {"draft": (512, 704, 20), "standard": (768, 1024, 30), "high": (1024, 1344, 30)}
# a taller picture for a whole-body frame (see photos.frame_of)
FULL_LENGTH = {"draft": (512, 768, 20), "standard": (832, 1216, 30), "high": (960, 1408, 30)}
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024


class ImageGenError(RuntimeError):
    pass


class ComfyTransportError(ImageGenError):
    """A failed or untrusted transport must not start a fallback generation."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _valid_base_url(url):
    if (not isinstance(url, str) or not url
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or any(char in url for char in ('\\', '?', '#'))):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme in {'http', 'https'} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None and parsed.port != 0)
    except ValueError:
        return False


def _json_response(raw):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise ComfyTransportError('ComfyUI returned invalid JSON.') from None


def _prompt_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ComfyTransportError('ComfyUI returned an invalid job identifier.')
    return value


class Comfy:
    def __init__(self, base_url="http://127.0.0.1:8188", models=None):
        self.base = base_url.rstrip("/") if isinstance(base_url, str) else base_url
        self.client_id = "altera-" + uuid.uuid4().hex[:8]
        self.models = {**DEFAULT_MODELS, **(models or {})}

    # ---- low level -------------------------------------------------------
    def _request(self, method, path, data=None, headers=None, timeout=30):
        if not _valid_base_url(self.base):
            raise ComfyTransportError('Invalid configured ComfyUI URL.')
        if (not isinstance(path, str) or not path.startswith('/') or path.startswith('//')
                or any(ord(char) <= 32 or ord(char) == 127 for char in path)
                or '\\' in path or '#' in path):
            raise ComfyTransportError('Invalid ComfyUI request path.')
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        limit = MAX_IMAGE_BYTES if path.startswith('/view?') else MAX_JSON_BYTES
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(req, timeout=timeout) as resp:
                length = resp.headers.get('Content-Length')
                if length is not None:
                    size = int(length)
                    if size < 0 or size > limit:
                        raise ComfyTransportError('ComfyUI returned an invalid or oversized response.')
                raw = resp.read(limit + 1)
                if len(raw) > limit or (length is not None and len(raw) != size):
                    raise ComfyTransportError('ComfyUI returned an invalid or oversized response.')
                return raw
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            if code == 400 and method == 'POST' and path == '/prompt':
                raise ImageGenError('ComfyUI rejected the workflow (HTTP 400).') from None
            raise ComfyTransportError(f'ComfyUI returned HTTP {code}.') from None
        except (urllib.error.URLError, OSError):
            raise ComfyTransportError('Cannot reach ComfyUI. Is it running?') from None
        except (http.client.HTTPException, ValueError, UnicodeError):
            raise ComfyTransportError('ComfyUI returned an invalid or oversized response.') from None

    def get_json(self, path, timeout=30):
        result = _json_response(self._request("GET", path, timeout=timeout))
        expected = list if path.startswith('/models/') else dict
        if not isinstance(result, expected):
            raise ComfyTransportError('ComfyUI returned invalid response metadata.')
        return result

    def health(self):
        """{online, missing:[...]} without raising."""
        try:
            self.get_json("/system_stats", timeout=5)
        except ImageGenError as e:
            return {"online": False, "error": str(e), "missing": []}
        missing = []
        for folder, key in (("diffusion_models", "unet_edit"), ("text_encoders", "clip"), ("vae", "vae")):
            try:
                have = self.get_json(f"/models/{folder}", timeout=10)
            except ComfyTransportError as e:
                return {"online": False, "error": str(e), "missing": []}
            except ImageGenError:
                have = []
            if self.models[key] not in have:
                missing.append(f"{folder}/{self.models[key]}")
        return {"online": True, "missing": missing}

    # ---- images ----------------------------------------------------------
    def upload_image(self, png_bytes, filename, image_type="temp", subfolder="altera"):
        """Upload into ComfyUI's temp folder. Returns the value a LoadImage node takes."""
        boundary = "----altera" + uuid.uuid4().hex
        parts = []

        def field(name, value):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())

        field("type", image_type)
        field("subfolder", subfolder)
        field("overwrite", "false")
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
                      f'Content-Type: image/png\r\n\r\n').encode())
        parts.append(png_bytes)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        raw = self._request("POST", "/upload/image", data=b"".join(parts),
                            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=60)
        info = _json_response(raw)
        if (not isinstance(info, dict) or info.get('name') != filename
                or info.get('subfolder') != subfolder or info.get('type') != image_type):
            raise ComfyTransportError('ComfyUI returned invalid image metadata.')
        name = f'{subfolder}/{filename}' if subfolder else filename
        return f"{name} [{image_type}]"

    def fetch_image(self, filename, subfolder="", image_type="temp"):
        q = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": image_type})
        return self._request("GET", "/view?" + q, timeout=60)

    # ---- running graphs --------------------------------------------------
    def submit(self, graph):
        body = json.dumps({"prompt": graph, "client_id": self.client_id}).encode()
        raw = self._request("POST", "/prompt", data=body, headers={"Content-Type": "application/json"})
        info = _json_response(raw)
        if not isinstance(info, dict):
            raise ComfyTransportError('ComfyUI returned invalid job metadata.')
        return _prompt_id(info.get('prompt_id'))

    def wait(self, prompt_id, timeout=900, poll=1.0):
        _prompt_id(prompt_id)
        deadline = time.time() + timeout
        while time.time() < deadline:
            entry = self.get_json(f"/history/{prompt_id}", timeout=15).get(prompt_id)
            if entry and entry.get("status", {}).get("completed") is not None:
                status = entry["status"]
                if status.get("status_str") != "success":
                    raise ImageGenError(_format_exec_error(status))
                return entry
            time.sleep(poll)
        raise ImageGenError(f"ComfyUI job {prompt_id} did not finish within {timeout}s")

    def run(self, graph, timeout=900):
        """Submit a graph, wait, and return the first output image as PNG bytes."""
        entry = self.wait(self.submit(graph), timeout=timeout)
        for node_out in entry.get("outputs", {}).values():
            for img in node_out.get("images", []):
                return self.fetch_image(img["filename"], img.get("subfolder", ""), img.get("type", "temp"))
        raise ImageGenError("ComfyUI finished but produced no image")


def _format_exec_error(status):
    for kind, payload in status.get("messages", []):
        if kind == "execution_error":
            return (f'ComfyUI failed in node {payload.get("node_id")} ({payload.get("node_type")}): '
                    f'{payload.get("exception_message", "").strip()}')
    return "ComfyUI reported a failed run"


# ---- big models: memory safety -----------------------------------------------------------------------------------------
# Measured on a 32 GB GPU / 62 GB RAM PC: Qwen-Image-Edit (20 GB) plus its 9 GB text encoder, left loaded between pictures, made
# the RAM creep up until Windows paged and the whole PC froze (twice). A workflow that declares `min_free_ram_gb` therefore runs
# the careful way: Altera asks ComfyUI to free models before and after each picture, requires sufficient free RAM, and watches
# time, VRAM and RAM. On trouble it attempts interruption and freeing, but a disconnected server may not receive those requests.
# Other workflows are only watched (never flushed on successful completion: small models reload slowly).
ABORT_FREE_RAM_MB = 8000       # cancel if the free RAM falls below this while a picture runs
MAX_SECONDS = 420              # the stall limit (a Qwen Lightning picture takes about 35 s)
VRAM_FULL = 0.985              # VRAM full for VRAM_FULL_SECONDS in a row = spilling into system memory = a crawl
VRAM_FULL_SECONDS = 30
_LOADERS = {"UNETLoader": ("diffusion_models", "unet_name"), "CLIPLoader": ("text_encoders", "clip_name"),
            "VAELoader": ("vae", "vae_name"), "LoraLoaderModelOnly": ("loras", "lora_name"), "LoraLoader": ("loras", "lora_name"),
            "CheckpointLoaderSimple": ("checkpoints", "ckpt_name")}


def required_models(template) -> list:
    """[(model folder, file name)] the loader nodes of a workflow ask for."""
    out = []
    for node in template.values():
        spec = _LOADERS.get(node.get("class_type")) if isinstance(node, dict) else None
        name = (node.get("inputs") or {}).get(spec[1]) if spec else None
        if spec and isinstance(name, str) and (spec[0], name) not in out:
            out.append((spec[0], name))
    return out


def missing_models(comfy, template) -> list:
    """The model files a workflow needs that ComfyUI does not have, as 'folder/name'."""
    return [f"{folder}/{name}" for folder, name in required_models(template) if name not in comfy.get_json(f"/models/{folder}", timeout=10)]


def _post(comfy, path, body=None):
    try:
        comfy._request("POST", path, data=json.dumps(body).encode() if body is not None else None,
                       headers={"Content-Type": "application/json"} if body is not None else None, timeout=10)
    except Exception:                                   # a flush that cannot be sent must never hide the real problem
        pass


def free_memory(comfy):
    """Ask ComfyUI to unload every model and free memory (it does so as soon as it is idle)."""
    _post(comfy, "/free", {"unload_models": True, "free_memory": True})


def cancel_and_free(comfy):
    _post(comfy, "/interrupt")
    free_memory(comfy)


def _stats(comfy):
    try:
        d = comfy.get_json("/system_stats", timeout=8)
        dev = d["devices"][0]
        total, free, ram = dev["vram_total"], dev["vram_free"], d["system"]["ram_free"]
        if (any(type(value) not in (int, float) or not math.isfinite(value) for value in (total, free, ram))
                or total < 0 or free < 0 or free > total or ram < 0):
            raise ValueError()
        return (total - free) // 2 ** 20, total // 2 ** 20, ram // 2 ** 20
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        raise ComfyTransportError('ComfyUI returned invalid system statistics.') from None


def wait_idle(comfy, seconds=60, sleep=None, clock=None):
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    end = clock() + seconds
    while clock() < end:
        try:
            q = comfy.get_json("/queue", timeout=8)
            if not q["queue_running"] and not q["queue_pending"]:
                return True
        except ComfyTransportError:
            raise
        except Exception:
            pass
        sleep(2)
    return False


def run_guarded(comfy, graph, min_free_ram_gb=0, sleep=None, clock=None):
    """Run a graph and return the PNG bytes, watching it (see the note above). With min_free_ram_gb > 0 it is the careful way:
    request a flush first and last, and do not start without that much free RAM. On trouble, attempt to interrupt the job and
    free memory; these server-wide requests are best effort and do not confirm that a particular job stopped."""
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    careful = min_free_ram_gb > 0
    need = int(min_free_ram_gb * 1024)
    if careful:
        cancel_and_free(comfy)                          # nothing else may be running, and start from an empty GPU
        if not wait_idle(comfy, 60, sleep, clock):
            raise ImageGenError("ComfyUI is busy or not answering: it was asked to stop and free its memory, but did not go idle.")
        deadline, free = clock() + 60, 0
        while True:
            try:
                free = _stats(comfy)[2]
            except ComfyTransportError:
                raise
            except Exception:
                free = 0
            if free >= need or clock() > deadline:
                break
            sleep(2)
        if free < need:
            raise ImageGenError(f"Not enough free memory for this picture ({free // 1024} GB of RAM free, about {min_free_ram_gb:g} GB "
                                "needed). Close programs or unload models in LM Studio, then try again.")
    try:
        prompt_id = comfy.submit(graph)
        start, full_since, misses = clock(), None, 0
        while True:
            sleep(2)
            elapsed = clock() - start
            try:
                vram, vram_total, free = _stats(comfy)
                misses = 0
            except ComfyTransportError:
                raise
            except Exception:
                misses += 1
                if misses >= 3:
                    raise ImageGenError("ComfyUI stopped answering while making the picture.") from None
                continue
            full_since = (full_since if full_since is not None else clock()) if vram > vram_total * VRAM_FULL else None
            if free < ABORT_FREE_RAM_MB:
                raise ImageGenError(f"The PC ran short of memory ({free // 1024} GB of RAM free) after {elapsed:.0f} s. Altera stopped waiting "
                                    "and attempted to ask ComfyUI to stop; check ComfyUI before retrying.")
            if full_since is not None and clock() - full_since > VRAM_FULL_SECONDS:
                raise ImageGenError("The GPU memory stayed full. Altera stopped waiting and attempted to ask ComfyUI to stop; check ComfyUI before retrying.")
            if elapsed > MAX_SECONDS:
                raise ImageGenError(f"The picture took more than {MAX_SECONDS} s. Altera attempted to ask ComfyUI to stop; check ComfyUI before retrying.")
            entry = comfy.get_json(f"/history/{prompt_id}", timeout=15).get(prompt_id)
            if entry and entry.get("status", {}).get("completed") is not None:
                if entry["status"].get("status_str") != "success":
                    raise ImageGenError(_format_exec_error(entry["status"]))
                for node_out in entry.get("outputs", {}).values():
                    for img in node_out.get("images", []):
                        return comfy.fetch_image(img["filename"], img.get("subfolder", ""), img.get("type", "temp"))
                raise ImageGenError("ComfyUI finished but produced no image")
    except BaseException:
        cancel_and_free(comfy)
        try:
            wait_idle(comfy, 60, sleep, clock)
        except ComfyTransportError:
            pass  # Keep the original failure after best-effort cleanup.
        raise
    finally:
        if careful:
            free_memory(comfy)                          # models out of memory after every picture


# ---- graphs -----------------------------------------------------------------

def edit_graph(models, images, prompt, seed, steps=30, cfg=4.0, size=(768, 1024), negative="", megapixels=1.0):
    """Mage-Flow-Edit: an instruction edit of one or more reference images, at an explicit output size.

    Mage-Flow-Edit takes up to 16 references; 1-3 work best. Every reference is resized to the output size by the encoder.
    """
    refs = [images] if isinstance(images, str) else list(images)
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": models["unet_edit"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": models["clip"], "type": "mage", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": models["vae"]}},
    }
    encoder_inputs = {}
    for j, name in enumerate(refs):
        load_id, scale_id = str(20 + 2 * j), str(21 + 2 * j)
        g[load_id] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g[scale_id] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": [load_id, 0], "upscale_method": "lanczos", "megapixels": float(megapixels), "resolution_steps": 16}}
        encoder_inputs[f"images.image_{j + 1}"] = [scale_id, 0]
    g["4"] = {"class_type": "TextEncodeMageFlowEdit", "inputs": {
        "clip": ["2", 0], "vae": ["3", 0], **encoder_inputs, "prompt": prompt, "negative_prompt": negative,
        "width": int(size[0]), "height": int(size[1]), "batch_size": 1}}
    g["5"] = {"class_type": "KSampler", "inputs": {
        "model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1], "latent_image": ["4", 2],
        "seed": int(seed), "steps": int(steps), "cfg": float(cfg),
        "sampler_name": "euler", "scheduler": "simple", "denoise": 1}}
    g["6"] = {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["3", 0]}}
    g["7"] = {"class_type": "PreviewImage", "inputs": {"images": ["6", 0]}}
    return g


_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def fill_workflow(template, values):
    """Copy of a workflow with {{name}} placeholders replaced. A string that is only a placeholder keeps the value's type
    (so {{seed}} becomes a number); a placeholder inside longer text is substituted as text."""
    unknown = sorted({m for m in _placeholders(template) if m not in values})
    if unknown:
        raise ImageGenError("The workflow uses unknown placeholders: " + ", ".join("{{" + u + "}}" for u in unknown))

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            whole = _PLACEHOLDER.fullmatch(node.strip())
            if whole:
                return values[whole.group(1)]
            return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), node)
        return node

    return walk(copy.deepcopy(template))


def _placeholders(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _placeholders(v)
    elif isinstance(node, list):
        for v in node:
            yield from _placeholders(v)
    elif isinstance(node, str):
        yield from _PLACEHOLDER.findall(node)


# ---- backends ---------------------------------------------------------------

def _upload_references(comfy, refs):
    # A single render owns its own names, including both sides of a shared
    # scene. Another instance or overlapping job must not replace them.
    token = uuid.uuid4().hex
    return [comfy.upload_image(png, f'altera_{token}_{index}.png') for index, png in enumerate(refs)]


class MageFlowBackend:
    name = "mage-flow-edit"

    def __init__(self, comfy, cfg=4.0):
        self.comfy = comfy
        self.cfg = cfg

    def render(self, refs, prompt, negative, seed, size, steps):
        names = _upload_references(self.comfy, refs)
        graph = edit_graph(self.comfy.models, names, prompt, seed, steps=steps, cfg=self.cfg, size=size, negative=negative)
        return self.comfy.run(graph)


class WorkflowBackend:
    def __init__(self, comfy, path, min_free_ram_gb=0):
        """min_free_ram_gb > 0 marks a big model: it runs the careful way (see run_guarded)."""
        self.comfy = comfy
        self.path = Path(path)
        self.min_free_ram_gb = float(min_free_ram_gb or 0)
        self.name = f"workflow:{self.path.name}"
        try:
            self.template = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError) as e:
            raise ImageGenError(f"Cannot read the workflow {self.path}: {e}") from None

    def accepts_shared_references(self):
        """Require both image placeholders in node inputs for a shared scene."""
        if not isinstance(self.template, dict):
            return False
        inputs = (node.get('inputs') for node in self.template.values() if isinstance(node, dict))
        references = {name for value in inputs for name in _placeholders(value)}
        return {'reference', 'reference_2'} <= references

    def render(self, refs, prompt, negative, seed, size, steps):
        missing = missing_models(self.comfy, self.template)
        if missing:
            raise ImageGenError(f"The workflow {self.path.name} needs files ComfyUI does not have: {', '.join(missing)}.")
        names = _upload_references(self.comfy, refs)
        values = {"prompt": prompt, "negative": negative, "seed": int(seed), "width": int(size[0]), "height": int(size[1]),
                  "steps": int(steps), "reference": names[0], "reference_2": names[1] if len(names) > 1 else names[0]}
        return run_guarded(self.comfy, fill_workflow(self.template, values), self.min_free_ram_gb)


class FallbackBackend:
    """Tries a big-model workflow first. When it cannot run (a file is missing, the PC is short of memory, the picture stalled)
    the built-in Mage-Flow-Edit makes the picture instead and the reason is logged and kept in `last_note`, so a chat is never
    left without a picture. It does not help when ComfyUI cannot be reached at all (Mage-Flow would fail too).
    imagegen.workflow_fallback: false makes a failing workflow fail instead."""

    def __init__(self, primary, fallback):
        self.primary, self.fallback, self.last_note = primary, fallback, None
        self.name = primary.name

    def render(self, refs, prompt, negative, seed, size, steps):
        self.last_note = None
        try:
            return self.primary.render(refs, prompt, negative, seed, size, steps)
        except ImageGenError as e:
            if isinstance(e, ComfyTransportError) or str(e).startswith("Cannot reach ComfyUI"):
                raise
            self.last_note = f"Made with {self.fallback.name} instead of {self.primary.name}: {e}"
            LOGGER.warning(self.last_note)
            return self.fallback.render(refs, prompt, negative, seed, size, steps)


def backend_factory(config, project_root, comfy=None):
    """level -> backend, from config.yaml's imagegen section. Blank workflow = the built-in Mage-Flow-Edit graph."""
    cfg = (config or {}).get("imagegen") or {}
    comfy = comfy or Comfy(cfg.get("comfy_url", "http://127.0.0.1:8188"))
    workflows = cfg.get("workflows") or {}
    fallback = cfg.get("workflow_fallback", True) is not False
    built_in = MageFlowBackend(comfy, cfg=float(cfg.get("cfg", 4.0)))
    cache = {}

    def for_level(level):
        entry = workflows.get(level) or ""
        # a plain path, or {path: ..., min_free_ram_gb: N} for a big model
        path = (entry.get("path") if isinstance(entry, dict) else str(entry)) or ""
        path = path.strip()
        if not path:
            return built_in
        min_ram = float(entry.get("min_free_ram_gb") or 0) if isinstance(entry, dict) else 0
        if (path, min_ram) not in cache:
            p = Path(path)
            backend = WorkflowBackend(comfy, p if p.is_absolute() else Path(project_root) / p, min_ram)
            cache[(path, min_ram)] = FallbackBackend(backend, built_in) if fallback else backend
        return cache[(path, min_ram)]

    for_level.comfy = comfy
    return for_level
