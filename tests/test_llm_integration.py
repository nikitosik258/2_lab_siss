import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

import database
from main import app


VALID_FARM = {
    "farm_id": "PRIVATE-FARM-001",
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
def llm_db_path(tmp_path, monkeypatch):
    path = tmp_path / "llm_test.db"
    monkeypatch.setattr(database, "DATABASE_PATH", path)
    return path


@pytest.fixture
def llm_app_client(llm_db_path):
    with TestClient(app) as client:
        yield client


def install_mock_llm(client: TestClient, handler) -> None:
    original_client = client.app.state.llm_client
    asyncio.run(original_client.aclose())
    client.app.state.llm_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )


def successful_response(answer: str = "Краткое объяснение") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "vireonix-test",
            "choices": [
                {
                    "message": {"content": answer},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def test_llm_chat_uses_openai_payload_without_authorization(llm_app_client):
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["url"] = str(request.url)
        received["headers"] = dict(request.headers)
        received["body"] = json.loads(request.content)
        return successful_response()

    install_mock_llm(llm_app_client, handler)
    response = llm_app_client.post(
        "/llm/chat",
        headers={"X-Request-ID": "llm-chat-1"},
        json={
            "prompt": "Привет",
            "model": "auto",
            "temperature": 0.3,
            "max_tokens": 300,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "request_id": "llm-chat-1",
        "requested_model": "auto",
        "returned_model": "vireonix-test",
        "answer": "Краткое объяснение",
        "finish_reason": "stop",
        "latency_ms": response.json()["latency_ms"],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    assert received["url"] == "https://vireonix.ai/v1/chat/completions"
    assert "authorization" not in received["headers"]
    assert received["body"]["model"] == "auto"
    assert received["body"]["temperature"] == 0.3
    assert received["body"]["max_tokens"] == 300
    assert received["body"]["messages"][1] == {
        "role": "user",
        "content": "Привет",
    }


@pytest.mark.parametrize(
    ("handler", "status_code", "detail"),
    [
        (lambda _: httpx.Response(500, text="provider error"), 502, "HTTP 500"),
        (lambda _: httpx.Response(200, content=b"not-json"), 502, "invalid JSON"),
        (
            lambda _: httpx.Response(
                200,
                json={"choices": [{"message": {}}]},
            ),
            502,
            "message content",
        ),
        (lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=request)), 504, "timeout"),
        (lambda request: (_ for _ in ()).throw(httpx.ConnectError("down", request=request)), 503, "unavailable"),
    ],
)
def test_llm_chat_maps_provider_failures_to_expected_http_errors(
    llm_app_client,
    handler,
    status_code,
    detail,
):
    install_mock_llm(llm_app_client, handler)
    response = llm_app_client.post(
        "/llm/chat",
        headers={"X-Request-ID": "llm-error-1"},
        json={"prompt": "Проверка"},
    )

    assert response.status_code == status_code
    assert detail.lower() in response.json()["detail"].lower()
    assert response.json()["request_id"] == "llm-error-1"
    assert response.headers["X-Request-ID"] == "llm-error-1"


def test_llm_chat_validates_input(llm_app_client):
    response = llm_app_client.post(
        "/llm/chat",
        json={"prompt": "", "temperature": 3, "max_tokens": 0},
    )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_predict_with_llm_keeps_scoring_unchanged_and_hides_farm_data(
    llm_app_client,
    llm_db_path,
):
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["body"] = json.loads(request.content)
        return successful_response("Объяснение готово")

    install_mock_llm(llm_app_client, handler)
    response = llm_app_client.post(
        "/predict-with-llm",
        headers={"X-Request-ID": "score-llm-1"},
        json=VALID_FARM,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["prediction"]["request_id"] == "score-llm-1"
    assert body["prediction"]["risk_score"] == 1.0
    assert body["prediction"]["risk_level"] == "high"
    assert body["llm"]["answer"] == "Объяснение готово"

    prompt = received["body"]["messages"][1]["content"]
    assert VALID_FARM["farm_id"] not in prompt
    assert str(VALID_FARM["debt"]) not in prompt
    assert "risk_score: 1.0" in prompt
    assert "risk_level: high" in prompt

    with sqlite3.connect(llm_db_path) as connection:
        row = connection.execute(
            "SELECT request_id, risk_score, risk_level, source FROM predictions"
        ).fetchone()
    assert row == ("score-llm-1", 1.0, "high", "single+llm")


def test_predict_with_llm_preserves_saved_prediction_when_provider_fails(
    llm_app_client,
    llm_db_path,
):
    install_mock_llm(
        llm_app_client,
        lambda _: httpx.Response(500, text="provider error"),
    )
    response = llm_app_client.post(
        "/predict-with-llm",
        headers={"X-Request-ID": "saved-before-llm-error"},
        json=VALID_FARM,
    )

    assert response.status_code == 502
    assert response.json()["request_id"] == "saved-before-llm-error"
    lookup = llm_app_client.get("/predictions/saved-before-llm-error")
    assert lookup.status_code == 200
    assert lookup.json()["risk_score"] == 1.0
    assert lookup.json()["risk_level"] == "high"

    with sqlite3.connect(llm_db_path) as connection:
        row = connection.execute(
            "SELECT source FROM predictions WHERE request_id = ?",
            ("saved-before-llm-error",),
        ).fetchone()
    assert row == ("single+llm",)
