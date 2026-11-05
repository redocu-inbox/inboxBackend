# Inbox API

Backend for an AI-powered inbox. Through phase 5 this repository runs a FastAPI app, an ARQ worker, Postgres 16 with pgvector, and Redis, all started with Docker Compose. Accounts can register and log in, connect an Outlook mailbox, and sync recent messages. PDF form templates can be uploaded, and a Microsoft Graph webhook can enqueue a mailbox sync. Filling a PDF, matching a template to a message, and calling a real LLM are still seams.

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or another Docker engine with Compose v2). You do not need a local Python install to run the stack.

## What each phase added

| Phase | What landed |
| ----- | ----------- |
| 1 | FastAPI, ARQ worker, Postgres 16 with pgvector, Redis, local file storage, `GET /health` |
| 2 | SQLModel tables (`users`, `connected_accounts`, `messages`, `form_templates`, `form_fill_jobs`) and the first Alembic migration |
| 3 | Graph webhook, form upload and fill-job routes, and the Graph, form-fill, and LLM service seams. Worker jobs are registered; at that point sync, matching, filling, and subscription renewal logged a TODO |
| 4 | Register and login, profile read and update, Argon2 password hashes, HS256 login JWTs, Fernet encryption for Microsoft tokens, and tests for auth, data isolation, and token encryption |
| 5 | Outlook mailbox connect (`GET /auth/microsoft/start`, `GET /auth/microsoft/callback`), refresh of encrypted Graph tokens, sync of recent messages, and `GET /messages` |

## Run it

From this directory:

```bash
cp .env.example .env
docker compose up --build
```

Leave that terminal open. The API listens on http://localhost:8000.

In a second terminal, apply migrations (Compose does not run them), then check that every dependency is connected:

```bash
docker compose exec api alembic upgrade head
curl http://localhost:8000/health
```

A healthy response looks like this:

```json
{
  "status": "ok",
  "checks": {
    "api": "ok",
    "database": "ok",
    "pgvector": "ok",
    "redis": "ok",
    "storage": "ok"
  }
}
```

FastAPI also publishes interactive docs at http://localhost:8000/docs.

The API allows the origins in `CORS_ORIGINS` and allows credentials. The default is the Vite dev server at http://localhost:5173 and http://127.0.0.1:5173.

Stop the stack with `Ctrl+C`, then:

```bash
docker compose down
```

## What is running

| Service    | Role                                                                 | Port on your machine |
| ---------- | -------------------------------------------------------------------- | -------------------- |
| `api`      | FastAPI, auto-reloads when you save a file                           | 8000                 |
| `worker`   | ARQ process for mailbox sync, form matching, form fill, and subscription renewal | none                 |
| `postgres` | PostgreSQL 16 with the pgvector extension                            | 5433                 |
| `redis`    | ARQ queue and the mailbox-connect state                              | 6380                 |

The `api` and `worker` containers mount this folder, so code changes apply without rebuilding the image. Rebuild when `requirements.txt` changes:

```bash
docker compose up --build
```

## Layout

```
app/
  main.py                 FastAPI app, CORS, lifespan (opens the ARQ pool)
  config.py               Settings loaded from .env / environment
  db.py                   SQLModel engine
  logging_config.py       Log format for app.* loggers
  security.py             Password hashing, login JWTs, EncryptedText
  api/health.py           GET /health
  api/auth.py             Register, login, and Microsoft mailbox connect
  api/users.py            GET and PATCH /users/me
  api/messages.py         GET /messages
  api/graph.py            POST /webhooks/graph
  api/forms.py            Form template upload and fill jobs
  api/deps.py             Current user, database session, ARQ pool
  models/                 SQLModel tables
  services/graph/         MSAL client, token save and refresh; delta client is still a seam
  services/forms/         Template storage, fill jobs, PDF filler seam
  services/llm/           LLM protocol; only the stub provider is implemented
  worker/                 ARQ settings and jobs
  storage/                Local filesystem storage, with a seam for S3/R2 later
alembic/                  Schema migrations
docker/postgres/          Enables the vector extension on first database create
tests/                    Auth, isolation, and token-encryption tests
```

## Data model

Every row that belongs to a person hangs off `users.id`. Deleting a user cascades to their mailboxes, messages, templates, and fill jobs.

