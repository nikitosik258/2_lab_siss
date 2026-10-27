"""Reusable inference entry points for explicit and future dynamic batches."""

import asyncio
from weakref import WeakKeyDictionary

from model import calculate_risk
from schemas import FarmRequest


# FastAPI uses one event loop per application process, so this lock is shared
# by single, explicit and dynamic inference. A separate lock per loop keeps
# isolated event-loop tests safe without weakening production serialization.
_model_locks: WeakKeyDictionary = WeakKeyDictionary()


def _get_model_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _model_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _model_locks[loop] = lock
    return lock


async def _calculate_batch_scores(items: list[FarmRequest]) -> list[float]:
    """Keep the deterministic business calculation separate from locking."""

    return [calculate_risk(item) for item in items]


async def model_predict_batch(items: list[FarmRequest]) -> list[float]:
    """Calculate scores for one model invocation while preserving item order.

    The shared lock models one computational resource. It is also the single
    extension point for replacing the lightweight calculation with real model I/O.
    """

    async with _get_model_lock():
        return await _calculate_batch_scores(items)


async def model_predict_single(item: FarmRequest) -> float:
    """Use the same reusable model entry point for one prediction."""

    return (await model_predict_batch([item]))[0]
