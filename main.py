# ПРАКТИЧЕСКОЕ ЗАНЯТИЕ № 1
# Разработка REST API для AI-сервиса на FastAPI

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError

from batching import DynamicBatcher, InferenceExecutionTimeout
from config import INFERENCE_TIMEOUT_SECONDS, LLM_TIMEOUT_SECONDS
from errors import (
    http_exception_handler,
    internal_server_error_handler,
    validation_exception_handler,
)
from database import init_db
from inference import model_predict_batch, model_predict_single
from llm_client import build_scoring_explanation_prompt, call_vireonix_llm
from schemas import (
    BatchPredictionResponse,
    BatchRequest,
    FarmRequest,
    HealthResponse,
    LLMChatRequest,
    LLMChatResponse,
    ModelInfoResponse,
    PaginatedPredictionsResponse,
    PredictionResponse,
    PredictWithLLMResponse,
)
from services import (
    get_recommendation,
    get_risk_level,
    validate_business_rules,
    WEEKDAY_MAP,
)
from repository import (
    get_predictions as get_predictions_from_db,
    get_predictions_by_request_id,
    get_predictions_by_weekday_page,
    save_prediction,
    save_predictions_batch,
    seed_weekday_predictions,
)


# ------------------------------------------------------------
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# FASTAPI-ПРИЛОЖЕНИЕ
# ------------------------------------------------------------

