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

Источник: [успешный workflow](https://github.com/ashromanov/test-task/actions/runs/37468454921), commit `d129ff3d517b35fb59e67421fa15567c4edc9e7d`. GitHub-hosted Ubuntu runner: **4 vCPU AMD EPYC 7763, 16 ГБ RAM**, API и Locust на одной машине, loopback HTTP. Это измерение изолированного Docker-стека в CI, не публичного HTTPS сервера.

20 пользователей, набор по 5 пользователей/с, каждый профиль — 60 секунд. Demo rate limit выключен; эмуляция сохранена без ускорения: 2–5 секунд, prefetch 16.

| Сценарий | Запросы | Ошибки | Средняя скорость | p50 | p95 |
|---|---:|---:|---:|---:|---:|
| HTTP: GET + idempotent POST + setup | 39 752 | 0 | **673,24 запроса/с** | 28 мс | 59 мс |
| GET payment | 19 862 | 0 | 336,38 запроса/с | 27 мс | 58 мс |
| POST idempotent replay | 19 870 | 0 | 336,52 запроса/с | 30 мс | 59 мс |
| POST нового платежа, pipeline | 240 | 0 | **4,14 запроса/с** | 9 мс | 12 мс |

Всего создано **280 платежей**, включая начальные платежи обоих профилей. После завершения нагрузки и 10 секунд ожидания **280/280 webhook доставлены, pending = 0**. Результаты эмуляции: 255 succeeded, 25 failed; бизнес-отказ также получил webhook. Скорость POST — приём в очередь, а не измеренный максимум полной обработки.

Снимок памяти **после** нагрузки, без тестового получателя и нагрузочного генератора:

| Контейнер | Память |
|---|---:|
| API | 67,79 MiB |
| Consumer + Outbox publisher | 74,96 MiB |
| PostgreSQL | 56,96 MiB |
| RabbitMQ | 90,57 MiB |
| **Всего** | **290,28 MiB** |

Это один post-load снимок `docker stats`, не максимальная память за всё время. **Холодный запуск со скачанными образами и пустыми volumes — 6,44 с**; повторный `compose up --wait` без изменений — **1,57 с**. Скачивание образов и их сборка в cold-start не включены. Общий образ собирается один раз для API и переиспользуется consumer.

Исходные результаты: [CSV HTTP](2026-10-06-ci-final/http_stats.csv), [CSV pipeline](2026-10-06-ci-final/pipeline_stats.csv), [состояние БД](2026-10-06-ci-final/pipeline_database.csv), [память](2026-10-06-ci-final/containers.jsonl), [окружение](2026-10-06-ci-final/environment.json). HTML-отчёты: [HTTP](2026-10-06-ci-final/http.html), [pipeline](2026-10-06-ci-final/pipeline.html) — скачайте и откройте в браузере.

Предыдущий замер сохранён в [2026-10-06-ci](2026-10-06-ci/). Эти короткие прогоны не являются поиском предельной нагрузки или долговременным endurance-тестом.
