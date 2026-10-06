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

Источник: [успешный workflow](https://github.com/ashromanov/test-task/actions/runs/37470518577), commit `18bda58f7f5fc9822faa3b82c547c077da13c4f6`. GitHub-hosted Ubuntu runner: **4 vCPU AMD EPYC 7763, 16 ГБ RAM**, API и Locust на одной машине, loopback HTTP. Это измерение изолированного Docker-стека в CI, не публичного HTTPS сервера.

20 пользователей, набор по 5 пользователей/с, каждый профиль — 60 секунд. Demo rate limit выключен; эмуляция сохранена без ускорения: 2–5 секунд, prefetch 16.

| Сценарий | Запросы | Ошибки | Средняя скорость | p50 | p95 |
|---|---:|---:|---:|---:|---:|
| HTTP: GET + idempotent POST + setup | 27 567 | 0 | **474,76 запроса/с** | 40 мс | 88 мс |
| GET payment | 13 769 | 0 | 237,13 запроса/с | 38 мс | 87 мс |
| POST idempotent replay | 13 778 | 0 | 237,28 запроса/с | 43 мс | 91 мс |
| POST нового платежа, pipeline | 240 | 0 | **4,14 запроса/с** | 13 мс | 19 мс |

Всего создано **280 платежей**, включая начальные платежи обоих профилей. После завершения нагрузки и 10 секунд ожидания **280/280 webhook доставлены, pending = 0**. Результаты эмуляции: 258 succeeded, 22 failed; бизнес-отказ также получил webhook. Скорость POST — приём в очередь, а не измеренный максимум полной обработки.

Снимок памяти **после** нагрузки, без тестового получателя и нагрузочного генератора:

| Контейнер | Память |
|---|---:|
| API | 67,39 MiB |
| Consumer + Outbox publisher | 76,54 MiB |
| PostgreSQL | 56,82 MiB |
| RabbitMQ | 89,64 MiB |
| **Всего** | **290,39 MiB** |

Это один post-load снимок `docker stats`, не максимальная память за всё время. **Холодный запуск со скачанными образами и пустыми volumes — 8,67 с**; повторный `compose up --wait` без изменений — **1,61 с**. Скачивание образов и их сборка в cold-start не включены. Общий образ собирается один раз для API и переиспользуется consumer.

Исходные результаты: [CSV HTTP](2026-10-06-refactor/http_stats.csv), [CSV pipeline](2026-10-06-refactor/pipeline_stats.csv), [состояние БД](2026-10-06-refactor/pipeline_database.csv), [память](2026-10-06-refactor/containers.jsonl), [окружение](2026-10-06-refactor/environment.json). HTML-отчёты: [HTTP](2026-10-06-refactor/http.html), [pipeline](2026-10-06-refactor/pipeline.html) — скачайте и откройте в браузере.

Предыдущие замеры сохранены в [2026-10-06-ci](2026-10-06-ci/) и [2026-10-06-ci-final](2026-10-06-ci-final/): 487,43 и 673,24 HTTP-запроса/с соответственно. Последний прогон выполнен после рефакторинга в feature-first layout. Прогоны выполнялись на разных CI runners; такие результаты не доказывают ускорение или замедление именно из-за рефакторинга. Это короткие smoke/load проверки, не поиск предельной нагрузки или долговременный endurance-тест.
