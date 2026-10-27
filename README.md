# Agro Scoring API

Учебный AI-сервис на FastAPI для детерминированной оценки агрориска. Проект развивает первую лабораторную работу: добавляет сквозной `request_id`, SQLite-хранилище, explicit и dynamic batching, backpressure, таймауты и текстовое пояснение через внешний LLM API Vireonix.

Расчёт `risk_score` выполняется только локальной функцией `calculate_risk`. LLM не участвует в вычислении и не может изменить `risk_score` или `risk_level`.

## Технологии

- Python, FastAPI и Pydantic;
- `asyncio`: очередь, worker, `Future`, lock и timeout;
- SQLite через `aiosqlite` (WAL, параметризованные запросы);
- `httpx.AsyncClient` для OpenAI-совместимого Vireonix API;
- pytest, httpx MockTransport для автономных тестов.

## Структура

| Файл | Назначение |
|---|---|
| `main.py` | приложение, lifespan, middleware и HTTP endpoints |
| `schemas.py` | Pydantic-контракты запросов и ответов |
| `model.py` | исходная формула `calculate_risk` |
| `services.py` | бизнес-валидация, уровень риска, рекомендации |
| `inference.py` | единая single/batch точка inference с общим lock |
| `batching.py` | bounded `asyncio.Queue` и dynamic batch worker |
| `database.py` | подключение SQLite и инициализация схемы |
| `repository.py` | асинхронные SQL-операции и транзакции |
| `llm_client.py` | запрос и проверка ответа Vireonix |
| `errors.py` | единый формат ошибок |
| `config.py` | лимиты batching, timeout и LLM-настройки |
| `tests/` | регрессионные, SQLite, batching и LLM-тесты |
| `reports/lab2/` | материалы для отчёта лабораторной №2 |

## Установка и запуск

PowerShell в корне проекта:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Swagger UI: <http://127.0.0.1:8000/docs>
OpenAPI JSON: <http://127.0.0.1:8000/openapi.json>

При запрете execution policy активировать окружение не требуется: используйте `.\.venv\Scripts\python.exe` напрямую, как в командах выше.

## Сквозная идентификация и ошибки

Передайте `X-Request-ID`, чтобы задать идентификатор самостоятельно; иначе middleware создаст UUID4. Во все ответы добавляются `X-Request-ID` и `X-Process-Time`; идентификатор попадает в логи и SQLite.

Контролируемые ошибки имеют вид:

```json
{
  "detail": "Описание ошибки или список ошибок Pydantic",
  "request_id": "..."
}
```

Поддерживаются 400, 404, 409, 422, 429, 500, 502, 503 и 504.

## Endpoints

| Endpoint | Назначение |
|---|---|
| `GET /health` | состояние API |
| `GET /model-info` | информация о модели |
| `POST /predict` | одиночный прогноз с сохранением в SQLite |
| `POST /batch-predict` | явный batch от 1 до 8 объектов |
| `POST /predict-queued` | одиночный прогноз через dynamic batching queue |
| `GET /queue-status` | размер и лимиты очереди |
| `GET /predictions` | история, `limit` и `risk_level` |
| `GET /predictions/by-weekday` | LIMIT/OFFSET-пагинация по дню недели |
| `GET /predictions/{request_id}` | ровно один результат; 409 для batch |
| `GET /requests/{request_id}/predictions` | все результаты одного request ID |
| `POST /demo/seed-weekdays` | явное создание демонстрационных записей |
| `POST /llm/chat` | произвольный prompt к Vireonix |
| `POST /predict-with-llm` | прогноз, сохранение и LLM-пояснение |

Полный контракт — в [`reports/lab2/api_contract.md`](reports/lab2/api_contract.md).

## Примеры

Проверка API:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Одиночный прогноз:

```powershell
$farm = @{
  farm_id = 'FARM-001'; region = 'Krasnodar'; crop_type = 'wheat'
  area_ha = 100; temperature_avg = 20; precipitation_mm = 80
  payment_delay_days = 10; previous_defaults = 0; debt = 100000
} | ConvertTo-Json

Invoke-RestMethod http://127.0.0.1:8000/predict -Method Post `
  -Headers @{ 'X-Request-ID' = 'demo-predict-001' } `
  -ContentType 'application/json' -Body $farm
```

Explicit batch:

```powershell
$batch = @{ items = @(
  ($farm | ConvertFrom-Json)
  ($farm | ConvertFrom-Json)
) } | ConvertTo-Json -Depth 4

Invoke-RestMethod http://127.0.0.1:8000/batch-predict -Method Post `
  -ContentType 'application/json' -Body $batch
```

Пагинация:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/predictions/by-weekday?weekday=monday&page=1&per_page=5'
```

## Batching, очередь и SQLite

Значения в `config.py`:

- `MAX_BATCH_SIZE = 8` — максимум элементов explicit/dynamic batch;
- `MAX_BATCH_WAIT_MS = 50` — окно накопления dynamic batch;
- `QUEUE_MAX_SIZE = 100` — ограничение очереди; переполнение возвращает 429;
- `INFERENCE_TIMEOUT_SECONDS = 2.0` — deadline вычисления и ожидания результата.

SQLite-файл `agro_scoring.db` создаётся при lifespan-старте, включает WAL и индекс по `request_id`. Все операции выполняются через `aiosqlite` и `await`. Explicit batch использует один `executemany()` и один `commit()`, откатывая транзакцию при ошибке.

## Vireonix LLM

Настройки находятся в `config.py`:

```python
LLM_BASE_URL = "https://vireonix.ai/v1"
LLM_MODEL = "auto"
LLM_TIMEOUT_SECONDS = 60.0
```

Клиент `httpx.AsyncClient` создаётся один раз в lifespan и закрывается при завершении приложения. Авторизация по методичке не используется. Сетевые ошибки отображаются как 503, timeout как 504, ответ провайдера или некорректный JSON как 502. Внешний провайдер может быть временно недоступен; mock-тесты не зависят от сети.

## Тесты

```powershell
.\.venv\Scripts\python.exe -m pytest --basetemp .pytest-tmp -q
```

Тесты создают временные SQLite-БД и не удаляют пользовательский `agro_scoring.db`. Результаты финальной проверки и инструкция для отчёта находятся в [`reports/lab2/`](reports/lab2/).
