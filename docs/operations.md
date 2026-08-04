# Operations Runbook

> **P0 recovery status, 16 July 2026:** the previous VM, IP, webhook, admin URL and all server
> artifacts are untrusted and offline. Do not run this runbook against the old host. A new runtime
> must be built from a clean vendor image and trusted Git checkout after a separate complete secret
> rotation. Do not copy old disks, images, volumes, `.env`, certificates, databases, backups or
> runtime files. See `docs/security_incident_20260715.md`.
>
> **Canonical recovery procedure, 20 July 2026:** use
> `docs/recovery_test_production_runbook_20260720.md` for the new HDE/VK test-production launch.
> It supersedes every legacy staging/bootstrap command in this document whenever the two differ.
> In particular, do not create a generic `.env`, do not use `:latest` application images and do
> not build after provider credentials have been placed on the host.

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
- Run live LLM evals only with `--max-llm-cost-rub`; routine runs use at most 10 cases.
  Larger/full runs additionally require a non-secret one-time `--high-cost-approval-id`.
  The ID must come from an external owner approval record for the exact runtime SHA, set,
  forecast and calculated stop-limit; it is non-secret but must never be invented for a command example.
  The persistent global `eval-cost-ledger-v1` reserves every live run before `/ask`, consumes
  approval IDs once, limits routine reservations to 300 RUB per rolling 24 hours and permits at
  most one full run per rolling 24 hours and release candidate. Missing/corrupt/unwritable ledger
  means STOP; it does not provide provider-billing data.
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
.venv\Scripts\python.exe -m eval.run_quality_suite --max-llm-cost-rub 100 --ask-max-cases 10
.venv\Scripts\python.exe -m eval.build_demo_quality_report --metrics reports\quality_suite\ask_eval.json --output reports\demo_quality.md
```

Run a transparent manual inspection against the ML app without semantic cache
or shared Redis session context:

```powershell
.venv\Scripts\python.exe scripts\manual_ask.py --file data\manual_complex_queries.json --target http://localhost:8001/ask --max-cases 10 --max-llm-cost-rub 30 --bypass-cache --isolate-users --output reports\manual_complex_inspection.json
```

The `app-ml` profile uses a longer local timeout than the lightweight app
because CPU rerank plus Max synthesis can exceed 90 seconds on cold runs.

Run the permanent clarification and long-dialog memory regression against the
live ML container. This scenario contains 19 turns, so it is not a routine `<=10` run and requires
a real one-time approval reference supplied from the external owner record. Clarification count
must never be the sole escalation reason:

```powershell
$HIGH_COST_APPROVAL_ID = $env:HIGH_COST_APPROVAL_ID
if ([string]::IsNullOrWhiteSpace($HIGH_COST_APPROVAL_ID)) {
  throw "STOP: obtain the one-time approval ID from the external owner record; do not invent it"
}
.venv\Scripts\python.exe -m eval.run_pre_pilot_quality_suite `
  --target http://127.0.0.1:8001/ask `
  --sections followup `
  --followup-cases eval\cases\dialog_memory_regression.json `
  --output-dir reports\dialog_memory_regression `
  --max-llm-cost-rub 20 `
  --high-cost-approval-id $HIGH_COST_APPROVAL_ID
