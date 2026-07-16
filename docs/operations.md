# Operations Runbook

> **P0 recovery status, 16 July 2026:** the previous VM, IP, webhook, admin URL and all server
> artifacts are untrusted and offline. Do not run this runbook against the old host. A new runtime
> must be built from a clean vendor image and trusted Git checkout after a separate complete secret
> rotation. Do not copy old disks, images, volumes, `.env`, certificates, databases, backups or
> runtime files. See `docs/security_incident_20260715.md`.

## Local Safety Rules

- Do not commit `.env`, raw HDE/ticket exports, API keys, tokens, passwords, or server dumps.
- Keep private ticket datasets only under `data/private/`; this path is ignored by Git.
- Keep local XLSX/PDF source materials under `data/private/source_materials/`, never in the
  repository root. `scripts/build_kb_seed.py` uses that private path by default.
- Application containers mount `data/` so the admin flow can atomically replace the versioned
  seed. A nested `data/private/runtime/` mount masks the host `data/private/` tree inside the
  container: raw source materials, ticket exports and operator datasets remain inaccessible.
  The one-shot indexer receives only `data/knowledge_base_seed.json` read-only.
- Set a stable, dedicated `USER_HASH_SECRET` in production. User and ticket identifiers are then
  pseudonymized with HMAC-SHA256. Rotating this secret intentionally starts a new pseudonym space;
  existing Redis sessions expire normally and old memory rows are removed by the retention job.
  Startup fails outside `local`/`test` when the variable is empty; operational API/webhook/admin
  tokens are intentionally not reused as the pseudonymization key.
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

Run the permanent clarification and long-dialog memory regression against the
live ML container. Clarification count must never be the sole escalation reason:

```powershell
.venv\Scripts\python.exe -m eval.run_pre_pilot_quality_suite `
  --target http://127.0.0.1:8001/ask `
  --sections followup `
  --followup-cases eval\cases\dialog_memory_regression.json `
  --output-dir reports\dialog_memory_regression `
  --max-llm-cost-rub 20
```

Interpret first-turn conversion and multi-turn resolution separately. A
clarification keeps the user in the bot flow but does not count as a closed
ticket until a later turn produces a grounded answer.

## Conversation Memory

Migration `006_conversation_memory` adds `user_memory.structured_context` and
the append-only `conversation_turns` table. Only PII-masked user text is stored.
Redis keeps the latest 20 `user/bot` pairs; PostgreSQL restores that recent
window and the rolling summary after Redis TTL expiration.

Verify the migration:

```powershell
docker compose exec -T app alembic current
docker compose exec -T postgres psql -U rosmol -d rosmol_ai_bot -P pager=off -c "select count(*) from conversation_turns;"
```

The production retention period for `conversation_turns`, `user_memory`, and
`request_traces` must be approved before enabling scheduled cleanup. No cron or
systemd timer is installed by this repository.

The retention command is read-only by default. It applies `MEMORY_TTL_DAYS` to
both conversation tables so the long-term summary and its stored turns expire
together. `request_traces` are excluded unless a separately approved TTL is
passed explicitly.

Preview the eligible row counts without exposing stored text:

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml \
  exec -T app-ml python scripts/purge_old_memory.py
```

After the data owner approves the TTL and a PostgreSQL backup has completed,
apply memory retention and immediately repeat the dry run:

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml \
  exec -T app-ml python scripts/purge_old_memory.py --apply
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml \
  exec -T app-ml python scripts/purge_old_memory.py
```

`request_traces` require a separate approved value. Preview first, back up the
database, then repeat the same command with `--apply` only after approval:

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml \
  exec -T app-ml python scripts/purge_old_memory.py \
  --request-trace-ttl-days <approved-days>