| Table                | Holds |
| -------------------- | ----- |
| `users`              | Email, optional display name, Argon2 password hash |
| `connected_accounts` | One Microsoft mailbox: encrypted tokens, delta link, Graph subscription id |
| `messages`           | A synced message (`graph_id`, subject, preview, received time, optional priority score) |
| `form_templates`     | An uploaded PDF: storage key, tags, and a `schema_json` field map (still empty) |
| `form_fill_jobs`     | One fill attempt: status, extracted values, confidence, error, filled-file path |

`form_fill_jobs.status` is a string (`pending`, `running`, `needs_review`, `completed`, `failed`), not a Postgres enum, so adding a status later is a normal migration.

Microsoft tokens on `ConnectedAccount` are encrypted by the column type (`EncryptedText`), so code reads and writes plaintext as usual. This needs `TOKEN_ENCRYPTION_KEY`; see `.env.example`. Losing that key means every mailbox has to reconnect. It is separate from `SECRET_KEY`.

Connecting a mailbox writes the email, tenant id, scopes, and encrypted tokens. `delta_link`, `graph_subscription_id`, and `graph_subscription_expires_at` stay unset. The message sync stores `graph_id`, subject, body preview, and received time. `priority_score` stays unset.

## Routes

| Route                        | Auth | Purpose |
| ---------------------------- | ---- | ------- |
| `GET /health`                | no   | API, database, pgvector, Redis, and storage |
| `POST /auth/register`        | no   | Create an account and return a token |
| `POST /auth/login`           | no   | Return a token |
| `GET /auth/microsoft/start`  | yes  | Store a short-lived state and redirect (302) to Microsoft |
| `GET /auth/microsoft/callback` | no | Exchange the code, save the mailbox, enqueue `sync_mailbox_delta` |
| `GET /users/me`              | yes  | Read your profile |
| `PATCH /users/me`            | yes  | Update `display_name` |
| `GET /messages`              | yes  | List your synced messages, newest first |
| `POST /webhooks/graph`       | no   | Graph subscription handshake, or enqueue `sync_mailbox_delta` |
| `POST /forms/templates`      | yes  | Upload a PDF template (`application/pdf` only) |
| `POST /forms/jobs`           | yes  | Create a pending fill job and enqueue `match_form_template` |
| `GET /forms/jobs/{job_id}`   | yes  | Status, confidence, error, and extracted values |

A new route is protected by adding `user: User = Depends(get_current_user)`, and it must filter its queries by `user.id`. Return 404, not 403, for another user's row, so a caller cannot tell the row exists. `/health`, `/auth/register`, `/auth/login`, `/auth/microsoft/callback`, and `/webhooks/graph` are public on purpose. The callback is public because Microsoft redirects the browser there. The Redis state from `/auth/microsoft/start` is what ties that request to a user.

Graph sends `validationToken` when a subscription is created. The webhook answers with that exact token as `text/plain`. Other calls are change notifications. A notification whose `clientState` matches `GRAPH_WEBHOOK_CLIENT_STATE` is accepted with `202` and enqueued with that notification's `subscriptionId`. A mismatch is logged and skipped. This API does not create the subscription. See Background jobs for how the id is resolved.

## Background jobs

ARQ job names are the function names in `app/worker/tasks.py`.

| Job                         | Started by | What it does today |
| --------------------------- | ---------- | ------------------ |
| `heartbeat`                 | nothing yet | Logs and returns `"ok"` |
| `sync_mailbox_delta`        | `GET /auth/microsoft/callback` (account id) and `POST /webhooks/graph` (subscription id) | Refreshes the access token when it is due, then lists recent messages and upserts them |
| `match_form_template`       | `POST /forms/jobs` | Sets the job to `running`, then logs a TODO. It does not call the LLM |
| `run_form_fill`             | nothing yet | Logs a TODO. `PdfFormFiller.fill` is not implemented |
| `renew_graph_subscriptions` | cron at 00:00 and 12:00 | Logs a TODO |

`sync_mailbox_delta` takes one string. It loads the `connected_accounts` row whose `graph_subscription_id` equals that string. When no row matches and the string is a UUID, it loads that primary key. The connect callback passes the account id, so that path finds the mailbox. The webhook passes a Graph subscription id. With `graph_subscription_id` empty, that UUID lookup misses, the job logs `No connected account for ...`, and it returns.

