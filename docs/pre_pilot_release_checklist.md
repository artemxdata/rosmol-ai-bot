# Pre-pilot release checklist

Этот чеклист нужен перед демонстрацией и тестовым подключением HDE/VK. Массовые проверки выполняются локально через `/ask`; HDE трогаем только тестовым каналом и короткими smoke-сценариями.

> **Текущий статус 23 июля 2026:** clean runtime `27cecaf` и limited HDE/VK smoke работают;
> HDE снова выключен на время Yonote/admin-KB цикла. Yonote egress proxy уже переключён на
> проверенные три destination, но `app-ml` capability ещё не активирована. Новый explicit
> test-editor candidate проходит локальный gate. Серверные команды в разделах 2 и 5
> сохранены только как исторический
> pre-incident checklist и **не должны выполняться**: они используют старый порядок, старый Compose
> stack и migration baseline. Разделы 3–4 задают только критерии, а не standalone-команды.
> Единственная актуальная исполнимая инструкция для нового HDE/VK test-production:
> `docs/recovery_test_production_runbook_20260720.md`. Старая VM, IP, webhook, admin URL и любые
> её artifacts запрещены.

## 1. Локально перед push

```powershell
cd D:\projects\rosmol-ai-bot
.venv\Scripts\ruff.exe check .
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe scripts\index_kb.py --validate-only `
  --forums-registry data\forums_registry.json
.venv\Scripts\python.exe scripts\audit_kb_seed.py `
  --forums-registry data\forums_registry.json --fail-on error
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

Быстрый преддемо smoke после локального Docker-запуска:

```powershell
.venv\Scripts\python.exe scripts\run_pre_demo_smoke.py `
  --target http://localhost:8001/ask `
  --output-dir reports/presentation_quality/pre_demo_smoke_latest `
  --fail-under 1.0
```

Он проверяет короткий набор показательных сценариев: составные вопросы по форумам, ФГАИС,
гранты, off-topic, operator requested, safety и PII masking. Отчёты появляются в:

- `reports/presentation_quality/pre_demo_smoke_latest/pre_demo_smoke.md`;
- `reports/presentation_quality/pre_demo_smoke_latest/pre_demo_smoke.json`.

Админка по умолчанию показывает tracked презентационный отчёт:
`reports/presentation_quality/presentation_quality_report.json`. Если нужен другой файл,
задай `ADMIN_QUALITY_REPORT_PATH=<path>` в `.env` и пересоздай `app/app-ml`.

## 2. Чистое развёртывание на новом сервере

Это не update и не rollback старого сервера. До начала должны быть отдельно завершены secret
rotation и базовое hardening новой VM. Сервер создаётся из чистого vendor image; repository
получается новым checkout из Git. Не копировать старый `/opt`, `.env`, certificates, SSH keys,
Docker images/volumes/cache, Redis, PostgreSQL/Qdrant backup или другие файлы старой VM.

На новой VM:

1. с доверенного устройства проверить GitHub deploy keys/tokens, audit history, commits/tags и
   Actions, отозвать старые server credentials и зафиксировать trusted commit;
2. сверить `origin/master` и ожидаемый trusted commit;
3. создать новый `.env` только из перевыпущенных секретов, не печатая значения;
4. собрать images с нуля;
5. создать PostgreSQL/Redis/Qdrant с нуля;
6. применить migration и полную published-only индексацию из trusted Git seed;
7. выпустить новый TLS certificate;
8. только затем поднимать ingress и выполнять preliminary acceptance.

До любого provisioning active infrastructure не должна содержать retired endpoint. Reviewed
parameterization patch должен быть частью trusted commit: `ADMIN_PUBLIC_HOST` обязателен, HTTP
admin закрыт до выпуска сертификата, cert path нейтрален, а readiness report не публикует URL до
handoff. Точные provider identifiers проверяются по private incident evidence, а не хранятся в
актуальном tracked tree.

Indexer берёт только `published` records, удаляет stale points и очищает semantic cache после
успешной KB mutation. Команды выполняются из clean checkout:

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml \
  build app app-ml index-kb
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml \
  stop nginx app app-ml
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml \
  run --rm migrate
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml \
  run --rm index-kb python scripts/init_qdrant.py
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml run --rm index-kb \
  python scripts/index_kb.py --path data/knowledge_base_seed.json \
    --forums-registry data/forums_registry.json --embedding-batch-size 32 --prune-stale
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml \
  up -d app app-ml nginx