```

After every live run, the budget owner manually reconciles `llm_estimated_cost_rub` with provider
billing for the exact UTC window and records run ID, runtime SHA, set/version, approval ID, both
amounts, percentage difference and verdict in private or external owner evidence. This is not
automated. Absolute variance above 10%, ambiguous attribution or missing final billing evidence
means STOP for further paid evals until pricing/attribution is fixed and newly approved.

## Local Human-Gold Quality Tooling

Эти команды выполняются только на trusted workstation. Они не подключаются к серверу и не
вызывают LLM:

```powershell
.venv\Scripts\python.exe scripts\manage_private_datasets.py inventory
.venv\Scripts\python.exe scripts\manage_private_datasets.py validate
.venv\Scripts\python.exe scripts\build_gold_ticket_dataset.py
```

Gold builder пишет selection, pending review queue и registry-entry только в
`data/private/eval/gold150_sanity_v2`. Он публикует новую версию атомарно; существующий version
directory нельзя перезаписать, вместо этого создаётся следующая immutable version. Завершение
review и freeze разрешены только после filesystem-backed проверки sealed GoldTicket JSONL,
selection membership, counts и hashes. Ticket-level файлы, registry и observations нельзя
копировать в `reports/`, staging Git, Docker build context или server runtime.

После human review safe stage report строится offline через `python -m eval.stage_funnel`.
Отсутствующее evidence получает `unscored`; legacy union не считается точной attribution, а
per-question source overlap остаётся coarse до явной claim binding. Multi-turn и неоднозначные
graded qrels не запускаются через legacy ask projection. Подробный контракт и команды:
`docs/human_gold_quality_workflow.md`.

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
passed explicitly. Terminal HDE rows are also excluded by default. They may be removed only with
an independently approved `--hde-terminal-ttl-days`: the command deletes `delivered` outbox first
and then its `processed` inbox, while unresolved/dead-letter rows never qualify.

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

Terminal HDE queue retention is a separate dry-run-first decision. It never deletes the recovery
audit and preserves every unresolved job:

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml \
  exec -T app-ml python scripts/purge_old_memory.py \
  --hde-terminal-ttl-days <approved-days>
# Only after data-owner approval and backup:
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml \
  exec -T app-ml python scripts/purge_old_memory.py \
  --hde-terminal-ttl-days <approved-days> --apply
```

`hde_transport_audit` is immutable at database level: revision `008` rejects `UPDATE` and
`DELETE`. It retains only pseudonymous job linkage, corporate operator id, fixed reason, evidence
digest and the pre-recovery error/attempt/dead-letter metadata. During incident recovery it is
held as security evidence; an audit-retention/deletion mechanism requires a separate data-owner
and security decision and is intentionally not part of the routine purge command.

The JSON result must show `mode=dry-run` during preview, the expected TTLs, and
only aggregate counts. Do not add a production scheduler during deployment;
schedule it as a separate, reviewed operations change with alerting and a
single-run lock.

## Yonote KB Refresh

Yonote is read-only for the bot. We never create, edit, or delete Yonote
documents from this project. The bot only reads selected collections and
updates its own normalized RAG seed.

The default production state remains disabled and needs no Yonote credential:

```env
YONOTE_SYNC_ENABLED=false
YONOTE_API_TOKEN=
```

An approved manual production preview uses:

```env
YONOTE_SYNC_ENABLED=true
YONOTE_SYNC_MODE=manual
YONOTE_API_TOKEN=<new-dedicated-read-only-token>
YONOTE_BASE_URL=https://rossmol.yonote.ru
YONOTE_COLLECTION_NAMES=Росмолодёжь: общее, структура, направления;Росмолодёжь: мероприятия
YONOTE_REQUEST_TIMEOUT_SECONDS=30
YONOTE_MAX_RETRIES=2
YONOTE_MIN_REQUEST_INTERVAL_SECONDS=0.15
```

The token is entered by a human from a password manager directly into the server-only
`.env.production`; it must not appear in a command line, chat, Git, rendered Squid config or logs.
Only `app-ml` receives it. `app` remains without the token or provider egress.

The client spaces read requests and retries temporary network, `429`, and
`5xx` failures with backoff. These settings protect Yonote from request bursts;
they do not grant write access or change any source document. Production always reads the full
configured collection set, fails closed if even one collection cannot be matched, and rejects
`limit_documents`. Concurrent pulls are rejected instead of running duplicate full scans.

The generated Squid allowlist contains the reviewed Cloud.ru and HDE hosts while Yonote is
disabled. When manual preview is enabled, it contains exactly one additional destination:
`rossmol.yonote.ru:443`. No credential is written to the proxy config.

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

Production admin panel flow:

1. Open `/admin/kb`.
2. Click `Yonote`.
3. Wait for the full pull of both configured collections.
4. Review or download `documents`, `fresh_yonote_records`, `added`, `changed` and `removed`.

