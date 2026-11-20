# Deploying the Inbox API

This is a runbook for the processes this repository actually starts. It is not a provisioned host, and it is not a production sign-off.

Local development stays `docker compose up` with `.env` copied from `.env.example`. That stack binds the API on port 8000, Postgres on host port 5433, and Redis on host port 6380. Those addresses are not a public origin.

The public API hostname is not in this repository. Write it as `<API_ORIGIN>` (scheme and host, no path) until a host assigns one. The browser origin for this product is exactly `https://redocu.app`. Where the DNS for that name is managed is not established here.

## What has to be running

| Piece | Why | Durable? |
| --- | --- | --- |
| API process | Uvicorn serving `app.main:app` on HTTP inside the container. TLS is terminated in front of it. | The process can restart. Login JWTs are signed with `SECRET_KEY` and checked against Postgres. |
| Worker process | `arq app.worker.settings.WorkerSettings`. Sync, form jobs, a mailbox sync every 5 minutes, and the 00:00 and 12:00 subscription cron run here. On the free plan this process shares the web service, which sleeps after inactivity, so the 5-minute cron runs only while the service is awake. | Same code image as the API. It must see the same database, Redis, and file directory. |
| Postgres | Users, mail rows, encrypted Microsoft tokens, revoked JWT ids. The first migration runs `CREATE EXTENSION vector`. Health stays degraded until that extension exists. | Required for anything you need after a restart. |
| Redis | OAuth `state` (10 minutes) and the ARQ queue. | Not required for login or logout. Required to start Outlook connect and to run a sync. |
| File directory | `STORAGE_LOCAL_PATH`, default `/app/storage`. PDF templates are written here. | Only if the disk survives restarts. |

`GET /health` checks the API, database, pgvector, Redis, and that the storage directory is writable. It returns 503 when any check is not `ok`. It is not a debug route. There is no `/debug` router in this repo.

`POST /auth/logout` writes the token id to the Postgres table `revoked_tokens`. A Redis flush does not sign that user back in, and it does not delete the row. Login itself does not read Redis. What Redis does lose:

- An Outlook connect that has not finished. The callback comes back as `invalid_state`.
- Queued and running `sync_mailbox_delta` jobs. `sync.status` can stay `pending` or `running` until the 10-minute stale window allows another `POST /messages/sync`.
- The worker cannot run while Redis is down. `POST /messages/sync` returns 503 `Mailbox sync could not be queued.` Connect start returns 503 `Outlook connect is temporarily unavailable.`

Do not describe the queue or an in-flight OAuth state as durable.

## Normal layout: two processes

Use this whenever the host can run two processes. `docker-compose.deploy.yml` builds the same image twice. It does not start Postgres or Redis, and it does not contain passwords.

```bash
cp deploy.env.example deploy.env
# fill deploy.env outside git
docker compose -f docker-compose.deploy.yml --profile migrate run --rm migrate
docker compose -f docker-compose.deploy.yml up -d api worker
```

`migrate` runs `python -m app.deploy.migrate_once`, which takes a session-level Postgres advisory lock and then `alembic upgrade head`. The API and the worker do not migrate when they boot. Do not point both of them at a migration command. A second migrate waits on the lock instead of applying the same revision twice.

The image's default command is only the API, without `--reload`:

```text
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The worker command is:

```text
python -m arq app.worker.settings.WorkerSettings
```

Inside this compose file both listen assumptions use port 8000 on the API container. The public HTTPS proxy forwards to that port. The application does not read `X-Forwarded-*` and does not redirect HTTP to HTTPS. `GRAPH_REDIRECT_URI` is the public `https://<API_ORIGIN>/auth/microsoft/callback`, not the container's private name.

`APP_ENV=production` refuses to start when any of these are true:

