# Agro Scoring API — контракт API

Общие правила:

- Middleware принимает `X-Request-ID` или создаёт UUID4, сохраняет его в `request.state`, пишет в логи и возвращает в header ответа.
- Каждый ответ также содержит `X-Process-Time`.
- Формат контролируемой ошибки: `{"detail": "…", "request_id": "…"}`; у 422 `detail` сохраняет массив деталей Pydantic.
- Все маршруты доступны в Swagger UI (`/docs`) и OpenAPI (`/openapi.json`).

| Endpoint | Method | Input | Output | Success | Возможные ошибки |
|---|---|---|---|---:|---|
| `/health` | GET | — | `{status}` | 200 | 500 |
| `/model-info` | GET | — | имя, версия, тип и статус модели | 200 | 500 |
| `/predict` | POST | `FarmRequest` | `PredictionResponse` | 200 | 400, 422, 503, 504 |
| `/batch-predict` | POST | `BatchRequest`: 1–8 `FarmRequest` | request ID, размер, время, список результатов | 200 | 400, 422, 503, 504 |
| `/predict-queued` | POST | `FarmRequest` | `PredictionResponse` | 200 | 400, 422, 429, 503, 504 |
| `/queue-status` | GET | — | размер/лимиты очереди и timeout | 200 | 500 |
| `/predictions` | GET | `limit=1..100`, необязательный `risk_level` | список `PredictionResponse` | 200 | 400, 422, 503 |
| `/predictions/by-weekday` | GET | `weekday`, `page>=1`, `per_page>=1` | `{data, meta}` с пагинацией | 200 | 400, 404, 422, 503 |
| `/predictions/{request_id}` | GET | path `request_id` | один `PredictionResponse` | 200 | 404, 409, 503 |
| `/requests/{request_id}/predictions` | GET | path `request_id` | список `PredictionResponse` одного HTTP-запроса | 200 | 404, 503 |
| `/demo/seed-weekdays` | POST | query `count=7..1000` | статус и число тестовых записей | 200 | 422, 503 |
| `/llm/chat` | POST | `LLMChatRequest`: prompt, model, temperature, max_tokens | request ID, модели, answer, finish reason, latency, usage | 200 | 422, 502, 503, 504 |
| `/predict-with-llm` | POST | `FarmRequest` | `{prediction: PredictionResponse, llm: LLMChatResponse}` | 200 | 400, 422, 502, 503, 504 |

## Pydantic-модели

- `FarmRequest`: `farm_id`, `region`, `crop_type`, `area_ha>0`, допустимые метео- и финансовые значения.
- `PredictionResponse`: `request_id`, `farm_id`, `risk_score`, `risk_level`, `recommendation`, `model_version`.
- `BatchRequest`: поле `items`, от 1 до 8 элементов `FarmRequest`.
- `BatchPredictionResponse`: общий `request_id`, `batch_size`, `processing_time_ms`, упорядоченные `items`.
- `PaginatedPredictionsResponse`: `data` и `meta` (`weekday`, страницы, количество, ссылки на соседние страницы).
- `LLMChatRequest` / `LLMChatResponse`: ограниченный пользовательский prompt и нормализованный результат внешнего OpenAI-совместимого API.
- `PredictWithLLMResponse`: детерминированный агроскоринг и отдельное пояснение LLM.

## Семантика хранения и идентификаторов

- SQLite `predictions` хранит `request_id`, `farm_id`, score, level, recommendation, версию, source и время создания.
- Для single prediction `GET /predictions/{request_id}` возвращает одну запись.
- Для explicit/dynamic batch несколько записей имеют общий `request_id`; одиночный маршрут отвечает 409, а `/requests/{request_id}/predictions` возвращает весь список.
- `/predict-with-llm` сохраняет детерминированный результат **до** внешнего вызова. Поэтому при 502/503/504 LLM результат доступен по его `request_id`, хотя комбинированный endpoint не возвращает ложный 200.
