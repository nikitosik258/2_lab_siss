"""OpenAI-compatible Vireonix integration without request-data disclosure."""

import logging
import time
from typing import Any

import httpx
from fastapi import HTTPException, status

from config import LLM_BASE_URL, LLM_DEFAULT_MAX_TOKENS, LLM_DEFAULT_TEMPERATURE, LLM_MODEL


logger = logging.getLogger(__name__)


def _provider_error(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


async def call_vireonix_llm(
    client: httpx.AsyncClient,
    *,
    prompt: str,
    request_id: str,
    model: str = LLM_MODEL,
    temperature: float = LLM_DEFAULT_TEMPERATURE,
    max_tokens: int = LLM_DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Call Vireonix and normalize a valid OpenAI-compatible response."""

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You explain an already calculated agricultural risk result. "
                    "Do not change risk_score or risk_level and do not invent "
                    "facts that are absent from the supplied scoring result."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    url = f"{LLM_BASE_URL}/chat/completions"
    started = time.perf_counter()

    logger.info(
        "LLM request started | request_id=%s | model=%s | prompt_length=%s",
        request_id,
        model,
        len(prompt),
    )

    try:
        response = await client.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
        )
    except httpx.TimeoutException as exc:
        logger.warning("LLM timeout | request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="LLM request timeout",
        ) from exc
    except httpx.RequestError as exc:
        logger.warning("LLM connection error | request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM service is unavailable",
        ) from exc

    latency_ms = round((time.perf_counter() - started) * 1000, 2)

    if response.status_code != status.HTTP_200_OK:
        logger.warning(
            "LLM provider error | request_id=%s | status=%s",
            request_id,
            response.status_code,
        )
        raise _provider_error(
            f"LLM provider returned HTTP {response.status_code}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise _provider_error("LLM provider returned invalid JSON") from exc

    if not isinstance(data, dict):
        raise _provider_error("LLM provider returned an invalid response body")

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise _provider_error("LLM response does not contain choices")

    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise _provider_error("LLM response does not contain a message")

    answer = message.get("content")
    if not isinstance(answer, str) or not answer.strip():
        raise _provider_error("LLM response does not contain message content")

    returned_model = data.get("model")
    finish_reason = choice.get("finish_reason")
    usage = data.get("usage")
    if returned_model is not None and not isinstance(returned_model, str):
        raise _provider_error("LLM response contains an invalid model field")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise _provider_error("LLM response contains an invalid finish_reason")
    if usage is not None and not isinstance(usage, dict):
        raise _provider_error("LLM response contains invalid usage")

    return {
        "request_id": request_id,
        "requested_model": model,
        "returned_model": returned_model,
        "answer": answer,
        "finish_reason": finish_reason,
        "latency_ms": latency_ms,
        "usage": usage,
    }


def build_scoring_explanation_prompt(prediction: dict[str, Any]) -> str:
    """Build an explanation prompt from non-confidential deterministic output."""

    return (
        "Give a concise Russian explanation of this completed agricultural risk "
        "assessment. Do not recalculate or change its values.\n"
        f"risk_score: {prediction['risk_score']}\n"
        f"risk_level: {prediction['risk_level']}\n"
        f"recommendation: {prediction['recommendation']}"
    )
