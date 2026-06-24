# Operations Runbook

## Local Safety Rules

- Do not commit `.env`, raw HDE/ticket exports, API keys, tokens, passwords, or server dumps.
- Keep private ticket datasets only under `data/private/`; this path is ignored by Git.
- Run LLM evals with an explicit budget: `--max-llm-cost-rub` or `--max-cases`.
- Run KB validation before indexing:
  `python scripts/index_kb.py --validate-only`.
- For production indexing, require a passed quality gate:
  `python scripts/index_kb.py --require-quality-gate --quality-gate reports/quality_suite/quality_gate.json`.

## Preflight Before Pilot

```powershell
.venv\Scripts\ruff.exe check .
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe scripts\index_kb.py --validate-only
docker compose ps
Invoke-RestMethod -Uri http://localhost:8080/ready
```

Run quality suite with a bounded LLM budget:

```powershell
.venv\Scripts\python.exe -m eval.run_quality_suite --max-llm-cost-rub 100 --ask-max-cases 50
.venv\Scripts\python.exe -m eval.build_demo_quality_report --metrics reports\quality_suite\ask_eval.json --output reports\demo_quality.md
```

## PostgreSQL Backup

Create a local dump from Docker:

```powershell
docker compose exec postgres pg_dump -U rosmol -d rosmol_ai_bot -Fc -f /tmp/rosmol_ai_bot.dump
docker compose cp postgres:/tmp/rosmol_ai_bot.dump .\backups\rosmol_ai_bot.dump
```

Restore into an empty database:

```powershell
docker compose cp .\backups\rosmol_ai_bot.dump postgres:/tmp/rosmol_ai_bot.dump
docker compose exec postgres pg_restore -U rosmol -d rosmol_ai_bot --clean --if-exists /tmp/rosmol_ai_bot.dump
```

## Qdrant Backup

For local Docker volumes, prefer filesystem-level snapshots while Qdrant is stopped:

```powershell
docker compose stop qdrant
docker run --rm -v rosmol-ai-bot_qdrant_storage:/qdrant/storage -v ${PWD}\backups:/backup alpine tar czf /backup/qdrant_storage.tgz -C /qdrant/storage .
docker compose start qdrant
```

Restore a Qdrant volume snapshot only into a stopped Qdrant service:

```powershell
docker compose stop qdrant
docker run --rm -v rosmol-ai-bot_qdrant_storage:/qdrant/storage -v ${PWD}\backups:/backup alpine sh -c "rm -rf /qdrant/storage/* && tar xzf /backup/qdrant_storage.tgz -C /qdrant/storage"
docker compose start qdrant
```

## Runtime Checks

- `/health` checks the FastAPI process.
- `/ready` checks Redis, PostgreSQL and Qdrant.
- Request traces in PostgreSQL are the source of truth for model choice, source chunks, escalation reason, LLM usage and cost.
- If RAG/ML dependencies are unavailable, the API must return controlled escalation rather than an unhandled error.
