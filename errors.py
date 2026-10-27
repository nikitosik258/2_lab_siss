"""Centralized HTTP error responses for the Agro Scoring API."""

import logging
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


logger = logging.getLogger(__name__)


def get_request_id(request: Request) -> str | None:
    """Return the ID assigned by request middleware, if available."""

    return getattr(request.state, "request_id", None)


def make_error_response(
    request: Request,
    *,
    status_code: int,
    detail: Any,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the API's common error response without losing error details."""

    return JSONResponse(
        status_code=status_code,
        content={
            "detail": jsonable_encoder(detail),
            "request_id": get_request_id(request),
        },
        headers=headers,
    )


async def http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    """Format expected API errors, including future 409/429/502/504 cases."""

    return make_error_response(
        request,
        status_code=exc.status_code,
        detail=exc.detail,
        headers=exc.headers,
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Keep Pydantic's detailed validation errors in a 422 response."""

    return make_error_response(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=exc.errors(),
    )


async def internal_server_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Avoid leaking internal exception details while preserving request tracing."""

    logger.exception(
        "Unhandled error | request_id=%s",
        get_request_id(request),
        exc_info=exc,
    )
    return make_error_response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Internal server error",
    )
