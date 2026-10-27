import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import database
import inference
import main
from batching import DynamicBatcher, InferenceExecutionTimeout
from main import app
from schemas import FarmRequest


def farm(number: int) -> FarmRequest:
    return FarmRequest(
        farm_id=f"QUEUED-{number:03}",
        region="Krasnodar",
        crop_type="wheat",
        area_ha=100,
        temperature_avg=20,
        precipitation_mm=120,
        payment_delay_days=number,
        previous_defaults=0,
        debt=0,
    )


def farm_json(number: int) -> dict:
    return farm(number).model_dump()


@pytest.fixture
def queued_db_path(tmp_path, monkeypatch):
    path = tmp_path / "queued_test.db"
    monkeypatch.setattr(database, "DATABASE_PATH", path)
    return path


@pytest.fixture
def queued_client(queued_db_path):
    with TestClient(app) as test_client:
        yield test_client


def test_worker_really_groups_requests_and_preserves_future_mapping():
    async def scenario():
        calls = []

        async def predictor(items):
            calls.append([item.farm_id for item in items])
            return [float(item.payment_delay_days) for item in items]

        batcher = DynamicBatcher(
            model_predictor=predictor,
            max_batch_size=8,
            max_batch_wait_ms=20,
            inference_timeout_seconds=1,
        )
        await batcher.start()
        futures = [batcher.submit(farm(index), f"id-{index}") for index in range(5)]
        results = await asyncio.gather(*futures)
        await batcher.queue.join()
        unfinished = batcher.queue._unfinished_tasks
        await batcher.stop()
        return calls, results, futures, unfinished

    calls, results, futures, unfinished = asyncio.run(scenario())

    assert calls == [[f"QUEUED-{index:03}" for index in range(5)]]
    assert results == [float(index) for index in range(5)]
    assert all(future.done() and not future.cancelled() for future in futures)
    assert unfinished == 0


def test_worker_honors_max_batch_size_without_lost_tasks():
    async def scenario():
        batch_sizes = []

        async def predictor(items):
            batch_sizes.append(len(items))
            return [float(item.payment_delay_days) for item in items]

        batcher = DynamicBatcher(
            model_predictor=predictor,
            max_batch_size=8,
            max_batch_wait_ms=5,
            inference_timeout_seconds=1,
        )
        await batcher.start()
        futures = [batcher.submit(farm(index), f"id-{index}") for index in range(17)]
        results = await asyncio.gather(*futures)
        await batcher.queue.join()
        unfinished = batcher.queue._unfinished_tasks
        await batcher.stop()
        return batch_sizes, results, unfinished

    batch_sizes, results, unfinished = asyncio.run(scenario())

    assert batch_sizes == [8, 8, 1]
    assert results == [float(index) for index in range(17)]
    assert unfinished == 0


def test_batch_accumulation_wait_is_bounded():
    async def scenario():
        loop = asyncio.get_running_loop()
        called_at = None

        async def predictor(items):
            nonlocal called_at
            called_at = loop.time()
            return [0.1 for _ in items]

        batcher = DynamicBatcher(
            model_predictor=predictor,
            max_batch_wait_ms=25,
            inference_timeout_seconds=1,
        )
        await batcher.start()
        started = loop.time()
        result = await batcher.submit(farm(1), "one")
        elapsed = called_at - started
        await batcher.queue.join()
        await batcher.stop()
        return result, elapsed

    result, elapsed = asyncio.run(scenario())

    assert result == 0.1
    assert 0.015 <= elapsed < 0.25


def test_real_queue_capacity_raises_queue_full():
    async def scenario():
        batcher = DynamicBatcher(queue_max_size=1)
        first = batcher.submit(farm(1), "first")
        with pytest.raises(asyncio.QueueFull):
            batcher.submit(farm(2), "second")

        first.cancel()
        queued = batcher.queue.get_nowait()
        batcher.queue.task_done()
        await batcher.queue.join()
        await batcher.stop()
        return queued.future.cancelled(), batcher.queue._unfinished_tasks

    cancelled, unfinished = asyncio.run(scenario())
    assert cancelled
    assert unfinished == 0