The default production operation is preview-only. It computes a diff in memory and changes neither
Yonote, the tracked seed, Qdrant, semantic cache nor bot answers. `Apply to KB`, `Save` and
`Reindex` are hidden in the default read-only UI and remain blocked by backend `403`. The
downloaded report can contain internal KB text; keep it only as private evidence, never in Git,
chat or public logs.

Publishing reviewed Yonote changes is a separate release-engineering operation: preserve the
reviewed snapshot, create a versioned seed change in Git, review its diff, run validation and
regression, build a clean candidate, perform a controlled full index with rollback evidence, clear
cache, restart the runtime and repeat readiness/security/smoke gates. Never mutate the trusted
production checkout from a one-click admin action.

The limited test-production editor is a separate explicit capability, not the default. It requires:

```env
ADMIN_READ_ONLY=false
ADMIN_MUTATIONS_ENABLED=true
ADMIN_KB_SEED_PATH=/app/data/private/admin-kb/knowledge_base_seed.json
```

Before enabling it, create the server-only working file from the exact deployed tracked seed,
verify equal SHA-256, owner `10001:10001` and mode `0600`, and keep a private backup. The directory
is writable only in `app-ml`; `app` sees it read-only. Never point writable admin at
`/app/data/knowledge_base_seed.json` or at the Git checkout.

In this mode Save and per-chunk Reindex may be used for a deliberate test. Yonote Apply writes only
the private working seed and never calls a Yonote write endpoint. It does not automatically run a
full index. Keep HDE off and do not Apply until the Preview diff is reviewed and the operator is
ready to execute the server-controlled full indexing gate. The public admin must not expose a raw
SQL or Qdrant console.

After an Apply that changes the published count, both runtime processes must be restarted after
the controlled full index so their seed manifests match Qdrant. Full indexing remains a server
operation with pre-backup/hash, `--prune-stale`, semantic-cache clear, readiness/security checks and
RAG smoke. The working copy is test evidence; it is not promoted to the canonical Git seed without
separate content review, regression and a versioned commit.

The local/disposable Apply flow below remains useful for development after explicit content review.

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

The clean runtime currently exposes the provisional HTTPS admin route
`https://bot-135-106-167-124.sslip.io/admin/kb`. Its certificate and route belong to the new clean
host. Manual login, list and read checks passed; a write attempt was correctly rejected by backend
`403`, but revealed that the running UI still displayed writable controls. The local candidate
fixes that UX contract; the fix is not active until the exact candidate is reviewed and deployed.
A permanent corporate subdomain is pending. The former public address, its certificate, SSH tunnel
and admin token belong to the compromised host and must not be used.

On the new clean VM:

1. provision a new HTTPS endpoint with a new certificate and new admin token;
2. keep plaintext admin login/API disabled and rate-limit login attempts;
3. verify `Secure`, `HttpOnly`, `SameSite=Lax` session cookies and security headers;
4. store certificate state only on the new host and verify automatic renewal;
5. record a temporary route as provisional; publish the permanent corporate team URL only after
   external HTTPS, `/ready` and manual UI checks;
6. use the admin in read-only mode during a new holdout: search/view, `Validate`, ops/quality
   reports and `Yonote Preview`; disable the explicit test-editor capability again before the
   sealed cohort.

Enabling Yonote preview or the test editor is a production configuration change. Before it,
disable the HDE dispatchers and prove the durable queues are empty. Render a new Squid config to a separate
candidate path (the generator intentionally refuses overwrite), parse it with the pinned Squid
image, compare the exact three hostnames, and only then install it atomically. Recreate only
`runtime-egress-proxy` and the explicitly affected runtime services; do not reindex Qdrant merely
to enable Preview. In default read-only mode verify `Apply=403`. In test-editor mode verify the
isolated working path/mount directions and explicit capability flags without printing secrets.
Always verify `/ready`, exact egress allow/deny, unchanged tracked-seed hash and expected Qdrant
counts before reenabling the test dispatchers.

Do not reuse any ACME directory, TLS private key, `.env` or tunnel command from the old host.

The provisioning script runs only as root, validates the new endpoint and reads its values from
the protected `.env.production` when no shell override is supplied. Run it only on the clean VM
after the new DNS name or IPv4 address is approved; do not place real values in shell history:

