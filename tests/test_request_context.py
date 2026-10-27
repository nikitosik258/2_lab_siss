import logging
import sqlite3
import uuid
from contextlib import asynccontextmanager

import aiosqlite
import pytest
from fastapi.testclient import TestClient

import database
import main
from main import app


VALID_FARM = {
    "farm_id": "FARM-001",
    "region": "Krasnodar",
    "crop_type": "wheat",
    "area_ha": 100,
    "temperature_avg": 20,
    "precipitation_mm": 80,
    "payment_delay_days": 31,
    "previous_defaults": 1,
    "debt": 6_000_000,
}


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test_agro_scoring.db"
    monkeypatch.setattr(database, "DATABASE_PATH", path)
    return path


@pytest.fixture
def client(db_path):
    with TestClient(app) as test_client:
        yield test_client


def assert_trace_headers(response):
    assert response.headers["X-Request-ID"]
    assert float(response.headers["X-Process-Time"]) >= 0


def seed_prediction(db_path, number: int, created_at: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO predictions (
                request_id, farm_id, risk_score, risk_level,
                recommendation, model_version, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"seed-{number}",
                f"FARM-{number:03}",
                0.1,
                "low",
                "ok",
                "1.0",
                "test-seed",
                created_at,
            ),
        )
        connection.commit()


def make_batch(size: int) -> dict:
    return {
        "items": [
            {
                **VALID_FARM,
                "farm_id": f"BATCH-{index:03}",
                "debt": index * 1_000_000,
            }
            for index in range(1, size + 1)
        ]
    }


def test_health_generates_uuid_request_id(client, caplog):
    caplog.set_level(logging.INFO)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert uuid.UUID(response.headers["X-Request-ID"]).version == 4
    assert_trace_headers(response)
    assert "HTTP request started" in caplog.text
    assert "HTTP request completed" in caplog.text


def test_health_preserves_client_request_id(client):
    request_id = "client-correlation-id-42"
    response = client.get("/health", headers={"X-Request-ID": request_id})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == request_id
    assert_trace_headers(response)


def test_predict_persists_request_id_and_result_in_sqlite(client, db_path):
    request_id = "predict-trace-001"
    response = client.post(
        "/predict",
        headers={"X-Request-ID": request_id},
        json=VALID_FARM,
    )

    assert response.status_code == 200
    assert response.json()["request_id"] == request_id
    assert response.headers["X-Request-ID"] == request_id

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT request_id, farm_id, risk_score, risk_level, source, created_at
            FROM predictions WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()

    assert row[:5] == (request_id, "FARM-001", 1.0, "high", "single")
    assert row[5]


def test_prediction_history_and_lookup_use_sqlite(client):
    created = client.post("/predict", json=VALID_FARM).json()
    lookup = client.get(f"/predictions/{created['request_id']}")
    history = client.get("/predictions?risk_level=high")
    all_for_request = client.get(
        f"/requests/{created['request_id']}/predictions"
    )

    assert lookup.status_code == 200
    assert lookup.json()["request_id"] == created["request_id"]
    assert history.status_code == 200
    assert [item["request_id"] for item in history.json()] == [
        created["request_id"]
    ]
    assert all_for_request.status_code == 200
    assert all_for_request.json() == [lookup.json()]


def test_data_survives_a_new_application_lifespan(db_path):
    with TestClient(app) as first_client:
        created = first_client.post("/predict", json=VALID_FARM).json()

    with TestClient(app) as recreated_client:
        response = recreated_client.get(
            f"/predictions/{created['request_id']}"
        )

    assert response.status_code == 200
    assert response.json()["farm_id"] == "FARM-001"


