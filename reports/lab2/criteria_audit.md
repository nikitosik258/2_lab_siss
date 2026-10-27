# Лабораторная работа №2 — аудит критериев оценивания

Проверка выполнена по таблице 4 методички. Балл присваивается только подтверждённой функциональности. Полная реальная успешность Vireonix в финальном запуске не подтверждена: provider вернул 502, поэтому два LLM endpoint-критерия временно не включены в подтверждённую сумму.

| Критерий | Макс. | Подтверждено | Реализован | Файлы и функции | Тесты / проверка | Замечание |
|---|---:|---:|---|---|---|---|
| Приложение запускается, Swagger/OpenAPI доступен | 5 | 5 | Да | `main.py`, `app` | Локально `/health` и `/openapi.json` — 200 | 13 путей в OpenAPI |
| Middleware: request ID и process time | 10 | 10 | Да | `main.py`, middleware | `test_health_generates_uuid_request_id`, `test_health_preserves_client_request_id` | Headers и логи есть |
| request ID в ответе, логах и БД | 5 | 5 | Да | `main.py`, `repository.py` | `test_predict_persists_request_id_and_result_in_sqlite` | Сквозная трассировка |
| SQLite через `aiosqlite` и `await` | 10 | 10 | Да | `database.py`, `repository.py` | тесты хранения и DB outage | WAL, parameterized SQL |
| `/predict` сохраняет прогноз | 5 | 5 | Да | `main.py:predict`, `save_prediction` | `test_predict_persists_request_id_and_result_in_sqlite` | Commit до 200 |
| История и поиск по request ID | 5 | 5 | Да | `repository.py`, GET routes | history/lookup/409 tests | Поддержан batch lookup |
| LIMIT/OFFSET + COUNT пагинация | 10 | 10 | Да | `get_predictions_by_weekday_page` | weekday pagination tests | Проверены границы/ошибки |
| Explicit `/batch-predict` | 10 | 10 | Да | `model_predict_batch`, `save_predictions_batch` | batch order, one call, rollback tests | Max 8, один commit |
| `asyncio.Queue` и dynamic batching | 10 | 10 | Да | `DynamicBatcher` | worker/grouping/concurrent HTTP tests | Future соответствует запросу |
| Backpressure и timeout | 5 | 5 | Да | `predict_queued`, `DynamicBatcher` | queue full и timeout tests | 429/504 |
| `/queue-status` и параметры очереди | 5 | 5 | Да | `queue_status`, `config.py` | queued endpoint tests + OpenAPI | Лимиты видны клиенту |
| Внешняя LLM через `httpx.AsyncClient` | 5 | 5 | Да | lifespan, `call_vireonix_llm` | mock tests; реальный provider достигнут и ответил 502 | Lifecycle и error mapping подтверждены |
| `/llm/chat` | 5 | 0 | Частично | `llm_chat`, `LLMChatRequest` | mock success; финальный Vireonix 502 | Реальный успешный 200 нужен для начисления |
| `/predict-with-llm`, разделение score/explanation | 5 | 0 | Частично | `predict_with_llm`, prompt builder | mock success, immutability and persistence tests | Реальный combined 200 нужен |
| Ошибки и API-контракт | 5 | 5 | Да | `errors.py`, schemas, routes | 400/404/409/422/429/502/503/504 tests | 422 сохраняет details |
| **Итого** | **100** | **90** | — | — | — | **10 баллов удержаны до успешного реального LLM-вызова** |

## Аудит качества кода

| Проверка | Результат |
|---|---|
| Модульность / отсутствие неоправданного дублирования | PASS: responsibilities разделены между app, schemas, inference, storage, batcher и LLM client. |
| `async`/`await` | PASS: SQLite, HTTP, queue, locks и timeout используются awaitably; нет `sqlite3.execute` внутри endpoint. |
| Lifecycle | PASS: lifespan вызывает `init_db`, запускает worker и создаёт один `httpx.AsyncClient`; в `finally` worker останавливается, client закрывается. |
| Фоновые задачи | PASS: worker cancel и queued futures обрабатываются в `DynamicBatcher.stop`. |
| Транзакционность | PASS: explicit batch использует rollback при исключении. |
| Сеть | PASS: timeout → 504, request error → 503, provider/JSON → 502. |
| Pydantic | PASS: входы и основные выходы имеют схемы и ограничения. |
| Совместимость с лабораторной №1 | PASS: формула и старые endpoints сохранены; регрессионный набор проходит. |
| `.gitignore` и секреты | PASS: окружения, кеши, БД/WAL/SHM, `.env` и локальные артефакты исключены; в коде API key нет. |
| Стабильность тестов | PASS: полный pytest — 46 passed in 7.55s. |
