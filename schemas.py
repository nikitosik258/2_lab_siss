from typing import List, Optional

from pydantic import BaseModel, Field

from config import (
    LLM_DEFAULT_MAX_TOKENS,
    LLM_DEFAULT_TEMPERATURE,
    LLM_MODEL,
    MAX_BATCH_SIZE,
)


class FarmRequest(BaseModel):
    """
    Данные сельскохозяйственного предприятия,
    которые клиент передает в POST /predict.
    """

    farm_id: str = Field(
        ...,
        min_length=1,
        description="Идентификатор хозяйства"
    )

    region: str = Field(
        ...,
        min_length=1,
        description="Регион хозяйства"
    )

    crop_type: str = Field(
        ...,
        min_length=1,
        description="Основная сельскохозяйственная культура"
    )

    area_ha: float = Field(
        ...,
        gt=0,
        description="Площадь посевов, га"
    )

    temperature_avg: float = Field(
        ...,
        ge=-60,
        le=60,
        description="Средняя температура, °C"
    )

    precipitation_mm: float = Field(
        ...,
        ge=0,
        description="Количество осадков, мм"
    )

    payment_delay_days: int = Field(
        ...,
        ge=0,
        description="Количество дней просрочки платежа"
    )

    previous_defaults: int = Field(
        ...,
        ge=0,
        description="Количество предыдущих дефолтов"
    )

    debt: float = Field(
        ...,
        ge=0,
        description="Текущая задолженность"
    )


class PredictionResponse(BaseModel):
    """
    Структура ответа сервиса после выполнения прогноза.
    """

    request_id: str
    farm_id: str
    risk_score: float
    risk_level: str
    recommendation: str
    model_version: str


class HealthResponse(BaseModel):
    status: str


class ModelInfoResponse(BaseModel):
    model_name: str
    model_version: str
    model_type: str
    status: str


class PaginationMeta(BaseModel):
    weekday: str
    current_page: int
    per_page: int
    total_pages: int
    total_items: int
    next_page: Optional[str]
    prev_page: Optional[str]


class PaginatedPredictionsResponse(BaseModel):
    data: List[PredictionResponse]
    meta: PaginationMeta


class BatchRequest(BaseModel):
    items: List[FarmRequest] = Field(
        ...,
        min_length=1,
        max_length=MAX_BATCH_SIZE,
        description="Farm requests for one explicit batch inference",
    )


class BatchItemResponse(BaseModel):
    farm_id: str
    risk_score: float
    risk_level: str
    recommendation: str
    model_version: str


class BatchPredictionResponse(BaseModel):
    request_id: str
    batch_size: int
    processing_time_ms: float
    items: List[BatchItemResponse]


class LLMChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=5_000)
    model: str = Field(default=LLM_MODEL, min_length=1)
    temperature: float = Field(
        default=LLM_DEFAULT_TEMPERATURE,
        ge=0.0,
        le=2.0,
    )
    max_tokens: int = Field(
        default=LLM_DEFAULT_MAX_TOKENS,
        ge=1,
        le=2_000,
    )


class LLMChatResponse(BaseModel):
    request_id: str
    requested_model: str
    returned_model: Optional[str]
    answer: str
    finish_reason: Optional[str]
    latency_ms: float
    usage: Optional[dict] = None


class PredictWithLLMResponse(BaseModel):
    prediction: PredictionResponse
    llm: LLMChatResponse
