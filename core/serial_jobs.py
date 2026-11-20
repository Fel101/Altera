"""A lazy serial Future executor whose exact worker belongs to StateGate."""
from collections import deque
from concurrent.futures import Executor, Future
import threading


class OwnedSerialExecutor(Executor):
    def __init__(self, *, name, gate=None):
        if not isinstance(name, str) or not name:
            raise ValueError('An owned worker name is required.')
        if gate is None:
            from core.state_gate import STATE
            gate = STATE
        self._gate, self._name = gate, name
        self._condition = threading.Condition(threading.RLock())
        self._pending = deque()
        self._closed = False
        self._worker = None
        self._starting = False

    def submit(self, fn, /, *args, **kwargs):
        with self._condition:
            if self._closed:
                raise RuntimeError('Cannot schedule work after executor shutdown.')
            admission = self._gate.enter(self._name + '-queue')
            try:
                future = Future()
                self._pending.append((future, fn, args, kwargs))
                start = self._worker is None and not self._starting
                if start:
                    self._starting = True
                self._condition.notify()
            except BaseException:
                admission.release()
                raise
        try:
            if start:
                try:
                    # Let the new worker take ready speech immediately. Holding
                    # its queue lock across Thread.start delays first audio.
                    worker = self._gate.start_job(self._run, name=self._name)
                except BaseException as error:
                    with self._condition:
                        self._starting = False
                        pending, self._pending = self._pending, deque()
                        self._condition.notify_all()
                    for item, _, _, _ in pending:
                        if item.set_running_or_notify_cancel():
                            item.set_exception(error)
                    raise
                with self._condition:
                    self._worker = worker
                    self._starting = False
                    self._condition.notify_all()
            return future
        finally:
            admission.release()

    def _run(self):
        try:
            while True:
                with self._condition:
                    while not self._pending and not self._closed:
                        self._condition.wait()
                    if not self._pending:
                        return
                    future, fn, args, kwargs = self._pending.popleft()
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = fn(*args, **kwargs)
                except BaseException as error:
                    future.set_exception(error)
                else:
                    future.set_result(result)
        finally:
            # Unexpected callback failure must not leave unstarted Futures
            # silently queued on a worker that can no longer service them.
            with self._condition:
                self._closed = True
                pending, self._pending = self._pending, deque()
                self._condition.notify_all()
            for future, _, _, _ in pending:
                future.cancel()

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._condition:
            self._closed = True
            try:
                if cancel_futures:
                    while self._pending:
                        future, _, _, _ = self._pending.popleft()
                        future.cancel()
            finally:
                while wait and self._starting:
                    self._condition.wait()
                worker = self._worker
                self._condition.notify_all()
        if wait and worker is not None:
            worker.join()
