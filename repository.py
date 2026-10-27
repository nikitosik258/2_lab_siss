"""Asynchronous persistence operations for predictions."""

import logging
import math
import uuid
from typing import Optional

import aiosqlite
from fastapi import HTTPException, status

import database


logger = logging.getLogger(__name__)


def storage_unavailable(exc: aiosqlite.Error) -> HTTPException:
    logger.exception("SQLite operation failed")
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Prediction storage is unavailable",
    )


async def save_prediction(result: dict, source: str) -> None:
    """Persist one prediction and return only after its transaction is committed."""

    try:
        async with database.get_db() as db:
            await db.execute(
                """
                INSERT INTO predictions (
                    request_id, farm_id, risk_score, risk_level,
                    recommendation, model_version, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    result["request_id"],
                    result["farm_id"],
                    result["risk_score"],
                    result["risk_level"],
                    result["recommendation"],
                    result["model_version"],
                    source,
                ),
            )
            await db.commit()
    except aiosqlite.Error as exc:
        raise storage_unavailable(exc) from exc


async def save_predictions_batch(request_id: str, items: list[dict]) -> None:
    """Store a complete explicit batch using one transaction and one commit."""

    rows = [
        (
            request_id,
            item["farm_id"],
            item["risk_score"],
            item["risk_level"],
            item["recommendation"],
            item["model_version"],
            "batch",
        )
        for item in items
    ]

    try:
        async with database.get_db() as db:
            try:
                await db.executemany(
                    """
                    INSERT INTO predictions (
                        request_id, farm_id, risk_score, risk_level,
                        recommendation, model_version, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    rows,
                )
                await db.commit()
            except aiosqlite.Error:
                await db.rollback()
                raise
    except aiosqlite.Error as exc:
        raise storage_unavailable(exc) from exc


async def get_predictions(
    limit: int,
    risk_level: Optional[str] = None,
) -> list[dict]:
    """Return newest predictions, optionally filtered by risk level."""

    query = """
        SELECT request_id, farm_id, risk_score, risk_level,
               recommendation, model_version
        FROM predictions
    """
    parameters: tuple[object, ...]

    if risk_level is None:
        query += " ORDER BY id DESC LIMIT ?;"
        parameters = (limit,)
    else:
        query += " WHERE risk_level = ? ORDER BY id DESC LIMIT ?;"
        parameters = (risk_level, limit)

    try:
        async with database.get_db() as db:
            cursor = await db.execute(query, parameters)
            rows = await cursor.fetchall()
            await cursor.close()
    except aiosqlite.Error as exc:
        raise storage_unavailable(exc) from exc

    return [dict(row) for row in rows]


async def get_predictions_by_request_id(request_id: str) -> list[dict]:
    """Return every prediction belonging to one incoming HTTP request."""

    try:
        async with database.get_db() as db:
            cursor = await db.execute(
                """
                SELECT request_id, farm_id, risk_score, risk_level,
                       recommendation, model_version
                FROM predictions
                WHERE request_id = ?
                ORDER BY id;
                """,
                (request_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
    except aiosqlite.Error as exc:
        raise storage_unavailable(exc) from exc

    return [dict(row) for row in rows]


async def get_predictions_by_weekday_page(
    weekday_number: str,
    page: int,
    per_page: int,
) -> tuple[list[dict], int, int]:
    """Return one LIMIT/OFFSET page and metadata for a weekday filter."""

    offset = (page - 1) * per_page

    try:
        async with database.get_db() as db:
            cursor = await db.execute(
                """
                SELECT request_id, farm_id, risk_score, risk_level,
                       recommendation, model_version
                FROM predictions
                WHERE strftime('%w', created_at) = ?
                ORDER BY id DESC
                LIMIT ? OFFSET ?;
                """,
                (weekday_number, per_page, offset),
            )
            rows = await cursor.fetchall()
            await cursor.close()

            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM predictions
                WHERE strftime('%w', created_at) = ?;
                """,
                (weekday_number,),
            )
            total_items = (await cursor.fetchone())[0]
            await cursor.close()
    except aiosqlite.Error as exc:
        raise storage_unavailable(exc) from exc

    total_pages = math.ceil(total_items / per_page) if total_items else 0
    return [dict(row) for row in rows], total_items, total_pages


async def seed_weekday_predictions(count: int) -> int:
    """Explicitly add demo rows across weekdays for manual pagination checks."""

    rows = [
        (
            str(uuid.uuid4()),
            f"DEMO-{index + 1:03}",
            0.1,
            "low",
            "Demo prediction",
            "1.0",
            "demo-seed",
            f"-{index % 7} days",
        )
        for index in range(count)
    ]

    try:
        async with database.get_db() as db:
            await db.executemany(
                """
                INSERT INTO predictions (
                    request_id, farm_id, risk_score, risk_level,
                    recommendation, model_version, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', ?));
                """,
                rows,
            )
            await db.commit()
    except aiosqlite.Error as exc:
        raise storage_unavailable(exc) from exc

    return count