def test_validation_error_keeps_details_and_trace_headers(client):
    payload = {**VALID_FARM, "area_ha": -100}
    response = client.post("/predict", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert isinstance(body["detail"], list)
    assert body["detail"][0]["loc"][-1] == "area_ha"
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert_trace_headers(response)


def test_business_rule_error_has_common_body_and_trace_headers(client):
    request_id = "invalid-region-001"
    payload = {**VALID_FARM, "region": "Unknown"}
    response = client.post(
        "/predict",
        headers={"X-Request-ID": request_id},
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Unknown region")
    assert response.json()["request_id"] == request_id
    assert response.headers["X-Request-ID"] == request_id
    assert_trace_headers(response)


def test_missing_prediction_has_common_body_and_trace_headers(client):
    response = client.get("/predictions/not-found")

    assert response.status_code == 404
    assert response.json()["detail"] == "Prediction not found"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert_trace_headers(response)


def test_database_unavailability_returns_503(client, monkeypatch):
    @asynccontextmanager
    async def unavailable_database():
        raise aiosqlite.OperationalError("database unavailable")
        yield

    monkeypatch.setattr(database, "get_db", unavailable_database)
    response = client.get("/predictions")

    assert response.status_code == 503
    assert response.json()["detail"] == "Prediction storage is unavailable"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_multiple_rows_for_one_request_id_return_409(client, db_path):
    request_id = "future-batch-request"
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            """
            INSERT INTO predictions (
                request_id, farm_id, risk_score, risk_level,
                recommendation, model_version, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (request_id, "FARM-001", 0.1, "low", "ok", "1.0", "test"),
                (request_id, "FARM-002", 0.4, "medium", "check", "1.0", "test"),
            ],
        )
        connection.commit()

    response = client.get(f"/predictions/{request_id}")
    collection = client.get(f"/requests/{request_id}/predictions")

    assert response.status_code == 409
    assert collection.status_code == 200
    assert len(collection.json()) == 2


def test_weekday_pagination_returns_first_and_last_pages(client, db_path):
    for number in range(1, 7):
        seed_prediction(db_path, number, f"2024-01-01 12:00:0{number}")

    first_page = client.get(
        "/predictions/by-weekday?weekday=monday&page=1&per_page=5"
    )
    last_page = client.get(
        "/predictions/by-weekday?weekday=monday&page=2&per_page=5"
    )

    assert first_page.status_code == 200
    assert len(first_page.json()["data"]) == 5
    assert first_page.json()["meta"] == {
        "weekday": "monday",
        "current_page": 1,
        "per_page": 5,
        "total_pages": 2,
        "total_items": 6,
        "next_page": "http://testserver/predictions/by-weekday?weekday=monday&page=2&per_page=5",
        "prev_page": None,
    }
    assert last_page.status_code == 200
    assert len(last_page.json()["data"]) == 1
    assert last_page.json()["meta"]["next_page"] is None
    assert last_page.json()["meta"]["prev_page"].endswith(
        "weekday=monday&page=1&per_page=5"
    )


def test_weekday_pagination_filters_different_days_and_empty_results(client, db_path):
    seed_prediction(db_path, 1, "2024-01-01 12:00:00")  # Monday
    seed_prediction(db_path, 2, "2024-01-02 12:00:00")  # Tuesday

    tuesday = client.get("/predictions/by-weekday?weekday=tuesday")
    sunday = client.get("/predictions/by-weekday?weekday=sunday")

    assert tuesday.status_code == 200
    assert [item["farm_id"] for item in tuesday.json()["data"]] == [
        "FARM-002"
    ]
    assert sunday.status_code == 200
    assert sunday.json()["data"] == []
    assert sunday.json()["meta"]["total_items"] == 0
    assert sunday.json()["meta"]["total_pages"] == 0
    assert sunday.json()["meta"]["next_page"] is None
    assert sunday.json()["meta"]["prev_page"] is None


def test_weekday_pagination_rejects_invalid_values_and_out_of_range_page(
    client,
    db_path,
):
    seed_prediction(db_path, 1, "2024-01-01 12:00:00")

    unknown_weekday = client.get("/predictions/by-weekday?weekday=holiday")
    invalid_page = client.get("/predictions/by-weekday?weekday=monday&page=-1")
    missing_page = client.get("/predictions/by-weekday?weekday=monday&page=2")
    empty_page = client.get("/predictions/by-weekday?weekday=sunday&page=2")

    assert unknown_weekday.status_code == 400
    assert invalid_page.status_code == 422
    assert missing_page.status_code == 404
    assert empty_page.status_code == 404


def test_demo_seed_requires_explicit_post_and_creates_requested_rows(client, db_path):
    response = client.post("/demo/seed-weekdays?count=7")

    assert response.status_code == 200
    assert response.json()["created"] == 7
    with sqlite3.connect(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM predictions WHERE source = 'demo-seed'"
        ).fetchone()[0]
    assert count == 7


@pytest.mark.parametrize("batch_size", [1, 3, 8])
def test_explicit_batch_persists_all_items_in_order(client, db_path, batch_size):
    request_id = f"batch-{batch_size}"
    payload = make_batch(batch_size)
    response = client.post(
        "/batch-predict",
        headers={"X-Request-ID": request_id},
        json=payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == request_id
    assert body["batch_size"] == batch_size
    assert body["processing_time_ms"] >= 0
    assert [item["farm_id"] for item in body["items"]] == [
        item["farm_id"] for item in payload["items"]
    ]
    assert_trace_headers(response)

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT request_id, farm_id, source FROM predictions
            WHERE request_id = ? ORDER BY id
            """,
            (request_id,),
        ).fetchall()

    assert len(rows) == batch_size
    assert all(row[0] == request_id and row[2] == "batch" for row in rows)
    assert [row[1] for row in rows] == [
        item["farm_id"] for item in payload["items"]
    ]


@pytest.mark.parametrize("payload", [{"items": []}, make_batch(9)])
def test_explicit_batch_rejects_empty_or_oversized_payload(client, payload):
    response = client.post("/batch-predict", json=payload)

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_explicit_batch_rejects_invalid_item(client):
    payload = make_batch(3)
    payload["items"][1]["area_ha"] = -1

    response = client.post("/batch-predict", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][1:3] == ["items", 1]


def test_explicit_batch_applies_business_validation_to_each_item(client):
    payload = make_batch(3)
    payload["items"][2]["region"] = "Unknown"

    response = client.post("/batch-predict", json=payload)

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Unknown region")


def test_explicit_batch_uses_one_model_call_and_batch_result_routes(
    client,
    monkeypatch,
):
    calls = []
    original_model_predict_batch = main.model_predict_batch

    async def spy_model_predict_batch(items):
        calls.append([item.farm_id for item in items])
        return await original_model_predict_batch(items)

    monkeypatch.setattr(main, "model_predict_batch", spy_model_predict_batch)
    payload = make_batch(3)
    response = client.post("/batch-predict", json=payload)
    request_id = response.json()["request_id"]

    single_result = client.get(f"/predictions/{request_id}")
    all_results = client.get(f"/requests/{request_id}/predictions")

    assert response.status_code == 200
    assert calls == [[item["farm_id"] for item in payload["items"]]]
    assert single_result.status_code == 409
    assert all_results.status_code == 200
    assert [item["farm_id"] for item in all_results.json()] == [
        item["farm_id"] for item in payload["items"]
    ]


def test_batch_storage_failure_rolls_back_all_rows(client, db_path):
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_batch_write
            BEFORE INSERT ON predictions
            WHEN NEW.farm_id = 'FAIL-STORE'
            BEGIN
                SELECT RAISE(ABORT, 'forced batch storage failure');
            END;
            """
        )
        connection.commit()

    payload = make_batch(3)
    payload["items"][1]["farm_id"] = "FAIL-STORE"
    response = client.post("/batch-predict", json=payload)

    assert response.status_code == 503
    assert response.json()["detail"] == "Prediction storage is unavailable"
    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    assert count == 0