```

Проверить migration и оба Qdrant count:

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml \
  exec -T app-ml alembic current
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml exec -T app-ml python - <<'PY'
from qdrant_client import QdrantClient
from src.config import get_settings

s = get_settings()
client = QdrantClient(url=s.qdrant_url)
for collection in (s.qdrant_knowledge_collection, 'response_cache'):
    print(collection, client.count(collection_name=collection, exact=True).count)
PY
```

Для code RC `8bca860` историческая baseline: Alembic head `007_hde_delivery_telemetry`,
`knowledge_base = 2152`, `response_cache = 0`. Новый count сверяется также с текущим trusted seed;
если migration, validation, index или count не совпали, runtime не отдавать операторам.

## 3. Серверные критерии acceptance

- `app`, `app-ml`, PostgreSQL, Redis и Qdrant не публикуют host ports; `8001` существует только в
  local/dev Compose.
- До TLS строгий ML `/ready` проверяется внутри `app-ml`, без host relay и SSH tunnel.
- После появления рабочего DNS и публично доверенного TLS runtime/security ingress gate обращается
  только к точному `https://ADMIN_PUBLIC_HOST`.
- Полный quality suite выполняется отдельным server-local `quality-acceptance` контейнером по
  internal `data` к `http://app-ml:8000/ask`; наружу production token и PostgreSQL не выносятся.
- После suite exact-HTTPS gate повторно сканирует runtime/Nginx/relay logs за всё окно quality.
- HDE/VK не используются для batch/eval-трафика.
- Yonote capability по умолчанию выключена. При отдельном enable новый read-only token получает
  только `app-ml`, а generated egress policy добавляет только exact `rossmol.yonote.ru:443`.
- В default read-only режиме аутентифицированный Preview читает полный configured collection set,
  но Apply возвращает `403`; до и после Preview совпадают tracked-seed hash и Qdrant counts,
  reindex не запускается.
- Explicit test-editor mode требует согласованную пару capability flags и exact private working
  seed path. `app` монтирует working seed read-only, `app-ml` writable; tracked seed остаётся
  неизменным. Full index не публикуется admin HTTP endpoint, HDE остаётся выключен до backup,
  controlled `--prune-stale`, restart, readiness/security и RAG smoke.

Точные fail-closed команды, SHA/provenance checks и output paths находятся только в Gate 4B и
«Финальный acceptance, привязанный к commit» файла
`docs/recovery_test_production_runbook_20260720.md`.

## 4. HDE/VK test contour

Перед тестами проверить:

- webhook URL указывает на новый согласованный HTTPS endpoint, а не на старый IP;
- заголовок `X-Webhook-Secret` совпадает с серверным `WEBHOOK_AUTH_TOKEN`;
- правила включены только для тестового департамента/канала;
- старый Chatme-ответчик не отвечает параллельно в том же тестовом канале;
- широкие production-правила не включены.

HDE имеет общий лимит 300 RPM на систему. Массовые тесты через HDE не запускать. Для HDE достаточно трёх ручных smoke:

1. `Как зарегистрироваться на форум?` → ответ из базы.
2. `Позови оператора` → controlled escalation.
3. `Какая погода завтра в Москве?` → scope-note по зоне ответственности.

## 5. Failure recovery нового контура

Старая VM не является rollback target. Старые image, disk, container volumes и backups не
восстанавливаются. Если новый чистый runtime не отвечает:

1. Остановить выдачу нового webhook и проверить логи:

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml logs --tail=160 app app-ml nginx
```

2. Если дефект только в коде и инфраструктура остаётся чистой, зафиксировать `git log -2
   --oneline`, вручную выбрать проверенный trusted commit и пересобрать **на новой VM**:

```bash
git switch --detach <previous_commit>
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml up -d --build app app-ml nginx
```

3. После исправления вернуться на `master`:

```bash
git switch master
git pull --ff-only
```

Важно: никакой failure recovery не возвращает старые secrets или artifacts. При подозрении на
новый security incident снова остановить VM и начать отдельный triage, а не выполнять rollback.

## 6. One-command final acceptance

Перед демонстрацией можно прогнать весь локальный release gate одной командой:

```powershell
.venv\Scripts\python.exe scripts\run_acceptance.py `
  --expected-git-sha <40_LOWERCASE_HEX_TRUSTED_SHA> `
  --target http://localhost:8001/ask `
  --max-llm-cost-rub 80
```

`--expected-git-sha` обязателен: это заранее зафиксированный trusted commit, а не значение,
которое acceptance должен молча принять из текущего checkout. Dirty worktree, другой SHA и
пропущенный шаг дают только `passed=false`.

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