def test_cancelled_future_does_not_block_following_request():
    async def scenario():
        async def predictor(items):
            return [float(item.payment_delay_days) for item in items]

        batcher = DynamicBatcher(
            model_predictor=predictor,
            max_batch_wait_ms=5,
            inference_timeout_seconds=1,
        )
        await batcher.start()
        cancelled = batcher.submit(farm(1), "cancelled")
        cancelled.cancel()
        following = batcher.submit(farm(2), "following")
        result = await following
        await batcher.queue.join()
        worker_alive = not batcher.worker_task.done()
        await batcher.stop()
        return cancelled.cancelled(), result, worker_alive

    was_cancelled, result, worker_alive = asyncio.run(scenario())
    assert was_cancelled
    assert result == 2.0
    assert worker_alive


def test_model_timeout_and_error_do_not_kill_worker():
    async def scenario():
        calls = 0

        async def predictor(items):
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.sleep(0.05)
            return [0.2 for _ in items]

        batcher = DynamicBatcher(
            model_predictor=predictor,
            max_batch_wait_ms=1,
            inference_timeout_seconds=0.01,
        )
        await batcher.start()
        timed_out = batcher.submit(farm(1), "timeout")
        with pytest.raises(InferenceExecutionTimeout):
            await timed_out

        following = batcher.submit(farm(2), "following")
        result = await following
        await batcher.queue.join()
        worker_alive = not batcher.worker_task.done()
        await batcher.stop()
        return result, worker_alive, timed_out.done(), following.done()

    result, worker_alive, timed_out_done, following_done = asyncio.run(scenario())
    assert result == 0.2
    assert worker_alive
    assert timed_out_done and following_done


def test_worker_continues_after_model_exception():
    async def scenario():
        calls = 0

        async def predictor(items):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("controlled model failure")
            return [0.3 for _ in items]

        batcher = DynamicBatcher(
            model_predictor=predictor,
            max_batch_wait_ms=1,
            inference_timeout_seconds=1,
        )
        await batcher.start()
        failed = batcher.submit(farm(1), "failed")
        with pytest.raises(RuntimeError, match="controlled model failure"):
            await failed

        following = batcher.submit(farm(2), "following")
        result = await following
        await batcher.queue.join()
        worker_alive = not batcher.worker_task.done()
        await batcher.stop()
        return result, worker_alive

    result, worker_alive = asyncio.run(scenario())
    assert result == 0.3
    assert worker_alive


def test_shutdown_cancels_active_future_and_balances_task_done():
    async def scenario():
        entered_model = asyncio.Event()

        async def predictor(items):
            entered_model.set()
            await asyncio.Event().wait()

        batcher = DynamicBatcher(
            model_predictor=predictor,
            max_batch_wait_ms=1,
            inference_timeout_seconds=10,
        )
        await batcher.start()
        future = batcher.submit(farm(1), "active")
        await asyncio.wait_for(entered_model.wait(), timeout=0.5)
        await batcher.stop()
        return future.cancelled(), batcher.queue._unfinished_tasks

    cancelled, unfinished = asyncio.run(scenario())
    assert cancelled
    assert unfinished == 0


def test_queued_endpoint_and_queue_status_persist_to_sqlite(
    queued_client,
    queued_db_path,
):
    response = queued_client.post(
        "/predict-queued",
        headers={"X-Request-ID": "queued-http-1"},
        json=farm_json(1),
    )
    status_response = queued_client.get("/queue-status")

    assert response.status_code == 200
    assert response.json()["request_id"] == "queued-http-1"
    assert status_response.status_code == 200
    assert status_response.json() == {
        "queue_size": 0,
        "queue_max_size": 100,
        "max_batch_size": 8,
        "max_batch_wait_ms": 50,
        "inference_timeout_seconds": 2.0,
    }

    with sqlite3.connect(queued_db_path) as connection:
        row = connection.execute(
            "SELECT request_id, farm_id, source FROM predictions"
        ).fetchone()
    assert row == ("queued-http-1", "QUEUED-001", "queued")