- `SECRET_KEY` is missing, shorter than 32 characters, or one of the example placeholders
- `TOKEN_ENCRYPTION_KEY` is missing or not a Fernet key
- `DATABASE_URL` is not `postgresql+psycopg://`, uses the password `inbox`, has no password, uses a host containing `-pooler`, sets `pgbouncer=true`, or omits `sslmode=require` (also accepted: `verify-ca`, `verify-full`)
- `REDIS_URL` is not `redis://` or `rediss://`, or has no password
- `FRONTEND_URL` is not https, or `CORS_ORIGINS` is not an https list that includes that exact origin
- `CORS_ORIGINS` contains `*`
- `GRAPH_REDIRECT_URI` is not `https://<host>/auth/microsoft/callback`
- `GRAPH_WEBHOOK_CLIENT_STATE` is shorter than 16 characters

`SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, and `SLACK_REDIRECT_URI` are not required to boot. Slack connect returns 503 until they are set. When `SLACK_REDIRECT_URI` is set, it must be `https://<host>/auth/slack/callback` with no query or userinfo. The production value for this API is `https://redocu-api.onrender.com/auth/slack/callback`. `SLACK_SEND_ENABLED` defaults to on. Set it to `0` to refuse Slack sends without stopping sync. These values stay on the API and the worker. They are not frontend build variables, and this repository does not change them on the host.

`GRAPH_CLIENT_ID` and `GRAPH_CLIENT_SECRET` are not required for the process to boot. Outlook connect will fail until they are set. They stay on the API and the worker. They are not frontend build variables. No Entra app has been verified from this repository.

`EMAIL_PROVIDER` is `resend` or `disabled` in production. `fake` and `console` refuse to boot. Resend also needs `EMAIL_API_KEY` and `EMAIL_FROM`. Password-reset logout is not the same as `POST /auth/logout`: a reset bumps `session_version` in Postgres and rejects every older access token. The reset code's hash is in Postgres. The code itself is not. Redis still only holds the rate-limit counters and the job ids.

`rediss://` is the TLS Redis scheme. ARQ's `RedisSettings.from_dsn` sets TLS when it sees that scheme. `redis://` is plain TCP, which is only appropriate on a private network. This repository does not open either URL during tests.

Use the Postgres provider's direct host with TLS and the SQLAlchemy driver scheme:

```text
postgresql+psycopg://USER:PASSWORD@DIRECT_HOST/DB?sslmode=require
```

Keep extra query parameters the provider added, including `channel_binding` when it is present. Do not use the pooled host for this app. Alembic and the migration lock need a direct session. That URL shape was checked with the SQLAlchemy parser only. No connection to a hosted database was opened.

## Optional one-container demo

Use this only when the host will run a single web container and will not run a second worker. It is not the default command.

```bash
python -m app.deploy.demo
```

The command honors `PORT` (default `8000`). Unless `RUN_MIGRATIONS=0`, it runs the locked upgrade once, then starts Uvicorn and ARQ under a small supervisor. The supervisor is not the API lifespan. On SIGTERM or SIGINT it signals both children and reaps them. If either child exits on its own, it stops the other and the process exits non-zero so the host can restart it.

Run one copy. A second copy would migrate under the same lock, which is safe, and would also run a second worker, which you do not want on a single small database.

When that worker starts, it rescores stored mail for every connected mailbox (Outlook and Gmail). Disconnected mailboxes are left alone. The pass does not call Graph or Gmail, write the mailbox, or call an LLM. `RESCORE_ON_STARTUP` defaults to on. Set it to `0` to skip. A failure is logged and the worker still starts.

After a deploy, the worker log has one summary line:

```text
priority rescore on startup finished accounts=<n> messages=<n>
```

`accounts` is connected mailboxes that finished. `messages` is stored messages on those mailboxes, including messages that already had a score. A skipped run logs `priority rescore on startup skipped`.

## Free-demo candidate, not a production deployment

A no-card combination that matches the current processes, with the limits that come with those free tiers:

- One free web service that runs `python -m app.deploy.demo`, so the API and the worker share the only process slot. A free web tier that has no background-worker product cannot run `docker-compose.deploy.yml`'s separate worker.
- Postgres from a free database that does not expire the data after a trial window, with the `vector` extension and a direct TLS URL. Do not use a database product that deletes the instance after 30 days.
- Redis from a free key-value service on a private network in the same region as the web service, with a password in `REDIS_URL`.