```bash
sudo env -u ADMIN_PUBLIC_HOST -u CERTBOT_EMAIL bash scripts/provision_admin_https.sh
```

## Server Staging Deploy

> **Superseded for incident recovery.** Do not execute the legacy commands in this section for the
> 20 July clean rebuild. They are retained only as historical operational context. The approved,
> fail-closed sequence (secretless pinned build first, then `.env.production`, production Compose,
> TLS, acceptance and limited HDE smoke) is in
> `docs/recovery_test_production_runbook_20260720.md`.

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

Create both dispatcher rules in the disabled state first. Use:

- method `POST`;
- URL `https://<approved-public-host>/webhook/hde`;
- JSON request format;
- Bearer authorization with the same server-only value as `WEBHOOK_AUTH_TOKEN`.

HelpDeskEddy documents Bearer Token as a supported dispatcher webhook authorization method:
<https://support.helpdeskeddy.com/ru/knowledge_base/art/127/cat/57/>. The application also accepts
the equivalent `X-Webhook-Secret` header, but the token must never be placed in the URL or JSON
body. Scope both rules to the single test department/channel and require the last-answer author
to be the client; otherwise the bot's own public HDE post can trigger a loop. Keep the legacy bot
and broad dispatcher rules disabled.

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

Do not put `HDE_TRIGGER_PREFIX` into the second payload. It is only the start marker for the
new-ticket rule, not webhook authentication. Both rules use Bearer authentication independently
of the payload.

### Visual HDE/VK smoke record, 23 July 2026

The limited test-channel smoke visually showed one public bot reply for the grounded inbound.
Its follow-up was delivered, but the sourced answer to the date question about «Правда» omitted
the exact date; this is a P1 answer-completeness backlog and is not fixed during channel
acceptance. Two separate new tickets behaved as required:

- `Позови оператора` produced exactly `Передаю обращение специалисту.`;
- `Какая погода завтра в Москве?` produced exactly one scope-note and did not escalate.

This is UI evidence, not final transport acceptance. Before handoff, capture the final aggregate
from PostgreSQL for all smoke events/traces and inbox/outbox/dead-letter state, finish the
traffic/security review for unexpected events or loops, and manually verify login and read-only
functions at the temporary admin route.

The HDE adapter uses `chat_id` as the bot conversation id because replies must
be bound to the ticket. The trigger prefix is stripped before the message reaches
PII masking, RAG, or LLM.