```

The JSON result must show `mode=dry-run` during preview, the expected TTLs, and
only aggregate counts. Do not add a production scheduler during deployment;
schedule it as a separate, reviewed operations change with alerting and a
single-run lock.

## Yonote KB Refresh

Yonote is read-only for the bot. We never create, edit, or delete Yonote
documents from this project. The bot only reads selected collections and
updates its own normalized RAG seed.

Required local/server `.env` values:

```env
YONOTE_API_TOKEN=<read-only-token>
YONOTE_BASE_URL=https://rossmol.yonote.ru
YONOTE_COLLECTION_NAMES=Росмолодёжь: общее, структура, направления;Росмолодёжь: мероприятия
YONOTE_REQUEST_TIMEOUT_SECONDS=30
YONOTE_MAX_RETRIES=2
YONOTE_MIN_REQUEST_INTERVAL_SECONDS=0.15
YONOTE_SYNC_ENABLED=false
YONOTE_SYNC_MODE=manual
```

The client spaces read requests and retries temporary network, `429`, and
`5xx` failures with backoff. These settings protect Yonote from request bursts;
they do not grant write access or change any source document.

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

Admin panel flow:

> Recovery freeze override: no trusted admin panel currently exists. Do not open the old URL or
> use old credentials. The steps below become available only after clean rebuild, a new HTTPS
> handoff and an explicitly approved batch quality change; until then stop before any mutation.

1. Open `/admin/kb`.
2. Click `Yonote`.
3. Review `documents`, `fresh_yonote_records`, `added`, `changed`, `removed`.
4. If the preview looks correct, click `Apply to KB`.
5. The button only updates this project's `data/knowledge_base_seed.json`; it never writes to Yonote.
6. After applying, run full Qdrant indexing before relying on the updated answers.

Rebuild the local Docker services and reindex Qdrant:

```powershell
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml up -d --build app app-ml nginx
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml run --rm index-kb `
  python scripts/index_kb.py --path data/knowledge_base_seed.json `
    --forums-registry data/forums_registry.json --embedding-batch-size 32 --prune-stale
```

The indexer validates the registry, selects only `status=published`, removes stale points when
explicitly requested, and clears all semantic responses after a successful KB mutation. Restart
`app`/`app-ml` after a live full index so process-local keyword snapshots cannot survive the
release. Then run smoke checks against `http://127.0.0.1:8001/ask` with `X-Bypass-Cache: true`.

## Secure Admin Access

There is currently no trusted shared admin URL. The former address
`https://139.100.225.44/admin/kb`, its certificate, SSH tunnel and admin token belong to the
compromised host and must not be used.

On the new clean VM:

1. provision a new HTTPS endpoint with a new certificate and new admin token;
2. keep plaintext admin login/API disabled and rate-limit login attempts;
3. verify `Secure`, `HttpOnly`, `SameSite=Lax` session cookies and security headers;
4. store certificate state only on the new host and verify automatic renewal;
5. publish the new team URL in `CURRENT_STATE.md` only after external HTTPS and `/ready` checks;
6. use the admin in read-only mode during a new holdout: search/view, `Validate`, ops/quality
   reports and `Yonote Preview`; do not use `Save`, `Reindex` or `Apply to KB`.

Do not reuse any ACME directory, TLS private key, `.env` or tunnel command from the old host.

## Server Staging Deploy

Current staging target does not yet exist. Create a new Ubuntu 24.04 VM from the provider's clean
image; never clone or attach the old VM/disk. The application container is bound to localhost
only; public traffic must go through Nginx.

Before application bootstrap, configure the provider security group: allow 22/tcp only from a
trusted admin IP/VPN and expose only 80/443 publicly. Install and verify Docker Engine plus the
Compose plugin from the trusted official repository; a clean vendor image must not be assumed to
contain Docker. Then apply the same restrictions in UFW. Exact bootstrap commands belong to the
separate reviewed infrastructure task for the selected provider/image.

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
ssh-keygen -t ed25519 -C "rosmol-ai-bot-clean-deploy" -f /root/.ssh/rosmol_ai_bot_deploy -N ""
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

Fill a brand-new `.env` manually on the new server. Do not copy the old file. Minimum staging
overrides:

```dotenv
APP_ENV=staging
NGINX_BIND=0.0.0.0
NGINX_HOST_PORT=80
NGINX_TLS_BIND=0.0.0.0
NGINX_TLS_HOST_PORT=443
INSTALL_ML=false
```

Also set real secrets and strong database credentials in `.env`: Cloud.ru API
key, API/webhook/admin tokens, `POSTGRES_PASSWORD`, and matching
`POSTGRES_DSN`. Every value potentially present on the old host must have been rotated first.
`USER_HASH_SECRET` is mandatory outside local/test and must be a new separate random value; do
not reuse the old value or an operational token and do not print it in logs.

