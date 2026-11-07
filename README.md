# Inbox API

Backend for an AI-powered inbox. Through phase 5 this repository runs a FastAPI app, an ARQ worker, Postgres 16 with pgvector, and Redis, all started with Docker Compose. Accounts can register and log in, connect an Outlook mailbox, and sync recent messages. Milestone F is the frontend connection: a Vite app can start Microsoft login, land back on its own origin, read the connected mailbox, ask for a sync, and render the message list. PDF form templates can be uploaded, and a Microsoft Graph webhook can enqueue a mailbox sync. Filling a PDF, matching a template to a message, and calling a real LLM are still seams.

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or another Docker engine with Compose v2). You do not need a local Python install to run the stack.

## What each phase added

| Phase | What landed |
| ----- | ----------- |
| 1 | FastAPI, ARQ worker, Postgres 16 with pgvector, Redis, local file storage, `GET /health` |
| 2 | SQLModel tables (`users`, `connected_accounts`, `messages`, `form_templates`, `form_fill_jobs`) and the first Alembic migration |
| 3 | Graph webhook, form upload and fill-job routes, and the Graph, form-fill, and LLM service seams. Worker jobs are registered; at that point sync, matching, filling, and subscription renewal logged a TODO |
| 4 | Register and login, profile read and update, Argon2 password hashes, HS256 login JWTs, Fernet encryption for Microsoft tokens, and tests for auth, data isolation, and token encryption |
| 5 | Outlook mailbox connect (`GET /auth/microsoft/start`, `GET /auth/microsoft/callback`), refresh of encrypted Graph tokens, sync of recent messages, and `GET /messages` |
| F | Frontend connection: `authorization_url` from Microsoft start, callback redirect to `FRONTEND_URL`, `mailbox` on `GET /users/me`, `POST /messages/sync`, and the frozen message list and detail contract |
| F2 | One active Outlook mailbox per user, callback failures always redirect, Graph sender/read/plain-text body, sync status, disconnect, and server-side logout |

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

The API allows the origins in `CORS_ORIGINS` and allows credentials. It does not set a session cookie. Browsers may send `Authorization`, `Content-Type`, and `Accept`. `X-Total-Count` is exposed so the app can read the message total. The local default is the Vite dev server at http://localhost:5173 and http://127.0.0.1:5173. Do not set `CORS_ORIGINS` to `*`.

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
  api/users.py            GET and PATCH /users/me, including mailbox
  api/messages.py         GET /messages, GET /messages/{id}, POST /messages/sync
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

Connecting a mailbox writes the email, tenant id, scopes, and encrypted tokens. `delta_link`, `graph_subscription_id`, and `graph_subscription_expires_at` stay unset until a later phase. The message sync stores `graph_id`, subject, preview, received time, sender name, sender email, read state, and plain-text body. `priority_score` stays unset. `conversation_id`, `priority_band`, `priority_reasons`, and `suggested_form` stay null or empty. Detail adds `body_text` and `draft`. `draft` stays null.

## Routes

| Route                        | Auth | Purpose |
| ---------------------------- | ---- | ------- |
| `GET /health`                | no   | API, database, pgvector, Redis, and storage |
| `POST /auth/register`        | no   | Create an account and return a token |
| `POST /auth/login`           | no   | Return a token |
| `GET /auth/microsoft/start`  | yes  | Return `200` and `{"authorization_url": "..."}`. The Bearer token stays on this request. Redis down is `503` |
| `GET /auth/microsoft/callback` | no | Exchange the code, save or reject the mailbox, enqueue sync, then `302` to `{FRONTEND_URL}/inbox` |
| `POST /auth/microsoft/disconnect` | yes | Clear the active mailbox's tokens and hide its messages. `200` or `404` |
| `POST /auth/logout`          | yes  | `204`. Revoke this app JWT on the server |
| `GET /users/me`              | yes  | Read your profile, including `mailbox` and `sync` |
| `PATCH /users/me`            | yes  | Update `display_name`. The response includes `mailbox` and `sync` too |
| `GET /messages`              | yes  | List messages for the active mailbox. `X-Total-Count` is that count |
| `GET /messages/{id}`         | yes  | One active-mailbox message, or `404` with detail `Message not found.` |
| `POST /messages/sync`        | yes  | `202` with `enqueued` and `sync_status`, `404` when no mailbox is connected, `503` when the queue rejects the job |
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
| `sync_mailbox_delta`        | `GET /auth/microsoft/callback` and `POST /messages/sync` (account id), and `POST /webhooks/graph` (subscription id) | Refreshes the access token when it is due, then lists recent messages and upserts them |
| `match_form_template`       | `POST /forms/jobs` | Sets the job to `running`, then logs a TODO. It does not call the LLM |
| `run_form_fill`             | nothing yet | Logs a TODO. `PdfFormFiller.fill` is not implemented |
| `renew_graph_subscriptions` | cron at 00:00 and 12:00 | Logs a TODO |

