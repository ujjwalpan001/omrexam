"""One queue for the CPU/memory-heavy work (grading scans, cleaning photos, compiling LaTeX).

A small server (e.g. 512 MB, a fraction of a CPU) can only afford a few of these at a time, so every heavy job
takes a slot first: extra uploads wait their turn instead of running together and running out of memory.
Settings (environment): OMR_WORKERS = jobs at once (default 1), OMR_QUEUE_MAX = uploads allowed to wait (default 20),
OMR_CV_THREADS = OpenCV threads per job (default 1; more threads only help on a machine with spare cores)."""
import os
import threading
from contextlib import contextmanager

import cv2
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

WORKERS = max(1, int(os.environ.get("OMR_WORKERS", "1")))
QUEUE_MAX = max(0, int(os.environ.get("OMR_QUEUE_MAX", "20")))
cv2.setNumThreads(max(1, int(os.environ.get("OMR_CV_THREADS", "1"))))

_slots = threading.BoundedSemaphore(WORKERS)
_held = threading.local()
_lock = threading.Lock()
_pending = 0                    # uploads running or waiting


@contextmanager
def slot():
    """Hold one worker slot for the block. Re-entrant: a heavy step inside another one (in the same thread) reuses it."""
    if getattr(_held, "depth", 0):
        _held.depth += 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    with _slots:
        _held.depth = 1
        try:
            yield
        finally:
            _held.depth = 0


def _in_slot(fn, args):
    with slot():
        return fn(*args)


async def run(fn, *args):
    """Run fn(*args) in a worker thread once a slot is free. Refuses with 503 when the queue is already full."""
    global _pending
    with _lock:
        if _pending >= WORKERS + QUEUE_MAX:
            raise HTTPException(503, "The server is busy with other scans - please try again in a minute", headers={"Retry-After": "30"})
        _pending += 1
    try:
        return await run_in_threadpool(_in_slot, fn, args)
    finally:
        with _lock:
            _pending -= 1
