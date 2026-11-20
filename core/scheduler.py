"""Joinable, one-way periodic ownership; importing starts no threads or work."""
import math
import threading
import time


class OwnedScheduler:
    def __init__(self, app, *, name, label, service, logger, interval=30., before_cycle=None, gate=None):
        if (not isinstance(name, str) or not name or not isinstance(label, str) or not label
                or not callable(service) or before_cycle is not None and not callable(before_cycle)
                or type(interval) not in {int, float} or not math.isfinite(interval) or not .01 <= interval <= 3600):
            raise ValueError('Invalid scheduler ownership configuration.')
        if gate is None:
            from core.state_gate import STATE
            gate = STATE
        self._app, self._service, self._logger = app, service, logger
        self._name, self._label, self._interval, self._before, self._gate = name, label, interval, before_cycle, gate
        self._lock, self._stop = threading.RLock(), threading.Event()
        self._thread = None

    def start(self):
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError('A stopped scheduler cannot restart.')
            if self._thread is not None:
                return self
            thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._stop.set()
                self._thread = None
                raise
        return self

    def _loop(self):
        from core.state_gate import StateBusy
        while not self._stop.wait(self._interval):
            lease = None
            try:
                # Registration covers initialization, planning, generation,
                # parsing, publication and final cleanup, never the idle wait.
                with self._lock:
                    if self._stop.is_set():
                        return
                    lease = self._gate.enter(self._label)
                if self._before is not None:
                    self._before()
                with self._app.app_context():
                    service = self._service()
                    service.tick()
                    if not self._stop.is_set():
                        if self._before is not None:
                            self._before()
                        service.run_one()
            except StateBusy:
                if self._gate.draining:
                    self.set()
            except Exception:
                self._logger.exception('Owned scheduling failed safely')
            finally:
                if lease is not None:
                    lease.release()

    def set(self):
        """Event-compatible stop request; no joins, I/O or restart path."""
        with self._lock:
            self._stop.set()

    def is_set(self):
        return self._stop.is_set()

    def is_alive(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def close(self, *, timeout):
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('A finite close timeout is required.')
        deadline = time.monotonic() + timeout
        self.set()
        with self._lock:
            thread = self._thread
        if thread is threading.current_thread():
            return False
        if thread is not None:
            thread.join(timeout=max(0., deadline - time.monotonic()))
        return not self.is_alive()


def start_scheduler(app, *, key, register=None, **options):
    """Keep existing runtime idempotence; register ownership before starting."""
    if app.extensions.get(key) is not None:
        return app.extensions[key]
    owner = OwnedScheduler(app, **options)
    app.extensions[key] = owner
    if register is not None:
        register(owner)
    return owner.start()