Treat that combination as a demo:

- The web process sleeps after a short idle period and cold-starts on the next request. The worker sleeps with it. Mail does not sync while it is asleep. The 00:00 and 12:00 cron does not fire while it is asleep. Do not add a ping job to keep it awake.
- The free web disk is ephemeral. PDF templates in `/app/storage` disappear on restart, redeploy, or a move to a new instance. Logout rows, users, and messages survive only if Postgres does. This repo does not add object storage in this change.
- The free key-value store that keeps data only in memory drops the queue and OAuth states on restart. Logout still holds, because it is in Postgres. Sync jobs do not.
- The free web service provides the HTTPS certificate for `https://<name>.onrender.com`. That name can be `<API_ORIGIN>` until someone chooses a custom domain. Nothing in this repo registers that name or proves that `redocu.app` DNS is already delegated anywhere.
- `FRONTEND_URL` and `CORS_ORIGINS` stay `https://redocu.app` even when the API host is an `onrender.com` name.
- One web service is the free shape. Do not add a second service, a paid instance type, a blueprint that selects a paid plan, or an automatic upgrade.

A separate always-on API and worker, with durable Postgres, durable Redis, and a disk for `/app/storage`, is the layout in `docker-compose.deploy.yml`. That layout is not what the free web tier includes. Neither layout has been started against a hosted account from this repository.

## Migration and rollback

Current head is `b7d3e9a41c20`.

Apply:

```bash
python -m app.deploy.migrate_once
```

Or, with the deploy compose file:

```bash
docker compose -f docker-compose.deploy.yml --profile migrate run --rm migrate
```

The lock id is fixed in `app.deploy.migrate`. It is not a secret. The connection that holds it stays open until Alembic returns, then the lock is released. Closing the connection also releases it.

Roll back the image before you roll back the schema. Columns added by `b7d3e9a41c20` are ignored by an older build that does not select them. Downgrade that revision anyway only from a backup: it drops the new message and sync columns, and it writes an empty string into null access tokens so the old NOT NULL column can be restored. Prefer restoring the database backup over `alembic downgrade`.

```bash
alembic current
alembic downgrade b7d3e9a41c20
alembic upgrade head
```

`alembic downgrade b7d3e9a41c20` moves from head to the previous revision. Read `alembic history` before you type it.

## Verify the 30-day deletion window

Redocu's backend runs on Render and its frontend runs on Cloudflare Workers. Record the production settings and capture date before confirming the window. Include every copy that can contain data covered by the commitment.