`message.id` must be mapped to HDE dispatcher tag `{last_post_id}`, the stable identifier of the
source HDE post, not to the ticket id or a generated timestamp. The tag is documented in the
[official HDE system tag list](https://support.helpdeskeddy.com/ru/knowledge_base/article/372/category/56/)
as the ID of the last answer. The adapter also accepts `message.message_id`,
`message.post_id`, `data.message.id`, `event.id`, root `event_id`, `message_id` and `post_id`.
Repeated delivery of the same stable id for one ticket is acknowledged with HTTP 200 but does
not generate or send a second answer. A stable id is mandatory: payloads without it receive 422
and must not be enabled in the dispatcher. Before handoff, replay one identical test payload and
prove that the PostgreSQL inbox contains one event and HDE contains one bot answer.

Migration `008_hde_durable_transport` adds a PostgreSQL inbox/outbox. The webhook masks message
text immediately, encrypts the reversible ticket reference with pgcrypto and returns HTTP 200
only after the inbox transaction commits. A worker runs only in the `ml` runtime, processes one
event per ticket in order, verifies the persisted `request_traces.response_text`, then creates the
encrypted outbox atomically. Confirmed delivery purges both reversible envelopes and the masked
inbox payload and atomically updates `request_traces.delivery_status`. Raw HDE identifiers, raw
message text and response text are not stored in queue columns as plaintext.

HDE does not provide a confirmed idempotency key for the public-post request. Therefore an
attempted timeout, network error, unexpected exception or non-429 HTTP error is ambiguous and is
quarantined as `dead_letter`; it is never sent automatically a second time. Automatic retry is
allowed only when no provider call was attempted or HDE explicitly returned 429. `/ready` fails
on a dead-letter, stopped worker or stale queue. Disable the dispatcher, reconcile the ticket in
the HDE UI, then explicitly mark the outbox delivered or requeue it. This is an honest at-least-once
boundary: an incorrect manual requeue after HDE accepted an unconfirmed request can duplicate a
public answer.

Dead-letter recovery is available only through the server-local audited CLI; there is no public
or admin recovery endpoint. First disable the dispatcher and inspect privacy-safe metadata:

```bash
dc=(sudo docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.ml.yml -f docker-compose.prod.yml)
"${dc[@]}" --profile ml exec -T app-ml python scripts/hde_transport_admin.py list
```

The operator reviews the corresponding HDE ticket/posts without copying them to Git or chat,
stores evidence under `data/private/runtime/`, and calculates its SHA-256. The mutation requires
an operator id, one fixed reason code, evidence digest and a repeated job id. Choose exactly one:

```bash
# HDE confirms that the public post exists:
"${dc[@]}" --profile ml exec -T app-ml python scripts/hde_transport_admin.py \
  reconcile-delivered --job-id <ID> --confirm-job-id <ID> \
  --operator <CORPORATE_LOGIN> --reason provider_confirmed_delivered \
  --evidence-sha256 <64_HEX_DIGEST> --http-status 200

# HDE confirms that no public post was accepted:
"${dc[@]}" --profile ml exec -T app-ml python scripts/hde_transport_admin.py \
  requeue-outbox --job-id <ID> --confirm-job-id <ID> \
  --operator <CORPORATE_LOGIN> --reason provider_confirmed_not_delivered \
  --evidence-sha256 <64_HEX_DIGEST>

# Inbox processing side effects were reviewed and safe resume is proven:
"${dc[@]}" --profile ml exec -T app-ml python scripts/hde_transport_admin.py \
  requeue-inbox --job-id <ID> --confirm-job-id <ID> \
  --operator <CORPORATE_LOGIN> --reason side_effects_reviewed_safe_to_resume \
  --evidence-sha256 <64_HEX_DIGEST>

"${dc[@]}" --profile ml exec -T app-ml python scripts/hde_transport_admin.py audit --job-id <ID>
```

Every mutation and its reason/evidence digest are committed atomically to
`hde_transport_audit`; the row also preserves the original attempt count, error code,
dead-letter timestamp and previous delivery HTTP status before recovery clears queue diagnostics.
The database rejects audit `UPDATE`/`DELETE`. Trace retention excludes unresolved HDE inbox/outbox rows, so the trace
needed for safe reconciliation cannot be removed by `purge_old_memory.py`. Re-enable the
dispatcher only after the affected queue is empty, `/ready` is green and the audit row is present.

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
HDE_TRANSPORT_ENABLED=true
HDE_TRANSPORT_EVENT_KEY_SECRET=<independent-random-secret>
HDE_TRANSPORT_ENCRYPTION_KEY=<different-independent-random-secret>
HDE_TRANSPORT_LEASE_TIMEOUT_SECONDS=420
HDE_TRANSPORT_RECOVERY_INTERVAL_SECONDS=30
HDE_TRANSPORT_SHUTDOWN_TIMEOUT_SECONDS=420
HDE_TRANSPORT_QUEUE_STALE_AFTER_SECONDS=900
HDE_REQUEST_TIMEOUT_SECONDS=20
HDE_RATE_LIMIT_RPM=250
HDE_RATE_LIMIT_REMAINING_RESERVE=30
HDE_RATE_LIMIT_BAN_SECONDS=1200
```

Use `/posts/` for a public answer visible to the client. `/comments/` is for
internal staff comments and must not be used for normal bot replies.

The HDE webhook endpoint returns `{"ok": true}` only after durable inbox commit. RAG/LLM and
delivery run asynchronously in the persistent worker; a process restart cannot erase an
acknowledged inbox row. Before rotating either transport secret, the inbox/outbox and dead-letter
counts must be zero. Never change the encryption key while an encrypted row remains.

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
