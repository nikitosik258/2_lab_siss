"""Reliable asyncio queue worker for dynamic inference batching."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from config import (
    INFERENCE_TIMEOUT_SECONDS,
    MAX_BATCH_SIZE,
    MAX_BATCH_WAIT_MS,
    QUEUE_MAX_SIZE,
)
from inference import model_predict_batch
from schemas import FarmRequest


logger = logging.getLogger(__name__)
BatchPredictor = Callable[[list[FarmRequest]], Awaitable[list[float]]]


class InferenceExecutionTimeout(Exception):
    """The model call exceeded its execution deadline."""


@dataclass
class InferenceTask:
    data: FarmRequest
    request_id: str
    future: asyncio.Future[float]
    enqueued_at: float


class DynamicBatcher:
    """Own one bounded queue and one long-lived dynamic batch worker."""

    def __init__(
        self,
        *,
        model_predictor: BatchPredictor = model_predict_batch,
        max_batch_size: int = MAX_BATCH_SIZE,
        max_batch_wait_ms: int = MAX_BATCH_WAIT_MS,
        queue_max_size: int = QUEUE_MAX_SIZE,
        inference_timeout_seconds: float = INFERENCE_TIMEOUT_SECONDS,
    ) -> None:
        self.model_predictor = model_predictor
        self.max_batch_size = max_batch_size
        self.max_batch_wait_ms = max_batch_wait_ms
        self.queue_max_size = queue_max_size
        self.inference_timeout_seconds = inference_timeout_seconds
        self.queue: asyncio.Queue[InferenceTask] = asyncio.Queue(
            maxsize=queue_max_size
        )
        self.worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(
                self._worker_loop(),
                name="dynamic-batch-worker",
            )
            logger.info("Dynamic batch worker started")

    async def stop(self) -> None:
        """Cancel the worker and every Future still owned by this batcher."""

        if self.worker_task is not None and not self.worker_task.done():
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass

        self.worker_task = None

        while True:
            try:
                task = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            if not task.future.done():
                task.future.cancel()
            self.queue.task_done()

        await self.queue.join()
        logger.info("Dynamic batch worker stopped")

    def submit(self, data: FarmRequest, request_id: str) -> asyncio.Future[float]:
        """Put one request into the bounded queue without spawning a task."""

        loop = asyncio.get_running_loop()
        future: asyncio.Future[float] = loop.create_future()
        task = InferenceTask(
            data=data,
            request_id=request_id,
            future=future,
            enqueued_at=time.perf_counter(),
        )

        try:
            self.queue.put_nowait(task)
        except asyncio.QueueFull:
            future.cancel()
            raise

        return future

    async def _worker_loop(self) -> None:
        while True:
            first_task = await self.queue.get()
            acquired = [first_task]

            try:
                deadline = time.perf_counter() + self.max_batch_wait_ms / 1000

                while len(acquired) < self.max_batch_size:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        break

                    try:
                        task = await asyncio.wait_for(
                            self.queue.get(),
                            timeout=remaining,
                        )
                    except asyncio.TimeoutError:
                        break

                    acquired.append(task)

                active = [task for task in acquired if not task.future.done()]
                if not active:
                    continue

                logger.info(
                    "Dynamic batch formed | batch_size=%s | queue_size=%s | "
                    "max_queue_wait_ms=%.2f",
                    len(active),
                    self.queue.qsize(),
                    max(
                        (time.perf_counter() - task.enqueued_at) * 1000
                        for task in active
                    ),
                )

                try:
                    scores = await asyncio.wait_for(
                        self.model_predictor([task.data for task in active]),
                        timeout=self.inference_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    error = InferenceExecutionTimeout(
                        "Inference execution timeout"
                    )
                    self._set_exception(active, error)
                    continue
                except Exception as exc:
                    logger.exception("Dynamic batch inference failed")
                    self._set_exception(active, exc)
                    continue

                if len(scores) != len(active):
                    self._set_exception(
                        active,
                        RuntimeError("Model returned an unexpected result count"),
                    )
                    continue

                for task, score in zip(active, scores):
                    if not task.future.done():
                        task.future.set_result(score)

            except asyncio.CancelledError:
                for task in acquired:
                    if not task.future.done():
                        task.future.cancel()
                raise
            finally:
                for _ in acquired:
                    self.queue.task_done()

    @staticmethod
    def _set_exception(tasks: list[InferenceTask], exc: Exception) -> None:
        for task in tasks:
            if not task.future.done():
                task.future.set_exception(exc)
