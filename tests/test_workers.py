"""The heavy-work queue: never more jobs at once than slots, a full queue answers 503, nested heavy steps do not deadlock."""
import asyncio
import threading
import time

import pytest
from fastapi import HTTPException

from app import workers


def test_jobs_wait_their_turn_and_a_full_queue_is_refused(monkeypatch):
    monkeypatch.setattr(workers, "QUEUE_MAX", 3)
    running, peak, lock = [0], [0], threading.Lock()

    def job():
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.2)
        with lock:
            running[0] -= 1
        return "ok"

    async def burst(n):
        return await asyncio.gather(*(workers.run(job) for _ in range(n)), return_exceptions=True)

    out = asyncio.run(burst(workers.WORKERS + 3 + 2))
    assert peak[0] <= workers.WORKERS
    assert out.count("ok") == workers.WORKERS + 3
    busy = [e for e in out if isinstance(e, HTTPException)]
    assert len(busy) == 2 and all(e.status_code == 503 for e in busy)
    assert asyncio.run(workers.run(job)) == "ok"                       # the queue empties again afterwards


def test_nested_heavy_steps_reuse_the_slot():
    def outer():
        with workers.slot():                                         # e.g. a scan that has to compile a sheet format
            with workers.slot():
                return "done"

    t = threading.Thread(target=lambda: [outer() for _ in range(workers.WORKERS + 1)])
    t.start()
    t.join(5)
    assert not t.is_alive()


def test_errors_reach_the_caller_and_free_the_slot():
    def boom():
        raise HTTPException(422, "bad sheet")

    with pytest.raises(HTTPException) as e:
        asyncio.run(workers.run(boom))
    assert e.value.status_code == 422
    assert asyncio.run(workers.run(lambda: 1)) == 1
