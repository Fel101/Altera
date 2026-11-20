"""
Altera — the local voice service.

The Kokoro voice runs in its own Python environment, because Altera's own environment has no PyTorch: voice/serve.py in
voice/.venv, which voice/setup.bat or voice/setup.sh installs. voice.kokoro.python and voice.kokoro.script start another
compatible service instead. Altera talks to it over http on 127.0.0.1 only. When Altera starts it starts the service in the
background (voice.kokoro.autostart). An owned guardian supplies its parent-exit signal; shutdown waits for actual
process/output-reader closure and can remain pending. Existing independently running services are never adopted.
"""

import logging
import atexit
import copy
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from core import env, tts, voice_http
from core.owned_process import OwnedChildProcess

LOGGER = logging.getLogger(__name__)

START_RETRY_S = 60                      # do not start the service more often than this if it keeps failing
LOOPBACK = ("127.0.0.1", "localhost", "::1")
INSTALL_HINT = "Run voice\\setup.bat (Windows) or sh voice/setup.sh to install it."

_lock = threading.Lock()
_proc = None
_owner = None
_owner_key = None
_closing = False
_last_start = 0.0
_atexit_registered = False


def _service_environment():
    environment = {key: value for key, value in os.environ.items()
                   if not env.is_api_key(key)}
    environment.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    return environment


def _log_line(line):
    LOGGER.info('Voice service: %s', line)


def _launcher(cfg):
    """(python, script) for the voice service: voice.kokoro.python and .script when set, else Altera's own voice/serve.py
    in voice/.venv beside the source."""
    k = cfg["kokoro"]
    folder = tts.PROJECT_ROOT / "voice"
    own_python = folder / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return (Path(k["python"]) if k["python"] else own_python,
            Path(k["script"]) if k["script"] else folder / "serve.py")


def installed(cfg=None):
    """Whether the voice service Altera would start is there."""
    python, script = _launcher(cfg or tts.settings())
    return python.is_file() and script.is_file()


def info(cfg=None, timeout=0.7):
    """What the service says about itself ({"device", "loaded": [...]}), or None when it cannot be reached."""
    cfg = cfg or tts.settings()
    try:
        raw = voice_http.read(cfg['kokoro']['url'], '/api/info', timeout=timeout, limit=65536)
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def status(cfg=None, ensure_started=False):
    """{"state": cloud | ready | warming | starting | down, "message": text} for the status light."""
    cfg = cfg or tts.settings()
    if cfg["engine"] != "kokoro":
        return {"state": "cloud", "message": tts.voice_label(cfg)}
    label = tts.voice_label(cfg)
    with _lock:
        if _closing or _owner is not None and _owner.stopping:
            pending = _owner is not None or _proc is not None
            return {'state': 'stopping' if pending else 'down',
                    'message': f'{label} is closing' if pending else 'Voice startup is stopped for this Altera process.'}
    data = info(cfg)
    if data is not None:
        if "kokoro" in (data.get("loaded") or []):
            return {"state": "ready", "message": f"{label} ready"}
        return {"state": "warming", "message": f"{label} is loading (about 30 seconds)"}
    with _lock:
        starting = _proc is not None and _proc.poll() is None
    if starting:
        return {"state": "starting", "message": f"{label} is starting"}
    if ensure_started:
        ensure_started_async(cfg)
    if cfg["kokoro"]["autostart"] and not installed(cfg):
        return {"state": "down", "message": f"{label} is not installed. {INSTALL_HINT}"}
    return {"state": "down", "message": f"{label} is not running"}


def problem(cfg=None):
    """Why speech in cfg's voice was not made, in words for the person (a sample that stayed silent). Never starts anything."""
    cfg = cfg or tts.settings()
    if cfg["engine"] != "kokoro":
        return "Microsoft's online voice did not answer. Check the internet connection and try again."
    voice = cfg["kokoro"]["voice"]
    from core.pals import voice_name
    name = voice_name(voice)
    data = info(cfg)
    if data is None:
        if not installed(cfg):
            return f"The local voice is not installed. {INSTALL_HINT}"
        return "The local voice service is not running yet. Altera is starting it: try again in a minute."
    if "kokoro" not in (data.get("loaded") or []):
        return "The local voice is still loading. Try again in about 30 seconds."
    voices = data.get("voices")
    if isinstance(voices, list) and voices and voice not in voices:
        if cfg["kokoro"]["python"] or cfg["kokoro"]["script"]:
            return f"{name} is not installed in your voice service. Add the voice {voice} to it, or choose another."
        return (f"{name} is not installed in the voice service. Add it with "
                f"voice\\.venv\\Scripts\\python.exe voice\\serve.py --download --voices {voice}, or choose another.")
    return f"The local voice could not speak as {name}. The voice service's log says why."


