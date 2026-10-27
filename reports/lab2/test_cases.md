# Лабораторная работа №2 — 16 тест-кейсов API

Дата финальной проверки: 2026-10-28.

Разделение результатов принципиально: локальные тесты используют временную SQLite-БД; LLM mock-тесты не являются подтверждением внешней сети. В финальном запуске `pytest --basetemp .pytest-tmp -q` получено **46 passed in 7.55s**.

| № | Сценарий | Ожидаемый HTTP-код | Фактически полученный HTTP-код | Статус | Подтверждение / пояснение |
|---:|---|---:|---|---|---|
| 1 | `GET /health` | 200 | 200 | PASS | Реальный HTTP к локальному Uvicorn и автотест. |
| 2 | Валидный `POST /predict` | 200 | 200 | PASS | Временная SQLite, запись подтверждена автотестом. |
| 3 | `/predict`, `area_ha=-100` | 422 | 422 | PASS | Pydantic validation. |
| 4 | `/predict` с неизвестным регионом | 400 | 400 | PASS | Business validation. |
| 5 | Поиск существующего `request_id` | 200 | 200 | PASS | SQLite lookup после сохранения. |
| 6 | Поиск неизвестного `request_id` | 404 | 404 | PASS | Единый error body. |
| 7 | Валидный `POST /batch-predict` | 200 | 200 | PASS | Проверены порядок, общий ID и хранение строк. |
| 8 | Batch из 9 объектов | 422 | 422 | PASS | `MAX_BATCH_SIZE=8`. |
| 9 | Валидный `POST /predict-queued` | 200 | 200 | PASS | Worker и Future. |
| 10 | Переполненная очередь | 429 | 429 | PASS | Bounded `asyncio.Queue`, проверен `Retry-After`. |
| 11 | Валидный `/predictions/by-weekday` | 200 | 200 | PASS | SQLite `LIMIT`, `OFFSET`, `COUNT(*)`. |
| 12 | `page=-1` | 422 | 422 | PASS | Query validation. |
| 13 | Неизвестный `weekday` | 400 | 400 | PASS | Проверка допустимого дня. |
| 14 | Валидный `/llm/chat` | 200 | 200 (mock); 502 (финальный Vireonix) | PASS mock / FAIL real | Внешний Vireonix вернул `502 Bad Gateway`; результат не подменялся. |
| 15 | Недоступная LLM / ошибка провайдера | 502/503/504 | 502, 503, 504 (mock); 502 (Vireonix) | PASS | Проверено отображение provider, connection и timeout ошибок. |
| 16 | Валидный `/predict-with-llm` | 200 | 200 (mock); 502 (финальный Vireonix) | PASS mock / FAIL real | Scoring сохраняется до LLM-вызова; комбинированный endpoint не возвращает ложный 200. |

## Реальные проверки, выполненные в окружении

- Локальный Uvicorn: `GET /health` — 200, присутствуют `X-Request-ID` и `X-Process-Time`.
- Локальный Uvicorn: `GET /openapi.json` — 200; найдены все 8 обязательных маршрутов, всего 13 путей.
- Реальные вызовы Vireonix через `httpx.AsyncClient`: в финальном повторе оба получили 502 от `https://vireonix.ai/v1/chat/completions`. Ранее в том же окружении были получены 200 для короткого и scoring prompt, поэтому проблема является периодической внешней недоступностью, а не постоянной ошибкой payload.

## Покрытие дополнительных требований

- общий `asyncio.Lock`: `test_single_explicit_and_dynamic_inference_share_one_model_lock`;
- динамическое объединение запросов: `test_concurrent_http_requests_are_dynamically_batched`;
- сохранение после нового lifespan: `test_data_survives_a_new_application_lifespan`;
- rollback batch при ошибке хранилища: `test_batch_storage_failure_rolls_back_all_rows`;
- неизменность score/level LLM: `test_predict_with_llm_keeps_scoring_unchanged_and_hides_farm_data`.
