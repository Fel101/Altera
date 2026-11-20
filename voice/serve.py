"""Altera's local voice: a small Kokoro speech service.

Kokoro needs PyTorch, which Altera's own environment does not have, so this runs in its own environment (voice/.venv,
made by voice/setup.bat or voice/setup.sh). Altera starts it when voice.kokoro.autostart is on, and stops it when it exits.

It listens on 127.0.0.1 only and answers the two requests Altera makes:

    GET  /api/info    {"device": "cpu" or "cuda", "loaded": ["kokoro"] once the model is in memory, "voices": [...]}
    POST /api/speak   {"text": ..., "voice": "af_heart", "speed": 1.0}  ->  a 24 kHz mono 16-bit WAV

Kokoro and its voices: https://huggingface.co/hexgrad/Kokoro-82M (Apache-2.0). Run `serve.py --download` once to fetch
them; after that the service works offline.

It runs on the CPU, which is quick enough for an 82M model. A GPU is opt-in (--device auto or cuda, or the
ALTERA_VOICE_DEVICE environment variable), because some GPU builds of PyTorch crash on it: ROCm on Windows did.
"""
import argparse
import gc
import io
import json
import os
import re
import sys
import threading
import time
import warnings
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = "hexgrad/Kokoro-82M"
HOST = "127.0.0.1"
DEFAULT_PORT = 8770
SAMPLE_RATE = 24000
DEFAULT_VOICES = ("af_heart", "af_bella", "af_nicole", "af_sky", "am_adam", "am_michael", "bf_emma", "bm_george")
MAX_BODY = 16 * 1024            # a request is a short JSON object
MAX_TEXT = 1000                 # Altera sends at most 400 characters at a time
DRAIN_LIMIT = 1024 * 1024       # the most of a refused request's body that is read before answering
VOICE = re.compile(r"^[abefhijpz][fm]_[a-z]{2,20}$")     # the first letter is Kokoro's language code


def log(message):
    print(message, flush=True)


