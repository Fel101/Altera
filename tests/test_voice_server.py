"""Altera's own voice service (voice/serve.py), with a fake engine: no PyTorch, no Kokoro, no model."""
import http.client
import importlib.util
import json
import threading
import wave
import io
from pathlib import Path

import pytest

from core import tts, voice_http

SOURCE = Path(__file__).resolve().parent.parent / "voice" / "serve.py"
spec = importlib.util.spec_from_file_location("altera_voice_serve", SOURCE)
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)


class FakeEngine:
    device = "cpu"

    def __init__(self):
        self.loaded, self.spoken, self.fail = True, [], False

    def voices(self):
        return ["af_heart", "bf_emma"]

    def speak(self, text, voice, speed):
        if self.fail:
            raise RuntimeError("synthetic failure with a private detail")
        self.spoken.append((text, voice, speed))
        return b"\x01\x00" * 2400 * len(text)


@pytest.fixture
def service():
    engine = FakeEngine()
    server = serve.make_server(0, engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def call(method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        payload = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        connection.request(method, path, body=payload, headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, response.getheader("Content-Type"), data

    yield engine, port, call
    server.shutdown()
    server.server_close()


def test_it_listens_on_loopback_only(service):
    assert serve.HOST == "127.0.0.1"


def test_info_says_whether_the_model_is_loaded(service):
    engine, _, call = service
    status, kind, data = call("GET", "/api/info")
    assert status == 200 and kind == "application/json"
    assert json.loads(data) == {"device": "cpu", "loaded": ["kokoro"], "voices": ["af_heart", "bf_emma"]}
    engine.loaded = False
    assert json.loads(call("GET", "/api/info")[2])["loaded"] == []


def test_speak_answers_with_a_24khz_mono_wav(service):
    engine, _, call = service
    status, kind, data = call("POST", "/api/speak", {"engine": "kokoro", "text": "Hi.", "voice": "bf_emma", "speed": 1.2})
    assert status == 200 and kind == "audio/wav"
    with wave.open(io.BytesIO(data)) as clip:
        assert (clip.getnchannels(), clip.getsampwidth(), clip.getframerate()) == (1, 2, 24000)
        assert clip.getnframes() == 2400 * 3
    assert engine.spoken == [("Hi.", "bf_emma", 1.2)]


@pytest.mark.parametrize("body", [
    {"text": ""}, {"text": "x" * 1001}, {"text": 5}, {"text": "Hi", "voice": "../af_heart"}, {"text": "Hi", "voice": "AF_HEART"},
    {"text": "Hi", "speed": 3}, {"text": "Hi", "speed": True}, {"text": "Hi", "engine": "other"}, ["not", "an", "object"],
])
def test_bad_requests_are_refused_before_any_speech(service, body):
    engine, _, call = service
    assert call("POST", "/api/speak", body)[0] == 400
    assert engine.spoken == []


def test_a_browser_page_cannot_use_it(service):
    engine, port, call = service
    # A simple cross-site form post (no preflight) and a page reaching it through its own domain name are both refused.
    assert call("POST", "/api/speak", b'{"text": "Hi"}', {"Content-Type": "text/plain"})[0] == 415
    assert call("POST", "/api/speak", {"text": "Hi"}, {"Host": f"attacker.example:{port}"})[0] == 403
    assert call("GET", "/api/info", None, {"Host": "attacker.example"})[0] == 403
    assert engine.spoken == []


def test_a_refused_request_is_answered_only_after_its_body_is_read(service):
    """Answering before the body is read lets Windows reset the connection, so the client sees no answer at all."""
    import socket
    _, port, _ = service
    body = b'{"text": "Hi"}'
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(b"POST /api/speak HTTP/1.1\r\nHost: attacker.example\r\nContent-Type: application/json\r\n"
                       b"Content-Length: %d\r\n\r\n" % len(body))
        client.settimeout(0.3)
        with pytest.raises(socket.timeout):
            client.recv(1)          # refused, but not before the announced body has arrived
        client.settimeout(5)
        client.sendall(body)
        answer = b""
        while chunk := client.recv(4096):
            answer += chunk
    assert answer.startswith(b"HTTP/1.0 403")


def test_a_large_body_or_unknown_path_is_refused(service):
    _, _, call = service
    assert call("POST", "/api/speak", b"{" + b" " * (serve.MAX_BODY + 1) + b"}")[0] == 413
    assert call("GET", "/api/other")[0] == 404
    assert call("POST", "/api/info", {"text": "Hi"})[0] == 404


def test_a_failure_says_so_without_its_detail(service):
    engine, _, call = service
    engine.fail = True
    status, _, data = call("POST", "/api/speak", {"text": "Hi"})
    assert status == 500 and b"private" not in data


def test_altera_speech_client_works_with_it(service, tmp_path, monkeypatch):
    """The real client in core/tts.py, against the real server: the contract both sides use."""
    _, port, _ = service
    monkeypatch.setattr(tts, "AUDIO_CACHE_DIR", str(tmp_path))
    tts._reset_state()
    cfg = tts.settings({"voice": {"tts_engine": "kokoro", "kokoro": {"url": f"http://127.0.0.1:{port}", "autostart": False}}})
    path = tts._kokoro_clip("Hello there.", cfg)
    with wave.open(path) as clip:
        assert clip.getframerate() == 24000
    assert json.loads(voice_http.read(cfg["kokoro"]["url"], "/api/info", timeout=5, limit=65536))["loaded"] == ["kokoro"]


def test_the_parent_check_sees_this_process_and_not_a_finished_one():
    import os
    import subprocess
    import sys
    assert serve.parent_alive(os.getpid()) is True
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert serve.parent_alive(child.pid) is False


def test_the_service_stops_when_its_parent_has_gone():
    stopped = threading.Event()

    class Server:
        def shutdown(self):
            stopped.set()

    alive = iter([True, False])
    original = serve.parent_alive
    serve.parent_alive = lambda pid: next(alive)
    try:
        serve.watch_parent(1, Server(), interval=0)
    finally:
        serve.parent_alive = original
    assert stopped.is_set()


def test_the_launch_flags_altera_passes_are_understood(monkeypatch):
    """The command core/voice_service.py runs: --port, --idle-minutes 0, --preload kokoro, --parent-pid."""
    seen = {"threads": []}

    class Server:
        def serve_forever(self):
            seen["served"] = True

        def server_close(self):
            pass

    class Thread:
        def __init__(self, target, args=(), daemon=None):
            self.name = target.__name__

        def start(self):
            seen["threads"].append(self.name)

    def make_server(port, engine):
        seen["port"] = port
        return Server()

    monkeypatch.setattr(serve, "make_server", make_server)
    monkeypatch.setattr(serve.threading, "Thread", Thread)
    assert serve.main(["--port", "8770", "--idle-minutes", "0", "--preload", "kokoro", "--parent-pid", "4242"]) == 0
    assert seen == {"threads": ["watch_parent", "preload"], "port": 8770, "served": True}


def test_download_prepares_pronunciation_so_the_first_spoken_reply_never_goes_online(monkeypatch, capsys):
    import sys
    import types
    fetched, pipelines = [], []
    hub = types.ModuleType("huggingface_hub")
    hub.hf_hub_download = lambda repo, name: fetched.append((repo, name))
    kokoro = types.ModuleType("kokoro")

    class KPipeline:
        def __init__(self, **kwargs):
            pipelines.append(kwargs)
    kokoro.KPipeline = KPipeline
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setitem(sys.modules, "kokoro", kokoro)
    for name in ("HF_HUB_DISABLE_SYMLINKS_WARNING", "HF_HUB_VERBOSITY"):   # set, then removed: restored afterwards
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    assert serve.main(["--download", "--voices", "af_heart,bf_emma,am_adam"]) == 0
    assert [name for _, name in fetched] == ["config.json", "kokoro-v1_0.pth", "voices/af_heart.pt",
                                             "voices/bf_emma.pt", "voices/am_adam.pt"]
    # One pipeline per language (American, British), without model weights: misaki installs spaCy's model now.
    assert pipelines == [{"lang_code": "a", "repo_id": serve.REPO, "model": False},
                         {"lang_code": "b", "repo_id": serve.REPO, "model": False}]
    # The token and symlink notices ask for things nobody needs here; errors still show.
    import os
    assert os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] == "1" and os.environ["HF_HUB_VERBOSITY"] == "error"