_LIGHT = {"ready": "ok", "cloud": "ok", "warming": "warming", "starting": "warming", 'stopping': 'warming'}


def light(state):
    """The status-light colour for a status state: ok (green), warming (amber: loading) or error (red: not running)."""
    return _LIGHT.get(state, "error")


def start(cfg=None, force=False):
    """Start the service if it is not already up. Returns what happened, as a short word:

    running (already up), started, disabled (not the kokoro engine, autostart off, or not a local address),
    no-launcher, waiting, stopping, or a fixed startup failure description.
    """
    global _proc, _owner, _owner_key, _last_start, _atexit_registered
    cfg = cfg or tts.settings()
    if cfg["engine"] != "kokoro" or not cfg["kokoro"]["autostart"]:
        return "disabled"
    try:
        parsed = urlparse(cfg["kokoro"]["url"])
        port = parsed.port
    except (TypeError, ValueError):
        return 'disabled'
    if parsed.scheme not in {'http', 'https'} or parsed.hostname not in LOOPBACK or not port:
        return "disabled"
    with _lock:
        if _closing:
            return 'stopping'
    if info(cfg) is not None:
        return "running"
    python, script = _launcher(cfg)
    if not python.exists() or not script.exists():
        return "no-launcher"
    python, script = python.resolve(), script.resolve()
    identity = (cfg['kokoro']['url'], str(python), str(script))
    with _lock:
        if _closing:
            return 'stopping'
        if _owner is not None:
            if not _owner.stopping and (_owner.starting or _owner.process is None or _owner.running):
                return 'started' if _owner_key == identity else 'waiting'
            if not _owner.close(timeout=0):
                return 'stopping'
            _owner = _proc = _owner_key = None
        if not force and time.time() - _last_start < START_RETRY_S:
            return "waiting"
        _last_start = time.time()
        owner = OwnedChildProcess(lambda parent: [str(python), str(script), '--port', str(port), '--idle-minutes', '0',
            '--preload', 'kokoro', '--parent-pid', str(parent)], cwd=str(script.parent),
            environment=_service_environment(), output=_log_line)
        _owner, _owner_key = owner, identity
        if not _atexit_registered:
            atexit.register(stop)
            _atexit_registered = True
    # Publish ownership before spawning; Stop can fence a concurrent startup.
    try:
        owner.start()
    except Exception:
        owner.close(timeout=5)
        with _lock:
            if _owner is owner and owner.closed:
                _owner = _proc = _owner_key = None
            return 'stopping' if _closing else 'failed: The voice service could not start.'
    with _lock:
        if _owner is owner:
            _proc = owner.process
        return 'stopping' if _closing else 'started'


_starter_busy = threading.Event()


def _queue_start(cfg=None, *, announce=False):
    if env.get('DISABLE_AUTOSTART') == '1':
        return False
    cfg = copy.deepcopy(cfg or tts.settings())
    if cfg.get('engine') != 'kokoro' or not cfg.get('kokoro', {}).get('autostart'):
        return False
    with _lock:
        if (_closing or _starter_busy.is_set()
                or _owner is None and time.time() - _last_start < START_RETRY_S):
            return False
        _starter_busy.set()

    def run():
        try:
            result = start(cfg)
            if announce:
                LOGGER.warning('Voice: %s — %s', tts.voice_label(cfg), result)
        except Exception:
            LOGGER.exception('Voice startup failed')
        finally:
            _starter_busy.clear()

    try:
        from core.state_gate import start_job
        start_job(run, name='altera-voice-start')
    except Exception:
        _starter_busy.clear()
        LOGGER.debug('Voice startup admission was refused', exc_info=True)
        return False
    return True


def ensure_started_async(cfg=None):
    """Queue one owned, finite startup attempt; never start after shutdown."""
    return _queue_start(cfg)


def start_in_background():
    """Called once when Altera starts. Prints one line about what it is doing and returns at once."""
    if env.get('DISABLE_AUTOSTART') == '1':
        return
    cfg = tts.settings()
    if cfg["engine"] != "kokoro":
        LOGGER.warning(f"  Voice: {tts.voice_label(cfg)} (sends replies to Microsoft to be spoken)")
        return

    return _queue_start(cfg, announce=True)


def begin_drain():
    """Latch the startup fence without waiting or closing an active service."""
    global _closing
    _closing = True


def resources_closed():
    with _lock:
        return (_owner is None or _owner.closed) and _proc is None and not _starter_busy.is_set()


def stop(timeout=5):
    """Fence startup and cooperatively close only this process's retained owner."""
    global _proc, _owner, _owner_key
    begin_drain()
    with _lock:
        owner = _owner
    if owner is None:
        return _proc is None
    closed = owner.close(timeout=timeout)
    if closed:
        with _lock:
            if _owner is owner:
                _owner = _proc = _owner_key = None
    return closed
