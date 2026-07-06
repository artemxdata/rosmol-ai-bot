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

Run a transparent manual inspection against the ML app without semantic cache
or shared Redis session context:

```powershell
.venv\Scripts\python.exe scripts\manual_ask.py --file data\manual_complex_queries.json --target http://localhost:8001/ask --max-cases 10 --bypass-cache --isolate-users --output reports\manual_complex_inspection.json
```

The `app-ml` profile uses a longer local timeout than the lightweight app
because CPU rerank plus Max synthesis can exceed 90 seconds on cold runs.

## Yonote KB Refresh

Yonote is read-only for the bot. We never create, edit, or delete Yonote
documents from this project. The bot only reads selected collections and
updates its own normalized RAG seed.

Required local/server `.env` values:

```env
YONOTE_API_TOKEN=<read-only-token>
YONOTE_BASE_URL=https://rossmol.yonote.ru
YONOTE_COLLECTION_NAMES=Росмолодёжь: общее, структура, направления;Росмолодёжь: мероприятия
YONOTE_SYNC_ENABLED=false
YONOTE_SYNC_MODE=manual
```

Build normalized Yonote chunks from API and merge them into the published KB seed:

```powershell
.venv\Scripts\python.exe scripts\sync_yonote_kb.py `
  --replace-existing-yonote `
  --extraction-date 2026-07-06
```

Preview without changing `data/knowledge_base_seed.json`:

```powershell
.venv\Scripts\python.exe scripts\sync_yonote_kb.py `
  --records-out data\private\yonote\yonote_api_records.preview.json `
  --replace-existing-yonote `
  --validate-only
```

ZIP exports are fallback source material only. Keep ZIP files under
`data/private/yonote/`; do not commit raw exports.

Fallback ZIP import:

```powershell
.venv\Scripts\python.exe scripts\build_yonote_kb_seed.py `
  --source-dir data\private\yonote `
  --base data\knowledge_base_seed.json `
  --out data\knowledge_base_seed.json `
  --replace-existing-yonote `
  --extraction-date 2026-07-04
```

Validate before indexing:

```powershell
.venv\Scripts\python.exe scripts\index_kb.py --validate-only
```

Rebuild the local Docker services and reindex Qdrant:

```powershell
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml up -d --build app app-ml nginx
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml run --rm index-kb `
  python scripts/index_kb.py --path data/knowledge_base_seed.json --embedding-batch-size 32 --prune-stale
```

After indexing, run smoke checks against `http://127.0.0.1:8001/ask` with
`X-Bypass-Cache: true`. For server rollout, repeat the same build/index
commands on the staging host after pulling the latest commit.

## Server Staging Deploy

Current staging target: Ubuntu 24.04 host with Docker already installed.
The application container is bound to localhost only; public traffic must go
through nginx.

Install missing base tools:

```bash
apt update
apt install -y git ufw ca-certificates curl
```

Add swap before enabling local ML/reranker workloads on an 11 GiB RAM host:

```bash
fallocate -l 8G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
cp /etc/fstab /etc/fstab.bak
echo '/swapfile none swap sw 0 0' >> /etc/fstab
free -h
```

Clone the private repository with a read-only GitHub deploy key. Do not paste
private keys or `.env` contents into chats:

```bash
ssh-keygen -t ed25519 -C "rosmol-ai-bot-deploy@rag-llmchatme" -f /root/.ssh/rosmol_ai_bot_deploy -N ""
cat /root/.ssh/rosmol_ai_bot_deploy.pub
```

After adding that public key in GitHub repository settings as a read-only
deploy key:

```bash
cat >> /root/.ssh/config <<'EOF'
Host github.com-rosmol
  HostName github.com
  User git
  IdentityFile /root/.ssh/rosmol_ai_bot_deploy
  IdentitiesOnly yes
EOF
chmod 600 /root/.ssh/config
ssh -T git@github.com-rosmol
git clone git@github.com-rosmol:artemxdata/rosmol-ai-bot.git /opt/rosmol-ai-bot
cd /opt/rosmol-ai-bot
cp .env.example .env
chmod 600 .env
```

Fill `.env` manually on the server. Minimum staging overrides:

```dotenv
APP_ENV=staging
NGINX_BIND=0.0.0.0
NGINX_HOST_PORT=80
INSTALL_ML=false
```

Also set real secrets and strong database credentials in `.env`: Cloud.ru API
key, API/webhook/admin tokens, `POSTGRES_PASSWORD`, and matching
`POSTGRES_DSN`.

Start and verify:

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1/ready
```

Open only required ports:

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw --force enable
ufw status
```

## HDE Dispatcher Payloads

Set the trigger marker only in server `.env`, not in Git:

```dotenv
HDE_TRIGGER_PREFIX=<dispatcher-start-marker>
```

For the HDE dispatcher rule that starts bot processing for a new ticket:

```json
{
  "event": "new_message",
  "chat_id": "{ticket_id}",
  "visitor": {
    "id": "{user_id}",
    "fields": {
      "name": "{user_name}"
    }
  },
  "message": {
    "kind": "visitor",
    "text": "{HDE_TRIGGER_PREFIX} {answer_last_without_html}"
  }
}
```

For the HDE dispatcher rule that sends a user's next reply to the bot:

```json
{
  "event": "new_message",
  "chat_id": "{ticket_id}",
  "visitor": {
    "id": "{user_id}",
    "fields": {
      "name": "{user_name}"
    }
  },
  "message": {
    "kind": "visitor",
    "text": "{answer_last_without_html}"
  }
}
```

The HDE adapter uses `chat_id` as the bot conversation id because replies must
be bound to the ticket. The trigger prefix is stripped before the message reaches
PII masking, RAG, or LLM.

HDE replies are sent through HelpDeskEddy API v2 as public ticket posts:

```http
POST /api/v2/tickets/{ticket_id}/posts/
Authorization: Basic <HDE_API_EMAIL:HDE_API_KEY>
Content-Type: application/x-www-form-urlencoded
```

Form fields:

```text
text=<bot answer>
user_id=<optional bot/user id>
```

Server `.env`:

```dotenv
HDE_BASE_URL=https://rosmolodezh.helpdeskeddy.com
HDE_API_EMAIL=<api-user-email>
HDE_API_KEY=<api-key>
HDE_BOT_USER_ID=
HDE_REQUEST_TIMEOUT_SECONDS=20
HDE_RATE_LIMIT_RPM=250
HDE_RATE_LIMIT_REMAINING_RESERVE=30
HDE_RATE_LIMIT_BAN_SECONDS=1200
```

Use `/posts/` for a public answer visible to the client. `/comments/` is for
internal staff comments and must not be used for normal bot replies.

The HDE webhook endpoint returns `{"ok": true}` immediately and processes the
RAG/LLM answer in the FastAPI background task. This prevents HDE/nginx timeouts
on slow CPU inference or Max-generation requests.

HelpDeskEddy applies a shared system-wide API limit. The standard HDE limit is
300 RPM for the whole account, including exports and unrelated scripts, not only
this bot. Keep the bot below the account limit (`HDE_RATE_LIMIT_RPM=250` by
default), watch `X-Rate-Limit` and `X-Rate-Limit-Remaining`, and pause outgoing
HDE sends when the remaining quota is low. Load and quality evals must run
locally through `/ask`; do not use HDE/VK as a load-test transport.

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