When `token_expires_at` is missing or within five minutes, the job exchanges the stored refresh token and saves the new tokens. A failed refresh raises `RuntimeError` and the job stops. It then calls `GET https://graph.microsoft.com/v1.0/me/messages` with `$select=id,subject,bodyPreview,receivedDateTime`, `$top=25`, and `$orderby=receivedDateTime desc`, and follows `@odata.nextLink` for at most four pages. An item that has both `id` and `receivedDateTime` is inserted or updated for that mailbox and `graph_id`.

`LLM_PROVIDER=stub` (the default) returns the fixed string `[stub llm]`. Any other provider raises `NotImplementedError`.

## Why SQLModel

Database models use [SQLModel](https://sqlmodel.tiangolo.com/). It is SQLAlchemy 2.0 plus Pydantic v2: one class is both the table and the validation model. That is less to learn than writing a SQLAlchemy table and a separate Pydantic schema for every resource. The same author maintains FastAPI and SQLModel, so the tutorials line up.

`DATABASE_URL` uses the form `postgresql+psycopg://...`. The `+psycopg` part tells SQLAlchemy to use the psycopg v3 driver.

## Useful commands

```bash
docker compose ps                  # status of each service
docker compose logs -f api         # follow API logs
docker compose logs -f worker      # follow worker logs
docker compose down -v             # stop and delete the database volume
```

`docker compose down -v` wipes local Postgres data. The next `up` runs `docker/postgres/init.sql` again, which creates the `vector` extension. Run `alembic upgrade head` again after that, because wiping the volume also drops the tables.

## Hostnames inside vs outside Docker

Inside Compose, containers find each other by service name: `postgres` and `redis`. Those names are what `.env.example` uses, and they only work inside the containers.

Published ports are for tools on your machine (a database GUI, `redis-cli`). From your laptop the host is `localhost`, and the ports are **5433** (Postgres) and **6380** (Redis). Those are offset from the usual 5432 and 6379 so a Postgres or Redis you already have installed does not block `docker compose up`. Inside the containers the ports are still 5432 and 6379.

## Migrations

Compose does not run migrations. Apply them after pulling new code, and again after `docker compose down -v`:

```bash
docker compose exec api alembic upgrade head
```

The chain is:

1. `8c125a803a3b` — core tables, and `CREATE EXTENSION vector`
2. `55194b925077` — `delta_link` and Graph subscription columns on `connected_accounts`
3. `a1f4c7d29e10` — `hashed_password` on `users`

## Authentication

Register or log in to get a token, then send it as `Authorization: Bearer <token>`. In the `/docs` page, paste the token into **Authorize**.

```bash
curl -X POST http://localhost:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email": "ana@example.com", "password": "a-long-password"}'

curl http://localhost:8000/users/me -H 'Authorization: Bearer <token>'
```

Emails are stored in lowercase, so `Ana@x.com` and `ana@x.com` are one account. Passwords must be 8–128 characters. A duplicate email is `409`. A wrong email or password is the same `401`, and the hasher still runs when the email is unknown so the two cases take the same time.

`hashed_password` can be null. That account cannot log in with a password (reserved for a later OAuth-only sign-in). Connecting a mailbox is a separate step. `GET /auth/microsoft/start` requires a Bearer token for an account that already exists, and the callback body is `connected` plus the mailbox email. Profile responses never include the hash. `PATCH /users/me` accepts `display_name` only; omitting it leaves the name unchanged, and sending `null` clears it. Sending `email` is `422`.

Tokens last `ACCESS_TOKEN_EXPIRE_MINUTES` and cannot be revoked individually. Changing `SECRET_KEY` logs everyone out. Outside `APP_ENV=development`, the API refuses to start with the placeholder secret or without `TOKEN_ENCRYPTION_KEY`.

## Connecting an Outlook mailbox

The connect flow uses a confidential Microsoft Entra app (MSAL) and stores the tokens encrypted. Fill these in `.env` before you connect a mailbox. `TOKEN_ENCRYPTION_KEY` has to be set before the callback saves a token, including when `APP_ENV=development`. Generate it with the command in `.env.example`. Outside development the API also refuses to start without it.

| Variable | Role |
| -------- | ---- |
| `GRAPH_CLIENT_ID` | Application (client) id |
| `GRAPH_CLIENT_SECRET` | Client secret |
| `GRAPH_TENANT_ID` | Authority segment. The authority is `https://login.microsoftonline.com/{GRAPH_TENANT_ID}`. `.env.example` uses `consumers` (personal Microsoft accounts). A work or school mailbox can use its tenant id, or `common`. If the variable is unset, the code default is `common` |
| `GRAPH_REDIRECT_URI` | Redirect URI registered on the app, and the callback this API serves. Locally: `http://localhost:8000/auth/microsoft/callback` |
| `GRAPH_SCOPES` | Comma-separated scopes sent on the authorize redirect, the code exchange, and token refresh. The default is `https://graph.microsoft.com/User.Read`, `https://graph.microsoft.com/Mail.Read`, and `https://graph.microsoft.com/Mail.ReadWrite` |
| `TOKEN_ENCRYPTION_KEY` | Fernet key for the token columns |

Create an app registration with a client secret and those delegated permissions, and allow the account type you set in `GRAPH_TENANT_ID`. Register `GRAPH_REDIRECT_URI` as that app's redirect URI. `consumers` only applies to personal Microsoft accounts.

`docker compose up` starts the worker that runs the sync. Register and log in first. The start route requires the Bearer token, so send it with curl and open the `Location` header in a browser:

```bash
curl -sD - -o /dev/null \
  http://localhost:8000/auth/microsoft/start \
  -H 'Authorization: Bearer <token>'
```

The response is `302`. The start route stores `oauth_state:<state>` in Redis for 600 seconds, mapped to your user id. Sign in at the Microsoft URL. Microsoft redirects the browser to `GRAPH_REDIRECT_URI` with `code` and `state`, which hits `GET /auth/microsoft/callback`. The callback deletes the state as it reads it, so the state works once.

A Microsoft `error` query parameter, or a request missing `code` or `state`, is `400` with detail `Microsoft authentication failed`. A `state` that is expired, already used, or not a user id is `400` with detail `Microsoft sign-in was not completed`. The same detail is returned when the token response has an error, or when it has no access token, no refresh token, no `oid`, or no email (`preferred_username`, otherwise `email`).

The callback finds `connected_accounts` by provider `microsoft` and that `oid`. Connecting the same mailbox again as the same user updates the email, tenant id (`tid`), and tokens. If that `oid` already belongs to another user, the response is `409` with detail `This mailbox is already connected to a different account`. On success it saves the encrypted tokens and enqueues `sync_mailbox_delta` with the account id. The body is:

```json
{"connected": true, "email": "ana@outlook.com"}
```

After the worker logs `sync_mailbox_delta`, list messages. Results are that user's rows, ordered by `received_at` descending. `limit` is 1–100 and defaults to 50. `offset` defaults to 0. Each item has `id`, `subject`, `body_preview`, and `received_at`.

```bash
curl 'http://localhost:8000/messages?limit=50&offset=0' \
  -H 'Authorization: Bearer <token>'
```

Watch the sync with `docker compose logs -f worker`.

## Tests

The suite creates and uses a separate `inbox_test` database, so your dev data is not touched. It migrates that database from the Alembic chain, then covers registration and login, per-user isolation on templates and fill jobs, and encryption of `ConnectedAccount` tokens. Microsoft connect and `sync_mailbox_delta` are outside this suite.

```bash
docker compose run --rm api sh -c "pip install -q -r requirements-dev.txt && pytest"
```

## Not built yet

Delta-link sync: `GraphClient.sync_inbox_delta` raises `NotImplementedError`, and the worker leaves `delta_link` unset. Creating a Graph subscription, and renewing one (`renew_graph_subscriptions` logs a TODO). This phase leaves `graph_subscription_id` and `graph_subscription_expires_at` unset, so a webhook notification has no mailbox to load. Passwordless Microsoft sign-in (an account with a null `hashed_password` still cannot log in). Writing PDF fields into `schema_json`, filling a PDF (`PdfFormFiller.fill` raises `NotImplementedError`), setting `priority_score`, and any LLM provider other than `stub`.
