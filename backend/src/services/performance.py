"""Request-scoped timings without prompts, URLs, credentials or model responses."""
import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from datetime import datetime, timezone
from functools import wraps


current_run = ContextVar('research_performance', default=None)
current_task = ContextVar('research_task', default=None)


class PerformanceRun:
    def __init__(self, persist, *, resumed=False, concurrency=2):
        self.persist = persist
        self.lock = threading.Lock()
        self.started = time.perf_counter()
        self.data = {'version': 1, 'started_at': datetime.now(timezone.utc).isoformat(),
                     'resumed': resumed, 'task_concurrency': concurrency,
                     'status': 'running', 'spans': [], 'counters': {}}

    def save(self):
        # Caller holds the lock; even late synchronous workers persist in order.
        self.data['observed_seconds'] = round(time.perf_counter() - self.started, 3)
        try:
            self.persist(self.data)
        except Exception:
            logging.getLogger(__name__).warning('Performance persistence unavailable')

    def record(self, stage, started, status, fields):
        with self.lock:
            self.data['spans'].append({'stage': stage, 'task_id': current_task.get(),
                'start_seconds': round(started - self.started, 3),
                'duration_seconds': round(time.perf_counter() - started, 3),
                'status': status, **fields})
            self.save()

    def count(self, name, value):
        with self.lock:
            counters = self.data['counters']
            counters[name] = counters.get(name, 0) + value

    def finish(self, status):
        with self.lock:
            self.data['status'] = status
            self.data['wall_seconds'] = round(time.perf_counter() - self.started, 3)
            self.save()


def count(name, value=1):
    recorder = current_run.get()
    if recorder:
        recorder.count(name, value)


@contextmanager
def span(stage):
    recorder = current_run.get()
    started, fields, status = time.perf_counter(), {}, 'ok'
    try:
        yield fields
    except BaseException:
        status = 'error_or_cancelled'
        raise
    finally:
        if recorder:
            recorder.record(stage, started, status, fields)


def measured(stage):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with span(stage):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def submit(executor, function, *args, **kwargs):
    """ThreadPoolExecutor does not propagate contextvars automatically."""
    return executor.submit(copy_context().run, function, *args, **kwargs)
