"""Voice startup owns only its launch, survives pending closure and deduplicates."""
from types import SimpleNamespace
import sys
import threading

import pytest

from core import state_gate, tts, voice_service
from core.owned_process import OwnedProcessError
from test_owned_process import SERVICE, wait_for

_INFO = voice_service.info


def test_the_launcher_is_altera_own_voice_service_beside_the_source(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, 'PROJECT_ROOT', tmp_path / 'Altera')
    cfg = tts.settings({'voice': {'tts_engine': 'kokoro'}})
    folder = tmp_path / 'Altera' / 'voice'
    python = folder / '.venv' / ('Scripts/python.exe' if voice_service.os.name == 'nt' else 'bin/python')

    assert voice_service._launcher(cfg) == (python, folder / 'serve.py')
    assert voice_service.installed(cfg) is False
    python.parent.mkdir(parents=True)
    python.touch()
    (folder / 'serve.py').touch()
    assert voice_service.installed(cfg) is True


def test_configured_paths_start_another_compatible_service(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, 'PROJECT_ROOT', tmp_path / 'Altera')
    other_python, other_script = tmp_path / 'lab' / 'python.exe', tmp_path / 'lab' / 'serve.py'
    cfg = tts.settings({'voice': {'tts_engine': 'kokoro', 'kokoro': {'python': str(other_python), 'script': str(other_script)}}})

    assert voice_service._launcher(cfg) == (other_python, other_script)
    only_python = tts.settings({'voice': {'tts_engine': 'kokoro', 'kokoro': {'python': str(other_python)}}})
    assert voice_service._launcher(only_python)[1] == tmp_path / 'Altera' / 'voice' / 'serve.py'


def test_a_voice_that_is_not_installed_says_how_to_install_it(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, 'PROJECT_ROOT', tmp_path / 'Altera')
    monkeypatch.setattr(voice_service, 'info', lambda cfg=None, timeout=0.7: None)
    cfg = tts.settings({'voice': {'tts_engine': 'kokoro'}})

    status = voice_service.status(cfg)
    assert status['state'] == 'down' and 'not installed' in status['message'] and 'voice\\setup.bat' in status['message']
    manual = tts.settings({'voice': {'tts_engine': 'kokoro', 'kokoro': {'autostart': False}}})
    assert voice_service.status(manual)['message'].endswith('is not running')


@pytest.fixture
def voice_owner(tmp_path, monkeypatch):
    gate = state_gate.StateGate()
    monkeypatch.setattr(state_gate, 'STATE', gate)
    for name, value in [('_owner', None), ('_owner_key', None), ('_proc', None), ('_closing', False),
                        ('_last_start', 0.), ('_atexit_registered', True), ('_starter_busy', threading.Event())]:
        monkeypatch.setattr(voice_service, name, value)
    monkeypatch.setattr(voice_service, 'info', lambda cfg=None, timeout=.7: None)
    script = tmp_path / 'synthetic_voice.py'
    script.write_text(SERVICE.replace("p.add_argument('--root', required=True)",
        "p.add_argument('--root', default=str(pathlib.Path(__file__).resolve().parent))").replace(
        'a = p.parse_args()', 'a = p.parse_known_args()[0]'), 'utf-8')
    cfg = tts.settings({'voice': {'tts_engine': 'kokoro', 'kokoro': {'autostart': True,
        'url': 'http://127.0.0.1:8899', 'python': sys.executable, 'script': str(script)}}})
    releases = []
    yield SimpleNamespace(cfg=cfg, root=tmp_path, gate=gate, releases=releases)
    for release in releases:
        release.set()
    assert gate.join_jobs(timeout=3)
    assert voice_service.stop(timeout=8), 'Disposable voice owner retained resources'


def test_real_voice_owner_uses_cooperative_parent_and_preserves_binding_until_close(voice_owner):
    w = voice_owner
    assert voice_service.start(w.cfg) == 'started'
    wait_for(lambda: (w.root / 'ready.json').exists())
    owner = voice_service._owner
    assert owner.running and voice_service._proc is owner.process
    assert voice_service.start(w.cfg) == 'started' and voice_service._owner is owner
    changed = {**w.cfg, 'kokoro': {**w.cfg['kokoro'], 'url': 'http://127.0.0.1:8898'}}
    assert voice_service.start(changed) == 'waiting' and voice_service._owner is owner
    assert voice_service.stop(timeout=3)
    assert owner.closed and owner.process.poll() == 0 and voice_service._owner is None
    assert voice_service.start(w.cfg, force=True) == 'stopping'


def test_already_running_external_service_is_never_adopted_or_closed(voice_owner, monkeypatch):
    monkeypatch.setattr(voice_service, 'info', lambda *a, **k: {'loaded': ['kokoro']})
    monkeypatch.setattr(voice_service, 'OwnedChildProcess', lambda *a, **k: pytest.fail('Adopted an external service'))
    assert voice_service.start(voice_owner.cfg) == 'running' and voice_service._owner is None
    assert voice_service.stop(timeout=0)
    assert voice_service.status(voice_owner.cfg)['message'] == 'Voice startup is stopped for this Altera process.'


