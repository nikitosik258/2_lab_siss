# Лабораторная работа №2 — API-контракт

Все ответы проходят middleware: `X-Request-ID` либо принимается от клиента, либо создаётся как UUID4; `X-Process-Time` добавляется в response. Контролируемая ошибка содержит `detail` и `request_id`, а 422 сохраняет Pydantic details.

| Endpoint | Method | Input | Output | Success | Возможные ошибки |
|---|---|---|---|---:|---|
| `/health` | GET | — | `{status}` | 200 | 500 |
| `/model-info` | GET | — | имя, версия, тип, статус модели | 200 | 500 |
| `/predict` | POST | `FarmRequest` | `PredictionResponse` | 200 | 400, 422, 503, 504 |
| `/batch-predict` | POST | `BatchRequest`, 1–8 `FarmRequest` | `BatchPredictionResponse` | 200 | 400, 422, 503, 504 |
| `/predict-queued` | POST | `FarmRequest` | `PredictionResponse` | 200 | 400, 422, 429, 503, 504 |
| `/queue-status` | GET | — | очередь, лимиты, timeout | 200 | 500 |
| `/predictions` | GET | `limit=1..100`, optional `risk_level` | `list[PredictionResponse]` | 200 | 400, 422, 503 |
| `/predictions/by-weekday` | GET | `weekday`, `page>=1`, `per_page>=1` | `{data, meta}` | 200 | 400, 404, 422, 503 |
| `/predictions/{request_id}` | GET | path `request_id` | один `PredictionResponse` | 200 | 404, 409, 503 |
| `/requests/{request_id}/predictions` | GET | path `request_id` | все `PredictionResponse` одного HTTP-запроса | 200 | 404, 503 |
| `/demo/seed-weekdays` | POST | query `count=7..1000` | статус и число demo-строк | 200 | 422, 503 |
| `/llm/chat` | POST | `LLMChatRequest` | `LLMChatResponse` | 200 | 422, 502, 503, 504 |
| `/predict-with-llm` | POST | `FarmRequest` | `PredictWithLLMResponse` | 200 | 400, 422, 502, 503, 504 |

## Основные Pydantic-модели

- `FarmRequest`: farm ID, регион, культура, площадь `>0`, погодные параметры, просрочка, дефолты, задолженность.
- `PredictionResponse`: request ID, farm ID, score, level, recommendation, model version.
- `BatchRequest`: список от 1 до 8 объектов `FarmRequest`.
- `BatchPredictionResponse`: общий request ID, размер, длительность и упорядоченные результаты.
- `PaginatedPredictionsResponse`: `data` и `meta` с total/current page и ссылками.
- `LLMChatRequest` / `LLMChatResponse`: prompt, generation settings и нормализованный ответ provider.
- `PredictWithLLMResponse`: локальный deterministic prediction и отдельный LLM result.

## Важные контрактные правила

1. Один batch имеет общий `request_id`; поэтому одиночный `/predictions/{request_id}` возвращает 409 при нескольких строках, а `/requests/{request_id}/predictions` возвращает весь batch.
2. `/predict-with-llm` сначала сохраняет deterministic prediction и только затем вызывает Vireonix. Ошибка LLM не удаляет прогноз, но endpoint возвращает 502/503/504 вместо ложного 200.
3. Static route `/predictions/by-weekday` зарегистрирован раньше динамического `/predictions/{request_id}`.
