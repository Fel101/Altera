"""Owned external service with cooperative parent-exit shutdown and bounded joins.

The command must honor its supplied parent PID. No process discovery/adoption or
terminate/kill fallback is provided. This is not a native storage sandbox.
"""
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

_GUARDIAN = 'import sys; sys.stdin.buffer.read()'


class OwnedProcessError(RuntimeError):
    def __init__(self):
        super().__init__('The owned service could not start or close its verified resources.')


def _timeout(value):
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise ValueError('A finite nonnegative service timeout is required.')
    return time.monotonic() + value


def _guardian_runtime():
    if os.name == 'nt':
        base = Path(sys.base_prefix).resolve(strict=True)
        runtime = Path(getattr(sys, '_base_executable', '') or base / 'python.exe').resolve(strict=True)
        if not runtime.is_relative_to(base) or runtime.name.casefold() != 'python.exe':
            raise OwnedProcessError()
        return runtime
    return Path(sys.executable).resolve(strict=True)


class OwnedChildProcess:
    def __init__(self, command, *, cwd, environment, output, process_factory=None):
        if not callable(command) or not callable(output):
            raise ValueError('An owned service command and output handler are required.')
        self._command, self._cwd, self._environment, self._output = command, cwd, dict(environment), output
        self._factory = process_factory or subprocess.Popen
        self._lock, self._close_lock = threading.RLock(), threading.Lock()
        self._guardian = self._process = self._reader = None
        self._started = self._starting = self._stopping = self._closed = False
        self._output_failed = False
        self._reader_closed = False

    @property
    def process(self):
        return self._process

    @property
    def running(self):
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def starting(self):
        with self._lock:
            return self._starting

    @property
    def stopping(self):
        with self._lock:
            return self._stopping and not self._closed

    @property
    def closed(self):
        with self._lock:
            return self._closed

    @property
    def output_failed(self):
        with self._lock:
            return self._output_failed

    def start(self):
        with self._lock:
            if self._started or self._closed:
                raise OwnedProcessError()
            self._started = True
            self._starting = True
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        try:
            guardian_env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ}
            self._guardian = self._factory([str(_guardian_runtime()), '-I', '-B', '-S', '-c', _GUARDIAN],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=guardian_env, creationflags=flags)
            with self._lock:
                if self._stopping or self._guardian.stdin is None or self._guardian.poll() is not None:
                    raise OwnedProcessError()
            command = self._command(self._guardian.pid)
            if (not isinstance(command, (list, tuple)) or not command
                    or any(not isinstance(value, str) or not value or '\0' in value for value in command)):
                raise OwnedProcessError()
            with self._lock:
                if self._stopping:
                    raise OwnedProcessError()
            self._process = self._factory(list(command), cwd=self._cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                env=self._environment, creationflags=flags)
            if self._process.stdout is None:
                raise OwnedProcessError()
            self._reader = threading.Thread(target=self._read_output, name='altera-owned-service-log', daemon=True)
            self._reader.start()
        except BaseException as error:
            # Keep all created owners for an explicit bounded close attempt.
            with self._lock:
                self._stopping = True
            if isinstance(error, Exception):
                raise OwnedProcessError() from None
            raise
        finally:
            with self._lock:
                self._starting = False
        with self._lock:
            if self._stopping:
                raise OwnedProcessError()
        return self

    def _read_output(self):
        stream = self._process.stdout
        try:
            while True:
                line = stream.readline(4096)
                if not line:
                    break
                if not self._output_failed:
                    try:
                        self._output(line.rstrip())
                    except Exception:
                        # Continue draining the pipe even if its log consumer
                        # fails; a full stdout pipe must not strand the service.
                        self._output_failed = True
        finally:
            try:
                stream.close()
            finally:
                self._reader_closed = stream.closed

    @staticmethod
    def _reap(guardian):
        """Collect the guardian as soon as it exits, even if this close gives up first.

        On Linux and macOS an exited child stays in the process table (a zombie) until
        its parent waits for it, and the service's os.kill(pid, 0) check still finds it.
        Waiting in the background means a close that times out cannot strand the service.
        Windows has no zombies, so nothing changes there.
        """
        if os.name == 'nt':
            return
        def wait():
            try:
                guardian.wait()
            except Exception:
                pass  # close() reports a guardian that did not end.
        try:
            threading.Thread(target=wait, name='altera-owned-guardian-reaper', daemon=True).start()
        except RuntimeError:
            pass  # close() still waits for the guardian before the service.

    def close(self, *, timeout):
        deadline = _timeout(timeout)
        if not self._close_lock.acquire(timeout=max(0., deadline - time.monotonic())):
            return False
        try:
            with self._lock:
                if self._closed:
                    return True
                self._stopping = True
                guardian, process, reader, starting = self._guardian, self._process, self._reader, self._starting
            try:
                if guardian is not None and guardian.stdin is not None and not guardian.stdin.closed:
                    guardian.stdin.close()  # EOF ends only our guardian, never an unknown service.
                    self._reap(guardian)
                if starting:
                    return False  # A spawn still in flight can publish another owned resource.
                # The guardian first: the service waits for the guardian's PID to disappear.
                for owned in (guardian, process):
                    if owned is not None:
                        owned.wait(timeout=max(0., deadline - time.monotonic()))
                if reader is not None and reader.ident is not None:
                    if reader is threading.current_thread():
                        return False
                    reader.join(timeout=max(0., deadline - time.monotonic()))
                    if reader.is_alive() or not self._reader_closed:
                        return False
                elif process is not None and process.stdout is not None:
                    process.stdout.close()  # No reader was successfully started.
                if any(owned is not None and owned.poll() is None for owned in (process, guardian)):
                    return False
                if process is not None and process.stdout is not None and not process.stdout.closed:
                    return False
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                return False
            with self._lock:
                self._closed = True
            return True
        finally:
            self._close_lock.release()