def test_pending_close_retains_owner_and_never_uses_legacy_process_force_methods(voice_owner, monkeypatch):
    w = voice_owner
    class PendingOwner:
        closed = stopping = starting = False
        running = True
        process = SimpleNamespace()
        allowed = False
        def __init__(self, *a, **k):
            pass
        def start(self):
            return self
        def close(self, *, timeout):
            self.stopping = True
            self.closed = self.allowed
            return self.closed
    monkeypatch.setattr(voice_service, 'OwnedChildProcess', PendingOwner)
    assert voice_service.start(w.cfg) == 'started'
    owner = voice_service._owner
    assert voice_service.stop(timeout=0) is False
    assert voice_service._owner is owner and voice_service._proc is owner.process
    assert voice_service.status(w.cfg)['state'] == 'stopping'
    owner.allowed = True
    assert voice_service.stop(timeout=0) and voice_service._owner is None


def test_duplicate_start_while_first_launch_is_pending_does_not_cancel_it(voice_owner, monkeypatch):
    w = voice_owner
    entered, release, results = threading.Event(), threading.Event(), []
    w.releases.append(release)
    class PendingStart:
        starting = True
        stopping = closed = False
        running = False
        process = None
        def __init__(self, *a, **k):
            pass
        def start(self):
            entered.set()
            assert release.wait(5)
            self.process = SimpleNamespace()
            self.running, self.starting = True, False
        def close(self, *, timeout):
            assert not self.starting, 'A duplicate launch cancelled startup'
            self.closed = True
            return True
    monkeypatch.setattr(voice_service, 'OwnedChildProcess', PendingStart)
    starter = threading.Thread(target=lambda: results.append(voice_service.start(w.cfg)))
    starter.start()
    try:
        assert entered.wait(3)
        owner = voice_service._owner
        assert voice_service.start(w.cfg) == 'started' and voice_service._owner is owner
    finally:
        release.set()
        starter.join(timeout=3)
    assert not starter.is_alive() and results == ['started']


def test_partial_start_failure_retains_its_owner_until_cleanup_is_confirmed(voice_owner, monkeypatch):
    class FailedOwner:
        starting = stopping = closed = False
        running = False
        process = None
        allowed = False
        def __init__(self, *a, **k):
            pass
        def start(self):
            raise OwnedProcessError()
        def close(self, *, timeout):
            self.stopping = True
            self.closed = self.allowed
            return self.closed
    monkeypatch.setattr(voice_service, 'OwnedChildProcess', FailedOwner)
    assert voice_service.start(voice_owner.cfg).startswith('failed:')
    owner = voice_service._owner
    assert owner is not None and not owner.closed
    assert voice_service.stop(timeout=0) is False and voice_service._owner is owner
    owner.allowed = True
    assert voice_service.stop(timeout=0)


def test_async_start_failure_clears_busy_and_retry_uses_frozen_settings(voice_owner, monkeypatch):
    w = voice_owner
    monkeypatch.delenv('ALTERA_DISABLE_AUTOSTART', raising=False)
    def refuse(*a, **k):
        raise RuntimeError('Synthetic startup-thread refusal')
    with monkeypatch.context() as failure:
        failure.setattr(threading.Thread, 'start', refuse)
        assert voice_service.ensure_started_async(w.cfg) is False
    assert not voice_service._starter_busy.is_set() and not w.gate.active and w.gate.owned_jobs == 0
    entered, release, received = threading.Event(), threading.Event(), []
    w.releases.append(release)
    def start(cfg):
        entered.set()
        assert release.wait(5)
        received.append(cfg['kokoro']['url'])
        return 'disabled'
    monkeypatch.setattr(voice_service, 'start', start)
    assert voice_service.ensure_started_async(w.cfg)
    assert entered.wait(3)
    original = w.cfg['kokoro']['url']
    w.cfg['kokoro']['url'] = 'http://127.0.0.1:8897'
    assert voice_service.ensure_started_async(w.cfg) is False
    release.set()
    assert w.gate.join_jobs(timeout=3) and received == [original]
    assert not voice_service._starter_busy.is_set()


def test_legacy_untracked_process_reference_is_not_adopted_or_cleared(voice_owner, monkeypatch):
    class Unknown:
        def terminate(self):
            pytest.fail('Terminated an untracked process')
        kill = terminate
    unknown = Unknown()
    with monkeypatch.context() as legacy:
        legacy.setattr(voice_service, '_proc', unknown)
        assert voice_service.stop(timeout=0) is False
        assert voice_service._proc is unknown and voice_service.status(voice_owner.cfg)['state'] == 'stopping'


def test_disabled_or_rate_limited_autostart_creates_no_background_job(voice_owner, monkeypatch):
    w = voice_owner
    monkeypatch.delenv('ALTERA_DISABLE_AUTOSTART', raising=False)
    disabled = {**w.cfg, 'kokoro': {**w.cfg['kokoro'], 'autostart': False}}
    assert voice_service.ensure_started_async(disabled) is False
    monkeypatch.setattr(voice_service, '_last_start', voice_service.time.time())
    assert voice_service.ensure_started_async(w.cfg) is False
    assert not w.gate.active and w.gate.owned_jobs == 0 and not voice_service._starter_busy.is_set()


def test_voice_metadata_read_is_bounded_and_oversized_output_is_unavailable(voice_owner, monkeypatch):
    reads = []
    class Response:
        status = 200
        headers = {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            reads.append(limit)
            return b'x' * limit
    opener = SimpleNamespace(open=lambda *a, **k: Response())
    monkeypatch.setattr(voice_service.voice_http.urllib.request, 'build_opener', lambda *a: opener)
    assert _INFO(voice_owner.cfg) is None and reads == [65537]
