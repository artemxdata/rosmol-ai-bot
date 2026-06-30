# Pre-pilot release checklist

Этот чеклист нужен перед демонстрацией и тестовым подключением HDE/VK. Массовые проверки выполняются локально через `/ask`; HDE трогаем только тестовым каналом и короткими smoke-сценариями.

## 1. Локально перед push

```powershell
cd D:\projects\rosmol-ai-bot
.venv\Scripts\ruff.exe check .
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe scripts\index_kb.py --validate-only
```

Если поднят Docker ML-контур:

```powershell
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml ps
Invoke-RestMethod -Uri http://localhost:8080/ready
Invoke-RestMethod -Uri http://localhost:8001/ready
```

Quality suite:

```powershell
.venv\Scripts\python.exe -m eval.run_pre_pilot_quality_suite `
  --target http://localhost:8001/ask `
  --output-dir reports/pre_pilot_quality_suite `
  --max-llm-cost-rub 80
```

Критерий: `summary.json` должен иметь `passed=true`, без остановки по бюджету.

Админка по умолчанию показывает tracked презентационный отчёт:
`reports/presentation_quality/presentation_quality_report.json`. Если нужен другой файл,
задай `ADMIN_QUALITY_REPORT_PATH=<path>` в `.env` и пересоздай `app/app-ml`.

## 2. Серверное обновление

Выполняется вручную на сервере:

```bash
ssh root@139.100.225.44
cd /opt/rosmol-ai-bot
git fetch origin
git status --short --branch
git pull --ff-only
git log -1 --oneline
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml up -d --build app app-ml nginx
```

Если менялся `data/knowledge_base_seed.json`, после обновления кода переиндексировать KB:

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml run --rm index-kb \
  python scripts/index_kb.py --path data/knowledge_base_seed.json --embedding-batch-size 32
```

Проверить количество точек:

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml exec -T app-ml python - <<'PY'
from qdrant_client import QdrantClient
from src.config import get_settings

s = get_settings()
client = QdrantClient(url=s.qdrant_url)
print(client.count(collection_name=s.qdrant_knowledge_collection, exact=True).count)
PY
```

Ожидаемо: количество равно числу валидных опубликованных чанков в `knowledge_base_seed.json`.

## 3. Серверные smoke-проверки

```bash
python3 - <<'PY'
from urllib.request import urlopen

for url in [
    "http://127.0.0.1/ready",
    "http://127.0.0.1:8001/ready",
    "http://127.0.0.1/admin/kb",
]:
    r = urlopen(url, timeout=60)
    body = r.read(120).decode("utf-8", errors="ignore")
    print(url, r.status, body)
PY
```

Проверка `/ask` через ML-контур:

```bash
python3 - <<'PY'
import json
from pathlib import Path
from urllib.request import Request, urlopen

env = {}
for line in Path(".env").read_text(encoding="utf-8").splitlines():
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k] = v

payload = {
    "user_id": "server-smoke",
    "channel": "api",
    "text": "Как зарегистрироваться на форум?",
}
headers = {
    "Content-Type": "application/json; charset=utf-8",
    "X-Bypass-Cache": "true",
}
if env.get("API_AUTH_TOKEN"):
    headers["Authorization"] = "Bearer " + env["API_AUTH_TOKEN"]

req = Request(
    "http://127.0.0.1:8001/ask",
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers=headers,
    method="POST",
)
print(urlopen(req, timeout=240).read().decode("utf-8"))
PY
```

## 4. HDE/VK test contour

Перед тестами проверить:

- webhook URL указывает на `http://139.100.225.44/webhook/hde`;
- заголовок `X-Webhook-Secret` совпадает с серверным `WEBHOOK_AUTH_TOKEN`;
- правила включены только для тестового департамента/канала;
- старый Chatme-ответчик не отвечает параллельно в том же тестовом канале;
- широкие production-правила не включены.

HDE имеет общий лимит 300 RPM на систему. Массовые тесты через HDE не запускать. Для HDE достаточно трёх ручных smoke:

1. `Как зарегистрироваться на форум?` → ответ из базы.
2. `Позови оператора` → controlled escalation.
3. `Какая погода завтра в Москве?` → scope-note по зоне ответственности.

## 5. Rollback

Без крайней необходимости rollback не делать. Если после обновления сервер не отвечает:

1. Сначала проверить логи:

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml logs --tail=160 app app-ml nginx
```

2. Если нужно временно вернуться на предыдущий коммит, зафиксировать текущий `git log -2 --oneline`, затем вручную переключиться на предыдущий commit в detached mode и пересобрать:

```bash
git switch --detach <previous_commit>
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml up -d --build app app-ml nginx
```

3. После исправления вернуться на `master`:

```bash
git switch master
git pull --ff-only
```

Важно: rollback не должен менять `.env`, секреты, Docker volumes Postgres/Qdrant/Redis и production-данные.

## 6. One-command final acceptance

Перед демонстрацией можно прогнать весь локальный release gate одной командой:

```powershell
.venv\Scripts\python.exe scripts\run_acceptance.py `
  --target http://localhost:8001/ask `
  --max-llm-cost-rub 80
```

Команда проверяет:

- `ruff check .`;
- `pytest`;
- `scripts/index_kb.py --validate-only`;
- `/ready` для обычного и ML-контура;
- pre-pilot quality suite без массовых запросов в HDE.

Итоговые файлы:

- `reports/final_acceptance/summary.md`;
- `reports/final_acceptance/summary.json`;
- `reports/pre_pilot_quality_suite/summary.md`;
- `reports/pre_pilot_quality_suite/summary.json`.

Критерий допуска к показу: `reports/final_acceptance/summary.json` содержит `"passed": true`.