Start and verify:

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1/ready
```

Open only required ports:

```bash
ufw allow from <TRUSTED_ADMIN_IP_OR_VPN_CIDR> to any port 22 proto tcp
ufw allow 80/tcp
ufw allow 443/tcp
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
    "id": "{last_post_id}",
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
    "id": "{last_post_id}",
    "kind": "visitor",
    "text": "{answer_last_without_html}"
  }
}
```

The HDE adapter uses `chat_id` as the bot conversation id because replies must
be bound to the ticket. The trigger prefix is stripped before the message reaches
PII masking, RAG, or LLM.

`message.id` must be mapped to HDE dispatcher tag `{last_post_id}`, the stable identifier of the
source HDE post, not to the ticket id or a generated timestamp. The tag is documented in the
[official HDE system tag list](https://support.helpdeskeddy.com/ru/knowledge_base/article/372/category/56/)
as the ID of the last answer. The adapter also accepts `message.message_id`,
`message.post_id`, `data.message.id`, `event.id`, root `event_id`, `message_id` and `post_id`.
Repeated delivery of the same stable id for one ticket is acknowledged with HTTP 200 but does
not generate or send a second answer. Payloads without a stable id remain accepted for backward
compatibility, but use a per-request fallback and therefore cannot be deduplicated reliably.

HDE turns are protected by distributed Redis locks: one ticket is processed and delivered in
sequence, while different tickets may run concurrently. Redis inbox keys are retained for seven
days. A stable event first receives a short `processing` lease; only confirmed HDE delivery turns
it into `done`. A known non-delivery releases the lease so upstream retry can try again. Redis
failure or a still-active processing collision returns HTTP 503, so the dispatcher must retry;
an already completed duplicate returns HTTP 200 without a second answer. This avoids concurrent
or duplicate public replies, but the current in-process `BackgroundTasks` implementation is not
a durable outbox: a process restart after HTTP acknowledgement can still lose an accepted turn
until the processing lease expires and upstream retries. Do not describe this residual risk as
closed until a persistent worker/outbox with retry is deployed.

If an HDE rule is dedicated to one known event, it may pass an explicit optional
context at the root of either payload:

```json
{
  "forum_context": "День молодёжи"
}
```

Only names and aliases present in `data/forums_registry.json` are accepted. The
field is useful when the user's first message is context-free, for example
`Где мой билет?`. Do not infer this field from a general MAX/VK channel: configure
it only on a dispatcher rule that is unambiguously bound to one event. Existing
payloads without `forum_context` remain compatible.

HDE replies are sent through HelpDeskEddy API v2 as public ticket posts:

```http
POST /api/v2/tickets/{ticket_id}/posts/
Authorization: Basic <HDE_API_EMAIL:HDE_API_KEY>
Content-Type: application/x-www-form-urlencoded
```

Migration `007_hde_delivery_telemetry` adds hashed ticket linkage, upstream/eval identifiers,
turn outcome and typed delivery telemetry to `request_traces`. The admin ops report exposes HDE
telemetry coverage, delivery success, outcome and delivery-status counts. `delivered`,
`rate_limited`, `timeout`, `not_configured`, `network_error`, `http_error` and
`ordering_failed` are distinct outcomes; a generated response is not considered delivered until
the trace has `delivery_status=delivered`.

For HDE, one `chat_id`/`ticket_id` is one product ticket. The ops report groups all turns by its
HMAC pseudonym and reports `bot_resolved_first_turn`, `bot_resolved_multi_turn`,
`operator_required`, `unresolved_clarification`, `not_delivered`, `delivery_unknown`, `error` or
`unresolved`. The primary conversion is the share of tickets whose latest turn is a delivered
answer and which never escalated to an operator; a clarification followed by a delivered answer
therefore counts as multi-turn resolution, not as an unresolved ticket.

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

These commands are for future backups created on a verified clean runtime. The dump made on the
old compromised VM is untrusted and must not be restored into the new runtime.

Create a local dump from Docker:

```powershell
docker compose exec postgres pg_dump -U rosmol -d rosmol_ai_bot -Fc -f /tmp/rosmol_ai_bot.dump
docker compose cp postgres:/tmp/rosmol_ai_bot.dump .\backups\rosmol_ai_bot.dump
```

Restore is intentionally omitted from the active recovery runbook. A future verified backup may
be restored only into a newly created empty/disposable target after checking the absolute project
path, database name, checksum and maintenance window. Never run `--clean` against an unidentified
or active production database.

## Qdrant Backup

These commands are for future snapshots of a verified clean runtime. Do not restore the old
Qdrant snapshot; rebuild the frozen baseline collection from the trusted versioned Git seed.

For local Docker volumes, prefer filesystem-level snapshots while Qdrant is stopped:

```powershell
docker compose stop qdrant
docker run --rm -v rosmol-ai-bot_qdrant_storage:/qdrant/storage -v ${PWD}\backups:/backup alpine tar czf /backup/qdrant_storage.tgz -C /qdrant/storage .
docker compose start qdrant
```

Restore is intentionally omitted from the active recovery runbook. A future trusted snapshot must
be restored into a new named volume/collection and validated before traffic is switched. Never
delete or overwrite the active Qdrant volume as part of an ad-hoc rollback.

## Runtime Checks

- `/health` checks the FastAPI process.
- `/ready` checks Redis, PostgreSQL and Qdrant.
- Request traces in PostgreSQL are the source of truth for model choice, source chunks, escalation reason, LLM usage and cost.
- If RAG/ML dependencies are unavailable, the API must return controlled escalation rather than an unhandled error.