`sync_mailbox_delta` takes the account or subscription id, and an optional generation integer. It loads the `connected_accounts` row whose `graph_subscription_id` equals the first string. When no row matches and the string is a UUID, it loads that primary key. The connect callback and `POST /messages/sync` pass the account id and the generation they just stored. The webhook passes only a Graph subscription id. With `graph_subscription_id` empty, that lookup misses, the job logs that no account was found, and it returns. A disconnected mailbox or a generation that is no longer current returns without calling Graph.

When `token_expires_at` is missing or within five minutes, the job exchanges the stored refresh token and saves the new tokens only if the mailbox is still connected and the job's generation still matches. A failed refresh sets `sync_status` to `failed` and `sync.error_code` to `token_refresh_failed`. The job then calls `GET https://graph.microsoft.com/v1.0/me/messages` with `$select=id,subject,bodyPreview,receivedDateTime,from,isRead,body`, `$top=25`, and `$orderby=receivedDateTime desc`, and follows `@odata.nextLink` only when it starts with `https://graph.microsoft.com/`, for at most four pages. HTML bodies are stored as plain text. A disconnect or a newer sync generation makes the job return without writing tokens or messages. `last_sync_at` changes only after that write commits. Graph errors set `sync_error_code` to `graph_unavailable` and leave `last_sync_at` as it was. The job does not raise after it has recorded that failure, so ARQ does not retry it into a newer generation.

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
4. `b7d3e9a41c20` — one active Microsoft mailbox per user, sync status, plain-text message fields, and `revoked_tokens`

`b7d3e9a41c20` does not delete messages or tokens. If a user already has more than one Microsoft row, the one with the latest `updated_at` (then the greatest `id`) stays active. The others get `disconnected_at` set and stay in the database with their tokens and mail. The API hides those rows until that same Microsoft account is connected again. An explicit disconnect is different: it clears tokens and the delta link, and it keeps the messages.

## Authentication

Register or log in to get a token, then send it as `Authorization: Bearer <token>`. In the `/docs` page, paste the token into **Authorize**.

```bash
curl -X POST http://localhost:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email": "ana@example.com", "password": "a-long-password"}'

curl http://localhost:8000/users/me -H 'Authorization: Bearer <token>'
```

Emails are stored in lowercase, so `Ana@x.com` and `ana@x.com` are one account. Passwords must be 8–128 characters. A duplicate email is `409`. A wrong email or password is the same `401`, and the hasher still runs when the email is unknown so the two cases take the same time.

`hashed_password` can be null. That account cannot log in with a password (reserved for a later OAuth-only sign-in). Connecting a mailbox is a separate step. `GET /auth/microsoft/start` requires a Bearer token for an account that already exists. Profile responses never include the hash. `GET /users/me` adds `mailbox` and `sync`. `mailbox` is `null` before connect and after disconnect. When a mailbox is active it is exactly:

```json
{"connected": true, "provider": "microsoft", "email": "ana@outlook.com"}
```

That object does not include tokens, scopes, the delta link, or sync fields. `sync` is `null` when `mailbox` is `null`. Otherwise:

```json
{"status": "idle", "last_successful_at": null, "error_code": null}
```

`status` is `idle`, `pending`, `running`, or `failed`. `last_successful_at` is an ISO-8601 UTC timestamp (`2026-11-07T20:43:08.343229Z`) set only after a sync finishes writing messages. It stays null, or stays at the previous success, while a job is queued, running, or failed. `error_code` is `null`, `queue_unavailable`, `token_refresh_failed`, or `graph_unavailable`. It is not an exception string and it never includes a token.

`PATCH /users/me` accepts `display_name` only; omitting it leaves the name unchanged, and sending `null` clears it. Sending `email` is `422`. The patch response uses the same shape, including `mailbox` and `sync`.

New login JWTs are HS256 with `sub`, `iat`, `exp`, and `jti`. `POST /auth/logout` with that token returns `204` and stores the `jti` in `revoked_tokens` until `exp`. A later request with the same token is `401 {"detail":"Not authenticated"}`. A second login's token has a different `jti` and keeps working. Logout does not disconnect the mailbox and does not affect another user.

Tokens issued before `jti` existed (only `sub`, `iat`, `exp`) still work until they expire. Logging one of those out sets `users.auth_valid_after` to that token's `iat`. Other legacy tokens with an `iat` at or before that instant stop working. Tokens that have a `jti` ignore `auth_valid_after`. Changing `SECRET_KEY` still logs everyone out. Outside `APP_ENV=development`, the API refuses to start with the placeholder secret or without `TOKEN_ENCRYPTION_KEY`.

```bash
curl -X POST http://localhost:8000/auth/logout \
  -H 'Authorization: Bearer <token>'
```

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
| `FRONTEND_URL` | One http(s) origin, no path. The callback redirects to `{FRONTEND_URL}/inbox`. Local default `http://localhost:5173`. Production `https://redocu.app`. Rejected at startup if it has whitespace, a path, a query, or userinfo |

Create an app registration with a client secret and those delegated permissions, and allow the account type you set in `GRAPH_TENANT_ID`. Register `GRAPH_REDIRECT_URI` as that app's redirect URI. `consumers` only applies to personal Microsoft accounts.

`docker compose up` starts the worker that runs the sync. Register and log in first. The Vite app then calls start with the Bearer token and sends the browser to `authorization_url`. The token is not copied into that URL.

```bash
curl -s http://localhost:8000/auth/microsoft/start \
  -H 'Authorization: Bearer <token>'
```

The response is `200`:

```json
{"authorization_url": "https://login.microsoftonline.com/..."}
```

The start route stores `oauth_state:<state>` in Redis for 600 seconds, mapped to your user id. Sign in at the Microsoft URL. Microsoft redirects the browser to `GRAPH_REDIRECT_URI` with `code` and `state`, which hits `GET /auth/microsoft/callback`. The callback deletes the state as it reads it, so the state works once. It always answers `302` to `{FRONTEND_URL}/inbox`.

Success:

```text
{FRONTEND_URL}/inbox?connected=1&email=ana%40outlook.com
```

Failure uses `connected=0` and one of these `error` values. The provider's own text is logged on the server and is not put in the URL.

| `error` | When |
| ------- | ---- |
| `microsoft_denied` | Microsoft sent an `error` query parameter (the user cancelled, or the authorize step failed) |
| `invalid_state` | `code` or `state` is missing, the state is not 32–128 url-safe characters, Redis could not be read, or the state is expired, already used, or not a user id |
| `token_exchange` | The code exchange failed, the token response is incomplete, or an unexpected error happened before the mailbox row was committed. General failure for the SPA |
| `mailbox_taken` | That Microsoft `oid` is already stored for a different app user, including a disconnected row. This user's mailbox is not changed |
| `mailbox_conflict` | This user already has a different active Outlook mailbox. Disconnect it first. Stored tokens are not replaced |

