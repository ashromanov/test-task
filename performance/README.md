# Производительность

Locust 2.46.7 находится в отдельной dependency group `load`; в runtime-образ не устанавливается.

## Повторить замеры

```bash
docker compose -f compose.yaml -f compose.test.yaml up --build -d --wait
mkdir -p performance/local

# HTTP: получение платежа и replay существующего ключа; новые платежи только при старте users.
API_KEY=local-development-key uv run --group load locust \
  -f load/locustfile.py --headless --host http://127.0.0.1:18000 \
  -u 20 -r 5 -t 60s --only-summary \
  --csv performance/local/http --html performance/local/http.html

# Полная цепочка: до 4 новых платежей/с, обычная эмуляция 2–5 секунд и настоящий webhook.
API_KEY=local-development-key LOAD_PROFILE=pipeline uv run --group load locust \
  -f load/locustfile.py --headless --host http://127.0.0.1:18000 \
  -u 20 -r 5 -t 60s --only-summary \
  --csv performance/local/pipeline --html performance/local/pipeline.html

docker compose -f compose.yaml -f compose.test.yaml down -v
```

Это разные показатели: высокая скорость HTTP-приёма не означает такую же скорость проведения платежей. HTTP-профиль проверяет API/идемпотентность без накопления тысяч сообщений; pipeline-профиль ограничивает входящий поток относительно предела эмулятора.

Скрипт `uv run --group load python scripts/benchmark.py --output performance/local` сбрасывает только тестовые volumes, измеряет холодный запуск с уже скачанными образами, повторный запуск без изменений и оба профиля. В конце проверяет отсутствие pending-платежей и сохраняет снимок памяти контейнеров. Не запускайте одновременно с интеграционными тестами.

В GitHub Actions доступен ручной workflow [Optional load test](https://github.com/ashromanov/test-task/actions/workflows/load.yml), его отчёты прикладываются как artifacts.

## Измеренный запуск — 06.10.2026

Источник: [успешный workflow](https://github.com/ashromanov/test-task/actions/runs/37467454781), commit `b72d528c7e01fc53873abf2201157db65f3ecf5d`. GitHub-hosted Ubuntu runner: **4 vCPU AMD EPYC 7763, 16 ГБ RAM**, API и Locust на одной машине, loopback HTTP. Это измерение изолированного Docker-стека в CI, не публичного HTTPS сервера.

20 пользователей, набор по 5 пользователей/с, каждый профиль — 60 секунд. Demo rate limit выключен; эмуляция сохранена без ускорения: 2–5 секунд, prefetch 16.

| Сценарий | Запросы | Ошибки | Средняя скорость | p50 | p95 |
|---|---:|---:|---:|---:|---:|
| HTTP: GET + idempotent POST + setup | 28 789 | 0 | **487,43 запроса/с** | 39 мс | 87 мс |
| GET payment | 14 378 | 0 | 243,44 запроса/с | 37 мс | 86 мс |
| POST idempotent replay | 14 391 | 0 | 243,66 запроса/с | 42 мс | 88 мс |
| POST нового платежа, pipeline | 240 | 0 | **4,14 запроса/с** | 13 мс | 18 мс |

Всего создано **280 платежей**, включая начальные платежи обоих профилей. После завершения нагрузки и 10 секунд ожидания **280/280 webhook доставлены, pending = 0**. Результаты эмуляции: 246 succeeded, 34 failed; бизнес-отказ также получил webhook. Скорость POST — приём в очередь, а не измеренный максимум полной обработки.

Снимок памяти **после** нагрузки, без тестового получателя и нагрузочного генератора:

| Контейнер | Память |
|---|---:|
| API | 67,48 MiB |
| Consumer + Outbox publisher | 75,34 MiB |
| PostgreSQL | 62,29 MiB |
| RabbitMQ | 95,95 MiB |
| **Всего** | **301,06 MiB** |

Это один post-load снимок `docker stats`, не максимальная память за всё время. Повторный `compose up --wait` в этом раннем запуске занял 14,03 с и пересоздал контейнеры; это не чистый cold-start benchmark. В текущем Compose общий образ собирается один раз для API и переиспользуется consumer; скрипт отдельно измеряет cold/unchanged запуск.

Исходные результаты: [CSV HTTP](2026-10-06-ci/http_stats.csv), [CSV pipeline](2026-10-06-ci/pipeline_stats.csv), [состояние БД](2026-10-06-ci/pipeline_database.csv), [память](2026-10-06-ci/containers.jsonl), [окружение](2026-10-06-ci/environment.json). HTML-отчёты: [HTTP](2026-10-06-ci/http.html), [pipeline](2026-10-06-ci/pipeline.html) — скачайте и откройте в браузере.
