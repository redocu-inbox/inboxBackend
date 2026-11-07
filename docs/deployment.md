# Deploying the Inbox API

This is a runbook for the processes this repository actually starts. It is not a provisioned host, and it is not a production sign-off.

Local development stays `docker compose up` with `.env` copied from `.env.example`. That stack binds the API on port 8000, Postgres on host port 5433, and Redis on host port 6380. Those addresses are not a public origin.

The public API hostname is not in this repository. Write it as `<API_ORIGIN>` (scheme and host, no path) until a host assigns one. The browser origin for this product is exactly `https://redocu.app`. Where the DNS for that name is managed is not established here.

## What has to be running

| Piece | Why | Durable? |
| --- | --- | --- |
| API process | Uvicorn serving `app.main:app` on HTTP inside the container. TLS is terminated in front of it. | The process can restart. Login JWTs are signed with `SECRET_KEY` and checked against Postgres. |
| Worker process | `arq app.worker.settings.WorkerSettings`. Sync, form jobs, and the 00:00 and 12:00 subscription cron run here. | Same code image as the API. It must see the same database, Redis, and file directory. |
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

`GRAPH_CLIENT_ID` and `GRAPH_CLIENT_SECRET` are not required for the process to boot. Outlook connect will fail until they are set. They stay on the API and the worker. They are not frontend build variables.

`rediss://` is the TLS Redis scheme. ARQ's `RedisSettings.from_dsn` sets TLS when it sees that scheme. `redis://` is plain TCP, which is only appropriate on a private network. This repository does not open either URL during tests.

A Neon-style URL is a direct host plus TLS, with the driver name changed:

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
6. Stop. Do not call a send endpoint. This API does not implement send. Do not grant `Mail.Send` for this test.

## What is still blocked

No Render, Neon, Redis, or Entra resource was created from here. No container image was built in this environment (Docker is not installed). Pytest covers the production setting rules, Redis URL parsing, the migration lock, and the process supervisor. It does not open a TLS connection to Neon or Redis, and it does not prove a free web service stays inside a vendor's quota.
