# Лабораторная работа №2 — чек-лист материалов отчёта

Состояние на 2026-10-28. Статус «требуется скриншот» означает, что endpoint реализован, но изображение не создавалось и не выдаётся за готовое.

| № | Требование методички | Статус | Материал / действие |
|---:|---|---|---|
| 1 | Цель работы | Готов | `architecture.md`, раздел «Цель работы». |
| 2 | Описание расширенной архитектуры | Готов | `architecture.md`. |
| 3 | Ссылка на исходный код | Требуется ручная проверка | Локальный Git создан; URL удалённого GitHub-репозитория отсутствует. |
| 4 | Скриншот Swagger UI | Требуется скриншот | Открыть `/docs`, сохранить `01_swagger.png`. |
| 5 | Скриншот `POST /predict` | Требуется скриншот | Выполнить валидный запрос с headers, сохранить `02_predict.png`. |
| 6 | SQLite или `GET /predictions` | Требуется скриншот | После predict открыть `/predictions`, сохранить `03_predictions.png`. |
| 7 | Скриншот `POST /batch-predict` | Требуется скриншот | Batch из 2–3 элементов, `04_batch_predict.png`. |
| 8 | Скриншот `GET /queue-status` | Требуется скриншот | Открыть endpoint, `05_queue_status.png`. |
| 9 | Скриншот weekday pagination | Требуется скриншот | Явно вызвать `/demo/seed-weekdays?count=14`, затем weekday endpoint, `06_pagination.png`. |
| 10 | Скриншот `POST /llm/chat` | Требуется скриншот и сеть | Вызвать короткий prompt; сохранить 200 либо фактический 502/503/504 как `07_llm_chat.png`. |
| 11 | Скриншот `POST /predict-with-llm` | Требуется скриншот и сеть | Выполнить запрос, сохранить фактический результат как `08_predict_with_llm.png`. |
| 12 | Таблица тест-кейсов | Готов | `test_cases.md`. |
| 13 | Таблица API-контракта | Готов | `api_contract.md`. |
| 14 | Вывод о request_id, batching, backpressure, БД, LLM | Готов | `architecture.md`, разделы «Роль механизмов» и «Заключение». |

## Команды для ручной части

```powershell
cd C:\Users\Nocomp\Desktop\2_lab_siss
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Затем открыть <http://127.0.0.1:8000/docs>. Для демонстрационных дат разрешён только явный endpoint:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/demo/seed-weekdays?count=14' -Method Post
Invoke-RestMethod 'http://127.0.0.1:8000/predictions/by-weekday?weekday=monday&page=1&per_page=5'
```

Проверка тестов:

```powershell
.\.venv\Scripts\python.exe -m pytest --basetemp .pytest-tmp -q
```

## GitHub после создания отдельного удалённого репозитория

Не указан remote URL, поэтому он не создавался автоматически. После создания пустого репозитория на GitHub вручную выполнить:

```powershell
git remote add origin https://github.com/<your-account>/<your-repository>.git
git branch -M main
git commit -m "Initial Agro Scoring API lab 2"
git push -u origin main
```

Перед `git push` проверить:

```powershell
git status
git check-ignore -v .venv agro_scoring.db .env
git ls-files
```