@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Initialize storage and own the dynamic batch worker lifecycle."""

    await init_db()
    batcher = DynamicBatcher()
    await batcher.start()
    app_instance.state.batcher = batcher
    app_instance.state.llm_client = httpx.AsyncClient(
        timeout=httpx.Timeout(LLM_TIMEOUT_SECONDS, connect=10.0),
    )

    try:
        yield
    finally:
        await batcher.stop()
        await app_instance.state.llm_client.aclose()


app = FastAPI(
    title="Agro Scoring API",
    description=(
        "REST API для оценки риска "
        "сельскохозяйственных предприятий."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ------------------------------------------------------------
# КОНСТАНТЫ МОДЕЛИ
# ------------------------------------------------------------

MODEL_NAME = "agro-risk-model"
MODEL_VERSION = "1.0"
MODEL_TYPE = "risk-scoring"

# Имитируем состояние модели.
# Если установить False, /predict будет возвращать HTTP 503.
MODEL_READY = True


# ------------------------------------------------------------
# MIDDLEWARE
# Измеряем время обработки каждого HTTP-запроса.
# ------------------------------------------------------------

@app.middleware("http")
async def add_process_time(request: Request, call_next):
    """
    Добавляет в HTTP-ответ заголовок X-Process-Time
    со временем обработки запроса.
    """

    start_time = time.perf_counter()
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id

    logger.info(
        "HTTP request started | request_id=%s | method=%s | path=%s",
        request_id,
        request.method,
        request.url.path,
    )

    try:
        response = await call_next(request)
    except Exception as exc:
        response = await internal_server_error_handler(request, exc)

    process_time = time.perf_counter() - start_time

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time"] = str(round(process_time, 6))

    logger.info(
        "HTTP request completed | request_id=%s | method=%s | path=%s | "
        "status=%s | process_time=%.6f",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        process_time,
    )

    return response


app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, internal_server_error_handler)


# ------------------------------------------------------------
# GET /health
# ------------------------------------------------------------

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Проверка состояния API",
    description=(
        "Используется для проверки того, "
        "что REST API запущен и отвечает."
    )
)
def health():
    """Проверка работоспособности API."""

    return {
        "status": "ok"
    }


# ------------------------------------------------------------
# GET /model-info
# ------------------------------------------------------------

@app.get(
    "/model-info",
    response_model=ModelInfoResponse,
    summary="Информация о модели",
    description=(
        "Возвращает название, версию, тип "
        "и текущее состояние модели."
    )
)
def model_info():
    """Возвращает информацию об используемой ML-модели."""

    model_status = (
        "ready"
        if MODEL_READY
        else "unavailable"
    )

    return {
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "model_type": MODEL_TYPE,
        "status": model_status
    }


@app.post(
    "/llm/chat",
    response_model=LLMChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Send a prompt to the external Vireonix LLM",
)
async def llm_chat(data: LLMChatRequest, request: Request):
    return await call_vireonix_llm(
        client=request.app.state.llm_client,
        prompt=data.prompt,
        request_id=request.state.request_id,
        model=data.model,
        temperature=data.temperature,
        max_tokens=data.max_tokens,
    )


@app.post(
    "/predict-with-llm",
    response_model=PredictWithLLMResponse,
    status_code=status.HTTP_200_OK,
    summary="Score a farm and request an LLM explanation",
)
async def predict_with_llm(data: FarmRequest, request: Request):
    """Persist deterministic scoring before requesting an external explanation."""

    if not MODEL_READY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model is temporarily unavailable",
        )

    validate_business_rules(data)
    request_id = request.state.request_id

    try:
        score = await asyncio.wait_for(
            model_predict_single(data),
            timeout=INFERENCE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Inference execution timeout",
        )

    risk_level = get_risk_level(score)
    prediction = {
        "request_id": request_id,
        "farm_id": data.farm_id,
        "risk_score": score,
        "risk_level": risk_level,
        "recommendation": get_recommendation(risk_level),
        "model_version": MODEL_VERSION,
    }

    # The deterministic result remains auditable even if the provider fails.
    await save_prediction(prediction, source="single+llm")

    llm_result = await call_vireonix_llm(
        client=request.app.state.llm_client,
        prompt=build_scoring_explanation_prompt(prediction),
        request_id=request_id,
    )

    return {"prediction": prediction, "llm": llm_result}


# ------------------------------------------------------------
# POST /predict
# ------------------------------------------------------------

@app.post(
    "/predict",
    response_model=PredictionResponse,
    status_code=status.HTTP_200_OK,
    summary="Оценить риск хозяйства",
    description=(
        "Принимает характеристики хозяйства, "
        "выполняет валидацию, инференс модели "
        "и возвращает оценку риска."
    )
)
async def predict(request: FarmRequest, http_request: Request):
    """Основной endpoint сервиса."""

    # 1. Проверяем состояние ML-модели
    if not MODEL_READY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model is temporarily unavailable"
        )

    # 2. Бизнес-валидация региона
    validate_business_rules(request)

    # 3. Логирование входящего запроса
    logger.info(
        "Prediction request received | request_id=%s | farm_id=%s",
        http_request.state.request_id,
        request.farm_id
    )

    # 4. Инференс
    try:
        score = await asyncio.wait_for(
            model_predict_single(request),
            timeout=INFERENCE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Inference execution timeout",
        )

    # 5. Постпроцессинг
    level = get_risk_level(score)
    recommendation = get_recommendation(level)

    # 6. Создаем уникальный ID запроса
    request_id = http_request.state.request_id

    # 7. Формируем результат
    result = {
        "request_id": request_id,
        "farm_id": request.farm_id,
        "risk_score": score,
        "risk_level": level,
        "recommendation": recommendation,
        "model_version": MODEL_VERSION
    }

    # 8. Сохраняем результат
    await save_prediction(result, source="single")

    # 9. Логирование результата
    logger.info(
        "Prediction completed | "
        "request_id=%s | "
        "farm_id=%s | "
        "risk_score=%s | "
        "risk_level=%s",
        request_id,
        request.farm_id,
        score,
        level
    )

    # 10. Возвращаем HTTP-ответ
    return result


@app.post(
    "/batch-predict",
    response_model=BatchPredictionResponse,
    status_code=status.HTTP_200_OK,
    summary="Run explicit batch inference",
)
async def batch_predict(batch: BatchRequest, http_request: Request):
    """Score an explicit client batch with one reusable batch inference call."""

    if not MODEL_READY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model is temporarily unavailable",
        )

    for item in batch.items:
        validate_business_rules(item)

    request_id = http_request.state.request_id
    started = time.perf_counter()
    try:
        scores = await asyncio.wait_for(
            model_predict_batch(batch.items),
            timeout=INFERENCE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Batch inference execution timeout",
        )

    response_items = []
    for item, score in zip(batch.items, scores):
        risk_level = get_risk_level(score)
        response_items.append(
            {
                "farm_id": item.farm_id,
                "risk_score": score,
                "risk_level": risk_level,
                "recommendation": get_recommendation(risk_level),
                "model_version": MODEL_VERSION,
            }
        )

    await save_predictions_batch(request_id, response_items)

    processing_time_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "Batch prediction completed | request_id=%s | batch_size=%s | "
        "processing_time_ms=%.2f",
        request_id,
        len(response_items),
        processing_time_ms,
    )

    return {
        "request_id": request_id,
        "batch_size": len(response_items),
        "processing_time_ms": round(processing_time_ms, 2),
        "items": response_items,
    }


@app.post(
    "/predict-queued",
    response_model=PredictionResponse,
    status_code=status.HTTP_200_OK,
    summary="Run inference through the dynamic batching queue",
)
async def predict_queued(data: FarmRequest, request: Request):
    """Queue one request and await only its own Future result."""

    if not MODEL_READY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model is temporarily unavailable",
        )

    validate_business_rules(data)
    request_id = request.state.request_id
    batcher: DynamicBatcher = request.app.state.batcher

    try:
        future = batcher.submit(data, request_id)
    except asyncio.QueueFull:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Inference queue is full. Try again later.",
            headers={"Retry-After": "1"},
        )

    logger.info(
        "Inference request queued | request_id=%s | queue_size=%s",
        request_id,
        batcher.queue.qsize(),
    )

    try:
        score = await asyncio.wait_for(
            asyncio.shield(future),
            timeout=INFERENCE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        if not future.done():
            future.cancel()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Inference result timeout while waiting in queue",
        )
    except InferenceExecutionTimeout:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Inference execution timeout",
        )
    except asyncio.CancelledError:
        if not future.done():
            future.cancel()
        raise

    risk_level = get_risk_level(score)
    result = {
        "request_id": request_id,
        "farm_id": data.farm_id,
        "risk_score": score,
        "risk_level": risk_level,
        "recommendation": get_recommendation(risk_level),
        "model_version": MODEL_VERSION,
    }
    await save_prediction(result, source="queued")
    return result


@app.get(
    "/queue-status",
    summary="Get dynamic batching queue status",
)
async def queue_status(request: Request):
    batcher: DynamicBatcher = request.app.state.batcher
    return {
        "queue_size": batcher.queue.qsize(),
        "queue_max_size": batcher.queue_max_size,
        "max_batch_size": batcher.max_batch_size,
        "max_batch_wait_ms": batcher.max_batch_wait_ms,
        "inference_timeout_seconds": batcher.inference_timeout_seconds,
    }


# ------------------------------------------------------------
# GET /predictions
# Этот endpoint расположен ДО /predictions/{request_id}.
# ------------------------------------------------------------

@app.get(
    "/predictions",
    response_model=List[PredictionResponse],
    summary="Получить список прогнозов",
    description=(
        "Возвращает список выполненных прогнозов. "
        "Поддерживает ограничение количества результатов "
        "и фильтрацию по уровню риска."
    )
)
async def get_predictions(
    limit: int = Query(
        default=10,
        ge=1,
        le=100,
        description="Максимальное количество результатов"
    ),
    risk_level: Optional[str] = Query(
        default=None,
        description=(
            "Фильтр по категории риска: "
            "low, medium или high"
        )
    )
):
    """Получение списка прогнозов."""

    allowed_levels = {
        "low",
        "medium",
        "high"
    }

    if risk_level is not None:
        if risk_level not in allowed_levels:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "risk_level must be "
                    "'low', 'medium' or 'high'"
                )
            )

    return await get_predictions_from_db(
        limit=limit,
        risk_level=risk_level,
    )


@app.get(
    "/predictions/by-weekday",
    response_model=PaginatedPredictionsResponse,
    summary="Get predictions for a weekday with pagination",
)
async def get_predictions_by_weekday(
    request: Request,
    weekday: str = Query(..., description="monday through sunday"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=5, ge=1),
):
    """Return a LIMIT/OFFSET page of predictions for one weekday."""

    weekday = weekday.lower()

    if weekday not in WEEKDAY_MAP:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "weekday must be one of: monday, tuesday, wednesday, "
                "thursday, friday, saturday, sunday"
            ),
        )

    data, total_items, total_pages = await get_predictions_by_weekday_page(
        weekday_number=WEEKDAY_MAP[weekday],
        page=page,
        per_page=per_page,
    )

    if total_pages > 0 and page > total_pages:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Page {page} does not exist. Total pages: {total_pages}",
        )

    if total_pages == 0 and page > 1:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No data for this weekday",
        )

    next_page = None
    if page < total_pages:
        next_page = str(
            request.url.include_query_params(
                weekday=weekday,
                page=page + 1,
                per_page=per_page,
            )
        )

    prev_page = None
    if page > 1 and total_pages > 0:
        prev_page = str(
            request.url.include_query_params(
                weekday=weekday,
                page=page - 1,
                per_page=per_page,
            )
        )

    return {
        "data": data,
        "meta": {
            "weekday": weekday,
            "current_page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "total_items": total_items,
            "next_page": next_page,
            "prev_page": prev_page,
        },
    }


@app.post(
    "/demo/seed-weekdays",
    summary="Explicitly create demo predictions across weekdays",
)
async def seed_demo_weekdays(
    count: int = Query(default=14, ge=7, le=1_000),
):
    """Create demo data only when this endpoint is explicitly called."""

    created = await seed_weekday_predictions(count)
    return {
        "status": "ok",
        "created": created,
        "message": "Demo predictions created for weekday pagination checks.",
    }


@app.get(
    "/requests/{request_id}/predictions",
    response_model=List[PredictionResponse],
    summary="Get all predictions created by one HTTP request",
)
async def get_request_predictions(request_id: str):
    """Return every prediction related to one request, including future batches."""

    results = await get_predictions_by_request_id(request_id)

    if not results:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prediction not found",
        )

    return results


# ------------------------------------------------------------
# GET /predictions/{request_id}
# ------------------------------------------------------------

@app.get(
    "/predictions/{request_id}",
    response_model=PredictionResponse,
    summary="Получить прогноз по request_id",
    description=(
        "Возвращает сохраненный прогноз "
        "по его уникальному идентификатору."
    )
)
async def get_prediction(request_id: str):
    """Получение одного прогноза по request_id."""

    results = await get_predictions_by_request_id(request_id)

    if not results:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prediction not found"
        )

    if len(results) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This request_id contains multiple predictions. "
                f"Use /requests/{request_id}/predictions"
            ),
        )

    return results[0]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