- **Render backend.** Capture the production workspace plan, the API and worker storage configuration, and any workspace log stream or service override. If PDFs and attachments use a persistent disk, check that the storage path is under its mount. Render publishes 7 days for Hobby service logs, 14 for Pro, and 30 for Scale and Enterprise. Persistent-disk snapshots are available for 7 days after creation. Check the retention of each external log destination separately. [Service logs](https://render.com/docs/logging#retention-period) · [Disk snapshots](https://render.com/docs/disks#disk-snapshots) · [Log streams](https://render.com/docs/log-streams)
- **Inbox database.** Identify the PostgreSQL provider and resource from the backend's production configuration. Record the provider and resource name and keep the connection string private. Capture the actual recovery window and inventory backups, restored instances, and copies for previews or testing. For managed Render Postgres, use the database's **Recovery** page. [Render Postgres recovery](https://render.com/docs/postgresql-backups)
- **Cloudflare frontend.** The `ai-inbox` Worker's repository configuration enables observability. Check the deployed Worker's **Observability** settings and any OpenTelemetry exports, Logpush jobs, or Tail Workers, including destination retention. Track D1 recovery and exports for the production and preview waitlist databases separately from the inbox database. [Workers Logs](https://developers.cloudflare.com/workers/observability/logs/workers-logs/) · [D1 recovery](https://developers.cloudflare.com/d1/reference/time-travel/)
- **Exports and other copies.** Record where database exports, copied disks, and log exports are stored and how long each destination keeps them. Verify lifecycle settings at the destination. Check which data is present in any additional log types covered by the commitment.

### Reapply account deletions after a restore

Keep a current, access-controlled copy of the deletion records outside the database and disk being restored. Verify that it includes every accepted deletion through the recovery cutover. A backup taken before a deletion can contain the account and lack its deletion record. A disk snapshot can also predate the database restore point. Keep the restored database and storage isolated until replay and verification finish.

With the Stripe cleanup migration applied, export the latest records from the authoritative database. This file contains internal account IDs and any Stripe customer IDs still needed for cleanup. Store it in the restricted recovery location. Record the source and export time with the file.

```bash
: "${CURRENT_PSQL_URL:?Set the authoritative database URL}"
umask 077
psql "$CURRENT_PSQL_URL" -X -v ON_ERROR_STOP=1 \
  -c '\copy (SELECT user_id, requested_at, stripe_customer_id, stripe_deleted_at FROM account_deletions) TO STDOUT WITH CSV HEADER' \
  > "account-deletions.csv"
```

Migrate the isolated restored database to the same schema. Run this in `psql` against that database with `ON_ERROR_STOP` enabled. Use the secured export path and verify the `COPY` row count against the authoritative ledger.

```sql
BEGIN;
CREATE TEMP TABLE deletion_replay (
    user_id uuid PRIMARY KEY,
    requested_at timestamptz NOT NULL,
    stripe_customer_id varchar(255),
    stripe_deleted_at timestamptz
) ON COMMIT DROP;
\copy deletion_replay FROM 'account-deletions.csv' WITH CSV HEADER

INSERT INTO account_deletions AS saved (
    user_id, requested_at, stripe_customer_id, stripe_deleted_at
)
SELECT user_id, requested_at, stripe_customer_id, stripe_deleted_at
FROM deletion_replay
ON CONFLICT (user_id) DO UPDATE SET
    requested_at = LEAST(saved.requested_at, EXCLUDED.requested_at),
    local_purged_at = NULL,
    stripe_customer_id = EXCLUDED.stripe_customer_id,
    stripe_deleted_at = EXCLUDED.stripe_deleted_at;
COMMIT;
```

Replay local cleanup with the database URL and storage path of the isolated restore. Resetting `local_purged_at` makes the worker sweep restored files even when the restored database already had a completed deletion. The one-shot command runs local cleanup and verifies the database and the two file directories for every deletion record. A nonzero exit keeps the restore out of service until cleanup can be rerun successfully.

```bash
: "${RESTORED_DATABASE_URL:?Set the isolated restore database URL}"
: "${RESTORED_STORAGE_PATH:?Set the isolated restore storage path}"
DATABASE_URL="$RESTORED_DATABASE_URL" STORAGE_BACKEND=local \
  STORAGE_LOCAL_PATH="$RESTORED_STORAGE_PATH" STRIPE_SECRET_KEY= python - <<'PY'
from pathlib import Path

from sqlalchemy import func
from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.models.account_deletion import AccountDeletion
from app.models.user import User
from app.services.account_deletion import purge_pending_accounts

purge_pending_accounts()
with Session(engine) as session:
    pending = session.scalar(
        select(func.count()).select_from(AccountDeletion).where(AccountDeletion.local_purged_at.is_(None))
    )
    restored_users = session.scalar(
        select(func.count()).select_from(User).join(AccountDeletion, AccountDeletion.user_id == User.id)
    )
    deleted_ids = session.exec(select(AccountDeletion.user_id)).all()

root = Path(get_settings().storage_local_path)
restored_paths = 0
for user_id in deleted_ids:
    for directory in ("forms", "attachments"):
        try:
            (root / directory / str(user_id)).lstat()
        except FileNotFoundError:
            continue
        restored_paths += 1
if pending or restored_users or restored_paths:
    raise SystemExit("Account deletion replay incomplete; keep the restore isolated.")
print(f"Verified account deletion replay for {len(deleted_ids)} records.")
PY
```

The November 19, 2026 local drill used a PostgreSQL dump and a file copy with two synthetic accounts. Replay removed the deleted account and its files from both a database-and-file restore and a file-only restore. The other account and its files passed verification in both cases.

## Entra

Register a Web platform redirect, not a SPA redirect. The URI is exactly:

```text
https://<API_ORIGIN>/auth/microsoft/callback
```

While the API host is the free web default, that is `https://<name>.onrender.com/auth/microsoft/callback` after the host shows you `<name>`. It is not `https://redocu.app/auth/microsoft/callback`. The browser returns to `https://redocu.app/inbox` only because `FRONTEND_URL` says so, after the API has exchanged the code.

`GRAPH_TENANT_ID` must match the account types on that app registration. The example file uses `common` as the code default when the variable is unset. `.env.example` for local Compose still shows `consumers`.

## Smoke test, without sending mail

Do this only after the process is up, Postgres has the `vector` extension, and the Entra secret is present. This repository has not run these steps against a live host.

1. `GET https://<API_ORIGIN>/health` returns 200 and every check is `ok`. A cold start can take about a minute before this answers.
2. `POST /auth/register` returns 201 and a bearer token. `GET /users/me` shows `mailbox: null`.
3. `GET /auth/microsoft/start` with that bearer token returns `authorization_url` on `https://login.microsoftonline.com/`. The token is not in that URL.
4. Open the URL in a browser, sign in, and stop on `https://redocu.app/inbox`. Success is `connected=1`. Failure is `connected=0` and one of `microsoft_denied`, `invalid_state`, `token_exchange`, `mailbox_taken`, `mailbox_conflict`.
5. `GET /users/me` shows `mailbox.email`. `POST /messages/sync` returns 202. Poll `GET /messages` until rows appear or `sync.status` is `idle` or `failed`.
6. Stop after sync. This smoke test ends before approving a reply.

## Approve & send release check

1. Deploy the API on Render before releasing the confirmation UI on Cloudflare. Run `python -m app.deploy.migrate_once` as part of the API release and check that the deployment is healthy. The confirmation UI uses `GET /messages/{id}/send-attempts/{attempt_id}` and reads `pending_send_attempt_id` from message detail after a reload.
2. Connect dedicated test mailboxes. Outlook needs `Mail.ReadWrite` to prepare drafts and `Mail.Send` to submit them. Gmail needs `gmail.send` or `gmail.modify` to send. Reconnect test mailboxes whose existing consent lacks the send permission.
3. From an inbox you control, send a fresh message to each test mailbox. Open each message in redocu, generate a reply, save a distinct edit, and click **Approve & send**. Check that the edited reply arrives once, from the original message's mailbox, and that redocu adds one approval record.
4. When Outlook returns pending confirmation, reload and use **Check send status** on the same message. The app should show **Sent** once it finds the provider's sent copy. If confirmation stays pending, check that mailbox's Sent folder and keep the attempt pending while the outcome is uncertain.

Run double-click and lost-response checks with simulated providers before rollout. A second approval request must use the existing attempt and the provider must receive exactly one send request.

## Slack

User token scopes, fixed in the app: `im:history`, `im:read`, `mpim:history`, `mpim:read`, `channels:history`, `channels:read`, `groups:history`, `groups:read`, `users:read`, `chat:write`. Register the redirect `https://redocu-api.onrender.com/auth/slack/callback` on the Slack app. The authorize request uses `user_scope`, so the stored token is a user token.

Sync pulls DMs, group DMs, and channel messages that @-mention the connected user. It does not post. `POST /messages/{id}/slack-reply` and `POST /slack/send` are the only routes that call `chat.postMessage`, and only for the signed-in user. Do not call them from the smoke test below.

## What is still blocked

Pytest covers the production setting rules, Redis URL parsing, the migration lock, and the process supervisor. Hosted TLS connectivity, deployed retention settings, and service quotas each require verification in the deployment environment.
