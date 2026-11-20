"""Actual disposable cooperative processes; no model or installed service runs."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from core.owned_process import OwnedChildProcess, OwnedProcessError

SERVICE = r'''
import argparse, ctypes, json, os, pathlib, subprocess, sys, time
p = argparse.ArgumentParser()
p.add_argument('--parent-pid', type=int, required=True)
p.add_argument('--root', required=True)
p.add_argument('--mode', default='normal')
a = p.parse_args()
root = pathlib.Path(a.root)
handle = None
if os.name == 'nt':
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, a.parent_pid)
(root/'ready.tmp').write_text(json.dumps({'pid':os.getpid(), 'parent_pid':a.parent_pid}))
os.replace(root/'ready.tmp', root/'ready.json')  # never seen half-written
print('synthetic service ready', flush=True)
if a.mode == 'output':
    print('x' * 20000, flush=True)
if a.mode == 'failed':
    sys.exit(7)
if os.name == 'nt':
    if handle:
        kernel.WaitForSingleObject(handle, 10000)
        kernel.CloseHandle(handle)
else:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try: os.kill(a.parent_pid, 0)
        except ProcessLookupError: break
        time.sleep(.01)
(root/'closing').touch()
if a.mode == 'hold':
    deadline = time.monotonic() + 6
    while not (root/'release').exists() and time.monotonic() < deadline:
        time.sleep(.01)
if a.mode == 'tail':
    code = "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); end=time.monotonic()+5\nwhile not p.exists() and time.monotonic()<end: time.sleep(.01)\nprint('synthetic tail closed',flush=True)"
    subprocess.Popen([sys.executable,'-I','-B','-S','-c',code,str(root/'release')], stdin=subprocess.DEVNULL)
print('synthetic service closed', flush=True)
'''


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.01)
    assert predicate(), 'Disposable service did not reach the expected state'


@pytest.fixture
def service(tmp_path):
    script = tmp_path / 'synthetic_service.py'
    script.write_text(SERVICE, 'utf-8')
    owners, lines = [], []
    def create(mode='normal', *, factory=None, output=None):
        owner = OwnedChildProcess(lambda parent: [sys.executable, str(script), '--parent-pid', str(parent),
            '--root', str(tmp_path), '--mode', mode], cwd=str(tmp_path), environment=os.environ,
            output=output or lines.append, process_factory=factory)
        owners.append(owner)
        return owner
    yield create, tmp_path, lines
    (tmp_path / 'release').touch()
    for owner in owners:
        assert owner.close(timeout=8), 'Disposable cooperative process retained resources'


def test_owned_process_is_inert_and_closes_only_its_cooperative_family(service):
    create, root, lines = service
    owner = create()
    assert owner.process is None and not (root / 'ready.json').exists()
    owner.start()
    wait_for(lambda: (root / 'ready.json').exists())
    ready = json.loads((root / 'ready.json').read_text())
    assert ready['parent_pid'] == owner._guardian.pid and ready['pid'] > 0
    assert owner.running and not owner.closed
    assert owner.close(timeout=3) and owner.closed and not owner.running
    assert owner.process.poll() == owner._guardian.poll() == 0
    assert owner._guardian.stdin.closed and owner.process.stdout.closed and not owner._reader.is_alive()
    assert 'synthetic service closed' in lines
    with pytest.raises(OwnedProcessError):
        owner.start()


def test_cooperative_timeout_retains_process_and_output_ownership_until_actual_exit(service):
    create, root, _ = service
    owner = create('hold').start()
    wait_for(lambda: (root / 'ready.json').exists())
    assert owner.close(timeout=.02) is False
    wait_for(lambda: (root / 'closing').exists())
    assert owner.stopping and owner.running and not owner.closed and owner.process.stdout is not None
    (root / 'release').touch()
    assert owner.close(timeout=3) and owner.process.poll() == 0


def test_pipe_inherited_by_a_test_tail_keeps_close_pending_after_service_exit(service):
    create, root, lines = service
    owner = create('tail').start()
    wait_for(lambda: (root / 'ready.json').exists())
    assert owner.close(timeout=.2) is False
    wait_for(lambda: owner.process.poll() is not None)
    assert not owner.closed and owner._reader.is_alive()
    (root / 'release').touch()
    assert owner.close(timeout=3)
    assert 'synthetic tail closed' in lines and owner.process.stdout.closed


def test_service_spawn_failure_preserves_and_closes_the_already_started_guardian(service):
    create, _, _ = service
    calls = []
    def factory(command, **kwargs):
        calls.append(command)
        if len(calls) == 2:
            raise OSError('Synthetic service spawn refusal')
        return subprocess.Popen(command, **kwargs)
    owner = create(factory=factory)
    with pytest.raises(OwnedProcessError):
        owner.start()
    assert owner._guardian is not None and owner.process is None
    assert owner.close(timeout=3) and owner._guardian.poll() == 0


def test_close_during_pending_spawn_returns_pending_and_does_not_lose_late_resources(service):
    create, _, _ = service
    entered, release, errors = threading.Event(), threading.Event(), []
    def factory(command, **kwargs):
        entered.set()
        assert release.wait(5)
        return subprocess.Popen(command, **kwargs)
    owner = create(factory=factory)
    def start():
        try:
            owner.start()
        except OwnedProcessError:
            errors.append('cancelled')
    starter = threading.Thread(target=start)
    starter.start()
    try:
        assert entered.wait(3)
        before = time.monotonic()
        assert owner.close(timeout=.01) is False and time.monotonic() - before < .5
    finally:
        release.set()
        starter.join(timeout=3)
    assert not starter.is_alive() and errors == ['cancelled']
    assert owner.close(timeout=3) and owner.process is None and owner._guardian.poll() == 0


def test_output_callback_failure_still_drains_and_closes_bounded_output(service):
    create, root, _ = service
    seen = []
    def broken(line):
        seen.append(line)
        raise RuntimeError('Synthetic log consumer refusal')
    owner = create('output', output=broken).start()
    wait_for(lambda: (root / 'ready.json').exists())
    assert owner.close(timeout=3) and owner.output_failed
    assert len(seen) == 1 and len(seen[0]) <= 4096


def test_reader_start_failure_can_close_the_real_unread_process_pipe(service, monkeypatch):
    create, _, _ = service
    owner = create()
    original_start = threading.Thread.start
    def start(thread):
        if thread.name == 'altera-owned-service-log':
            raise RuntimeError('Synthetic log thread startup refusal')
        return original_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    with pytest.raises(OwnedProcessError):
        owner.start()
    assert owner.process is not None and owner.close(timeout=3)
    assert owner.process.poll() == 0 and owner.process.stdout.closed


def test_natural_service_failure_is_distinct_from_confirmed_resource_closure(service):
    create, root, _ = service
    owner = create('failed').start()
    wait_for(lambda: (root / 'ready.json').exists())
    wait_for(lambda: not owner.running)
    assert not owner.closed and owner.process.poll() == 7
    assert owner.close(timeout=3) and owner.closed and owner.process.poll() == 7