def test_concurrent_http_requests_are_dynamically_batched(queued_client):
    observed_batches = []
    batcher = queued_client.app.state.batcher
    original_predictor = batcher.model_predictor

    async def observed_predictor(items):
        observed_batches.append([item.farm_id for item in items])
        return await original_predictor(items)

    batcher.model_predictor = observed_predictor
    barrier = threading.Barrier(6)

    def send(number):
        barrier.wait()
        return queued_client.post(
            "/predict-queued",
            headers={"X-Request-ID": f"parallel-{number}"},
            json=farm_json(number),
        )

    with ThreadPoolExecutor(max_workers=5) as executor:
        responses = [executor.submit(send, number) for number in range(1, 6)]
        barrier.wait()
        responses = [future.result(timeout=3) for future in responses]

    assert all(response.status_code == 200 for response in responses)
    assert sum(len(batch) for batch in observed_batches) == 5
    assert any(len(batch) > 1 for batch in observed_batches)
    assert {response.json()["request_id"] for response in responses} == {
        f"parallel-{number}" for number in range(1, 6)
    }


def test_actual_full_queue_returns_429_and_retry_after(queued_client):
    full_batcher = DynamicBatcher(queue_max_size=1)
    full_batcher.queue.put_nowait(object())
    queued_client.app.state.batcher = full_batcher

    response = queued_client.post("/predict-queued", json=farm_json(1))

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    queued = full_batcher.queue.get_nowait()
    assert queued is not None
    full_batcher.queue.task_done()


def test_queue_wait_timeout_returns_504_and_cancels_future(
    queued_client,
    monkeypatch,
):
    stopped_batcher = DynamicBatcher(queue_max_size=1)
    queued_client.app.state.batcher = stopped_batcher
    monkeypatch.setattr(main, "INFERENCE_TIMEOUT_SECONDS", 0.01)

    response = queued_client.post("/predict-queued", json=farm_json(1))

    assert response.status_code == 504
    assert response.json()["detail"] == "Inference result timeout while waiting in queue"
    queued = stopped_batcher.queue.get_nowait()
    assert queued.future.cancelled()
    stopped_batcher.queue.task_done()


def test_model_execution_timeout_returns_504_and_worker_recovers(queued_client):
    batcher = queued_client.app.state.batcher
    original_predictor = batcher.model_predictor
    original_timeout = batcher.inference_timeout_seconds

    async def slow_predictor(items):
        await asyncio.sleep(0.05)
        return [0.1 for _ in items]

    batcher.model_predictor = slow_predictor
    batcher.inference_timeout_seconds = 0.01
    timed_out = queued_client.post("/predict-queued", json=farm_json(1))

    batcher.model_predictor = original_predictor
    batcher.inference_timeout_seconds = original_timeout
    following = queued_client.post("/predict-queued", json=farm_json(2))

    assert timed_out.status_code == 504
    assert timed_out.json()["detail"] == "Inference execution timeout"
    assert following.status_code == 200
    assert following.json()["farm_id"] == "QUEUED-002"


def test_single_explicit_and_dynamic_inference_share_one_model_lock(monkeypatch):
    async def scenario():
        active_calls = 0
        max_active_calls = 0
        calls = []

        async def delayed_scores(items):
            nonlocal active_calls, max_active_calls
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
            calls.append([item.farm_id for item in items])
            await asyncio.sleep(0.01)
            active_calls -= 1
            return [0.1 for _ in items]

        monkeypatch.setattr(inference, "_calculate_batch_scores", delayed_scores)
        batcher = DynamicBatcher(max_batch_wait_ms=1, inference_timeout_seconds=1)
        await batcher.start()

        single = asyncio.create_task(inference.model_predict_single(farm(1)))
        explicit = asyncio.create_task(inference.model_predict_batch([farm(2)]))
        dynamic = batcher.submit(farm(3), "dynamic-lock")

        results = await asyncio.gather(single, explicit, dynamic)
        await batcher.queue.join()
        await batcher.stop()
        return results, calls, max_active_calls

    results, calls, max_active_calls = asyncio.run(scenario())

    assert results == [0.1, [0.1], 0.1]
    assert len(calls) == 3
    assert max_active_calls == 1
