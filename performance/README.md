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

В GitHub Actions доступен ручной workflow [Optional load test](https://github.com/ashromanov/test-task/actions/workflows/load.yml), его отчёты прикладываются как artifacts. Измеренные результаты этого запуска будут добавлены отдельной датированной папкой после проверки.