The redirect target is only `FRONTEND_URL` from the server environment. The request cannot choose a different host. Provider text, the authorization code, and tokens are not placed in the URL. If Redis is down, start returns `503 {"detail":"Outlook connect is temporarily unavailable."}` and the callback returns `invalid_state` without calling Microsoft.

The callback finds `connected_accounts` by provider `microsoft` and that `oid`. The same user connecting that same mailbox again, including after disconnect, updates the email, tenant id (`tid`), and tokens and clears `disconnected_at`. A different `oid` while one mailbox is active returns `mailbox_conflict` and does not write. The user row is locked for the duration of the decision, and the unique index `uq_connected_accounts_one_active_microsoft` plus `(provider, provider_account_id)` rejects the race. A lost race rolls back and redirects with `mailbox_conflict` or `mailbox_taken`. It does not return `500`.

On success the new tokens are committed before the worker is enqueued, because Microsoft may have rotated the refresh token and rolling that back would destroy the only working credential. The row is `sync.status` `pending`. If the enqueue fails, the tokens stay, `sync.status` becomes `failed`, `error_code` is `queue_unavailable`, `last_successful_at` is unchanged, and the browser still lands on `connected=1`.

`POST /auth/microsoft/disconnect` with the bearer token clears `access_token`, `refresh_token`, `scopes`, `token_expires_at`, and `delta_link`, sets `disconnected_at`, and bumps `sync_generation` so a queued or running job cannot write tokens or messages back. Messages and form rows stay. The list and detail routes hide that mail until the same Microsoft account connects again. Another user's mailbox is not touched. No active mailbox is `404 {"detail":"No mailbox is connected."}`. Success is `200 {"disconnected": true}`. This does not call Microsoft to revoke the grant. The stored refresh token is gone, so this API cannot refresh access. The user can still remove the app in their Microsoft account if they want the grant itself revoked. One Graph call already in flight can finish at Microsoft, and its result is discarded.

`GET /users/me` then shows the active mailbox. `POST /messages/sync` locks that row. When no job is in flight it bumps the generation, sets `pending`, and enqueues `sync_mailbox_delta` with the account id and that generation:

```json
{"enqueued": true, "sync_status": "pending"}
```

`enqueued` true means this call queued a job. It does not mean the sync finished. `last_successful_at` is still null or still the previous success. A second call while that job is `pending` or `running` and younger than 10 minutes returns `202` and does not queue another job:

```json
{"enqueued": false, "sync_status": "running"}
```

`pending` or `running` older than 10 minutes is treated as abandoned and a new generation is queued. The old job then sees the generation mismatch and does not write. If Redis rejects the enqueue, the response is `503 {"detail":"Mailbox sync could not be queued."}` and `sync.status` is `failed` with `queue_unavailable`. With no mailbox the status is `404` and the detail is `No mailbox is connected.`

```bash
curl -X POST http://localhost:8000/messages/sync \
  -H 'Authorization: Bearer <token>'
```

After the worker logs `sync_mailbox_delta`, list messages. The body is a JSON array of that user's rows. `limit` is 1–100 and defaults to 50. `offset` defaults to 0. `sort` is `received_at` (the default, newest first) or `priority` (highest `priority_score` first, null scores after every number, then newest first). `X-Total-Count` is the user's message count and ignores `limit` and `offset`.

Each item has `id`, `subject`, `body_preview`, `from_name`, `from_email`, `received_at`, `is_read`, `conversation_id`, `priority_score`, `priority_band`, `priority_reasons`, and `suggested_form`. After a sync, `from_name`, `from_email`, and `is_read` come from Graph. Missing values stay `null` (`is_read` is not assumed false). `conversation_id` is still always `null`. `priority_score` stays `null` until a later phase writes it, so `sort=priority` currently matches newest-first for synced mail. `priority_reasons` is `[]`. `priority_band` and `suggested_form` stay `null`. `GET /messages/{id}` adds `body_text` (plain text, or `null`) and `draft` (`null`). `body_text` is not on the list. Tags, script, and style are removed before storage, and the stored strings do not contain `<` or `>`. Empty text is `null`, so the client keeps showing `body_preview`. Text is capped at 100000 characters. Another user's id, a disconnected mailbox's id, or an unknown id is `404` with detail `Message not found.` A non-UUID is `422`.

