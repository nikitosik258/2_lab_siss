"""Asynchronous SQLite setup for persistent prediction storage."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "agro_scoring.db"
DB_BUSY_TIMEOUT_MS = 5_000


@asynccontextmanager
async def get_db():
    """Yield one configured asynchronous SQLite connection."""

    db = await aiosqlite.connect(
        str(DATABASE_PATH),
        timeout=DB_BUSY_TIMEOUT_MS / 1000,
    )
    db.row_factory = aiosqlite.Row
    await db.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS};")

    try:
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    """Create the persistent predictions table and its lookup index."""

    async with get_db() as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                farm_id TEXT NOT NULL,
                risk_score REAL NOT NULL,
                risk_level TEXT NOT NULL,
                recommendation TEXT NOT NULL,
                model_version TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_predictions_request_id
            ON predictions(request_id);
            """
        )
        await db.commit()

    logger.info("SQLite initialized | path=%s", DATABASE_PATH)