class Kokoro:
    """The Kokoro model, loaded on first use (or at start with --preload) and dropped again after --idle-minutes."""

    def __init__(self, device="cpu"):
        self.requested = device
        self.device = "cpu" if device == "cpu" else None
        self.model = None
        self.pipelines = {}
        self.lock = threading.Lock()
        self.last_used = time.monotonic()

    @property
    def loaded(self):
        return self.model is not None

    def _pick_device(self):
        import torch
        if self.requested == "cuda" or (self.requested == "auto" and torch.cuda.is_available()):
            return "cuda"
        return "cpu"

    def load(self):
        with self.lock:
            self._load()

    def _load(self):
        if self.model is None:
            from kokoro import KModel
            self.device = self._pick_device()
            log(f"Loading Kokoro on {self.device}...")
            self.model = KModel(repo_id=REPO).to(self.device).eval()
            log("Kokoro is ready.")
        self.last_used = time.monotonic()

    def unload(self):
        with self.lock:
            if self.model is None:
                return
            self.model, self.pipelines = None, {}
        gc.collect()
        log("Kokoro unloaded after being idle.")

    def voices(self):
        """The voice files in the local copy of Kokoro, without going online."""
        try:
            from huggingface_hub import snapshot_download
            folder = snapshot_download(REPO, local_files_only=True)
        except Exception:
            return []
        names = (name[:-3] for name in os.listdir(os.path.join(folder, "voices")) if name.endswith(".pt"))
        return sorted(name for name in names if VOICE.match(name))

    def speak(self, text, voice, speed):
        """The speech for text as 16-bit PCM bytes."""
        import numpy as np
        from kokoro import KPipeline
        with self.lock:
            self._load()
            code = voice[0]
            if code not in self.pipelines:
                self.pipelines[code] = KPipeline(lang_code=code, repo_id=REPO, model=self.model)
            pieces = [result.audio for result in self.pipelines[code](text, voice=voice, speed=speed)
                      if result.audio is not None]
            self.last_used = time.monotonic()
        if not pieces:
            return b""
        audio = np.concatenate([piece.detach().cpu().numpy() if hasattr(piece, "detach") else np.asarray(piece)
                                for piece in pieces])
        return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def wav(pcm):
    """16-bit mono PCM as a WAV file."""
    out = io.BytesIO()
    with wave.open(out, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(SAMPLE_RATE)
        file.writeframes(pcm)
    return out.getvalue()


def make_server(port, engine):
    """The HTTP server for engine (anything with loaded, device, voices() and speak(text, voice, speed)). Port 0: any."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "AlteraVoice/1"
        sys_version = ""
        timeout = 30        # a client that never sends the body it announced cannot hold a thread for ever
        _unread = 0         # request body bytes not read yet

        def log_message(self, *args):   # nothing about the request is written down
            pass

        def _drain(self):
            # Closing a socket with unread request bytes makes Windows send a reset, which can reach the client before
            # this answer does: a refused request would then look like a dropped connection. Read what was sent first.
            left, self._unread = min(self._unread, DRAIN_LIMIT), 0
            try:
                while left > 0:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
            except OSError:
                pass

        def _send(self, status, body, kind):
            self._drain()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, data):
            self._send(status, json.dumps(data).encode("utf-8"), "application/json")

        def _local(self):
            # A web page can make the browser send requests to 127.0.0.1. Asking for the loopback Host refuses a page
            # that reaches this service through its own domain name.
            port = self.server.server_address[1]
            if self.headers.get("Host") not in {f"{HOST}:{port}", f"localhost:{port}"}:
                self._json(403, {"error": "This service answers only on 127.0.0.1."})
                return False
            return True

        def do_GET(self):
            if not self._local():
                return
            if self.path != "/api/info":
                return self._json(404, {"error": "Not found."})
            self._json(200, {"device": engine.device or "cpu", "loaded": ["kokoro"] if engine.loaded else [],
                             "voices": engine.voices()})

        def do_POST(self):
            try:
                self._unread = max(0, int(self.headers.get("Content-Length") or 0))
            except ValueError:
                self._unread = 0
            if not self._local():
                return
            if self.path != "/api/speak":
                return self._json(404, {"error": "Not found."})
            # A JSON content type makes a browser ask first (CORS preflight), which this service never allows.
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                return self._json(415, {"error": "Send JSON."})
            try:
                size = int(self.headers.get("Content-Length") or "")
            except ValueError:
                return self._json(411, {"error": "Content-Length is required."})
            if size < 0 or size > MAX_BODY:
                return self._json(413, {"error": "The request is too large."})
            raw, self._unread = self.rfile.read(size), 0
            try:
                request = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return self._json(400, {"error": "The request is not JSON."})
            if not isinstance(request, dict):
                return self._json(400, {"error": "The request is not a JSON object."})
            text, voice, speed = request.get("text"), request.get("voice") or "af_heart", request.get("speed", 1.0)
            if request.get("engine", "kokoro") != "kokoro":
                return self._json(400, {"error": "Only the kokoro engine is here."})
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                return self._json(400, {"error": f"text must be 1 to {MAX_TEXT} characters."})
            if not isinstance(voice, str) or not VOICE.match(voice):
                return self._json(400, {"error": "Unknown voice."})
            if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0.5 <= speed <= 2.0:
                return self._json(400, {"error": "speed must be between 0.5 and 2.0."})
            try:
                pcm = engine.speak(text, voice, float(speed))
            except Exception as error:
                log(f"Speech failed: {type(error).__name__}")
                return self._json(500, {"error": "The speech could not be made. See the voice service's log."})
            if not pcm:
                return self._json(500, {"error": "Kokoro made no sound for this text."})
            self._send(200, wav(pcm), "audio/wav")

    server = Server((HOST, port), Handler)
    server.daemon_threads = True
    return server


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Altera's status check gives up after a moment; a client that hung up is not worth a traceback.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        log(f"A request failed: {sys.exc_info()[0].__name__}")


def parent_alive(pid):
    """Whether process pid is still running. Never signals it (os.kill would end it on Windows)."""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x00100000, False, pid)      # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102     # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def watch_parent(pid, server, interval=1.0):
    """Stop the server once the process that started it has gone."""
    while parent_alive(pid):
        time.sleep(interval)
    log("The process that started the voice service has ended; stopping.")
    server.shutdown()


def watch_idle(engine, minutes, interval=30.0):
    while True:
        time.sleep(interval)
        if engine.loaded and time.monotonic() - engine.last_used > minutes * 60:
            engine.unload()


def download(voices):
    """Fetch Kokoro and the given voices into the Hugging Face cache, so the service can run offline."""
    # Two notices that ask people for things they do not need: a Hugging Face token (these files are public) and
    # Windows Developer Mode for symlinks (without them the cache keeps plain copies). Errors are still shown.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_VERBOSITY", "error")
    from huggingface_hub import hf_hub_download
    for name in ("config.json", "kokoro-v1_0.pth", *(f"voices/{voice}.pt" for voice in voices)):
        log(f"Fetching {name}...")
        hf_hub_download(REPO, name)
    # Kokoro's English pronunciation (misaki) needs spaCy's small English model, and installs it the first time a
    # pipeline is made. Make each language's pipeline now, without the model weights, so that happens here and the
    # first spoken reply never goes online.
    from kokoro import KPipeline
    for code in sorted({voice[0] for voice in voices}):
        log(f"Preparing pronunciation for '{code}' voices...")
        KPipeline(lang_code=code, repo_id=REPO, model=False)
    log("Done. The voice service can now run offline.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Altera's local Kokoro voice service (127.0.0.1 only).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"),
                        default=os.environ.get("ALTERA_VOICE_DEVICE", "cpu").strip().lower() or "cpu",
                        help="cpu (the default), cuda, or auto: cuda when PyTorch has a GPU")
    parser.add_argument("--preload", default="", help="kokoro: load the model at start instead of on the first request")
    parser.add_argument("--idle-minutes", type=float, default=0, help="unload the model after this long unused (0: never)")
    parser.add_argument("--parent-pid", type=int, default=0, help="stop when this process ends (Altera passes it)")
    parser.add_argument("--download", action="store_true", help="fetch Kokoro and the default voices, then exit")
    parser.add_argument("--voices", default=",".join(DEFAULT_VOICES), help="the voices --download fetches")
    args = parser.parse_args(argv)
    # PyTorch warns that Kokoro uses torch.jit.script; that is for Kokoro's authors, and it lands in Altera's log.
    warnings.filterwarnings("ignore", message=r"`torch\.jit\.script` is deprecated", category=FutureWarning)
    if args.download:
        download([voice for voice in args.voices.split(",") if VOICE.match(voice.strip())])
        return 0
    if not 0 < args.port < 65536:
        parser.error("--port must be between 1 and 65535")

    engine = Kokoro(args.device)
    server = make_server(args.port, engine)
    log(f"Altera voice service on http://{HOST}:{args.port}")
    if args.parent_pid:
        threading.Thread(target=watch_parent, args=(args.parent_pid, server), daemon=True).start()
    if args.idle_minutes > 0:
        threading.Thread(target=watch_idle, args=(engine, args.idle_minutes), daemon=True).start()
    if "kokoro" in args.preload.split(","):
        def preload():
            try:
                engine.load()
            except Exception as error:
                log(f"Kokoro could not be loaded: {type(error).__name__}: {error}")
        threading.Thread(target=preload, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