Timestamps are UTC ISO-8601. Whole seconds look like `2024-06-01T00:00:00Z`. Values with microseconds look like `2026-11-07T20:43:08.343229Z`. A stored score of 50 is the JSON number `50.0`.

```bash
curl -D - 'http://localhost:8000/messages?limit=50&offset=0&sort=received_at' \
  -H 'Authorization: Bearer <token>'
```

Watch the sync with `docker compose logs -f worker`.

## Production

The SPA origin is `https://redocu.app`. The API's public HTTPS hostname is not in this repository. Call it `<API_ORIGIN>` (scheme and host, no path) until it is known. Do not guess one, and do not put it in source.

Set these on the API and worker only. None of the secrets belong in the frontend bundle. `GRAPH_CLIENT_ID` appears inside the authorize URL the API returns. The SPA does not need its own copy.

```text
APP_ENV=production
SECRET_KEY=<random 32+ characters, not the example placeholder>
TOKEN_ENCRYPTION_KEY=<Fernet key>
FRONTEND_URL=https://redocu.app
CORS_ORIGINS=https://redocu.app
GRAPH_REDIRECT_URI=https://<API_ORIGIN>/auth/microsoft/callback
GRAPH_CLIENT_ID=<Entra application id>
GRAPH_CLIENT_SECRET=<Entra client secret>
GRAPH_TENANT_ID=<common, consumers, or the directory id>
DATABASE_URL=<postgres>
REDIS_URL=<redis>
```

`www.redocu.app` is a different origin and is not allowed unless you add that exact string to `CORS_ORIGINS` and also change `FRONTEND_URL`. The callback can only return to one origin. Local `FRONTEND_URL` and `CORS_ORIGINS` stay on `localhost:5173` so Compose still works.

Entra: Web platform, not SPA. Redirect URI exactly `GRAPH_REDIRECT_URI`. The SPA URL is not the redirect URI.

The API process does not terminate TLS and does not redirect HTTP to HTTPS. Whatever is in front of Uvicorn has to do that. This repo does not choose that proxy.

Deploy:

1. Back up Postgres.
2. Set the environment above. Do not point `FRONTEND_URL` at a path.
3. `alembic upgrade head` (`b7d3e9a41c20`).
4. Restart the API and the worker together so they share the new columns.
5. Existing JWTs without `jti` keep working until they expire. New logins get a `jti`. `POST /auth/logout` revokes that `jti` only.
6. Users who already had two Microsoft rows keep both rows. Only the newest stays visible. Tokens and messages on the others are not deleted.

## Tests

The suite creates and uses a separate `inbox_test` database, so your dev data is not touched. It migrates that database from the Alembic chain, then covers registration and login, logout, per-user isolation, token encryption, the mailbox and OAuth rules, sync status, and plain-text Graph fields. Microsoft, Graph, and LLM calls are mocked or not made. A green suite is not a production connect.

```bash
docker compose run --rm api sh -c "pip install -q -r requirements-dev.txt && pytest"
```

## Not built yet

Delta-link sync: `GraphClient.sync_inbox_delta` raises `NotImplementedError`. The worker lists recent messages and leaves `delta_link` unset. Creating a Graph subscription, and renewing one (`renew_graph_subscriptions` logs a TODO). `graph_subscription_id` and `graph_subscription_expires_at` stay unset, so a webhook notification still has no mailbox to load unless something else wrote the subscription id. Passwordless Microsoft sign-in (an account with a null `hashed_password` still cannot log in). Writing PDF fields into `schema_json`, filling a PDF (`PdfFormFiller.fill` raises `NotImplementedError`), setting `priority_score`, and any LLM provider other than `stub`. Disconnect does not revoke the Microsoft grant remotely. Priority, drafts, and sending mail are not implemented.
