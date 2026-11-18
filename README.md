# Inbox API

Backend for an AI-powered inbox. This repository runs a FastAPI app, an ARQ worker, Postgres 16 with pgvector, and Redis, all started with Docker Compose. Through phase 9B, accounts can register and log in, connect an Outlook or Gmail mailbox, sync recent messages, and read a rule-based category and priority score. A message can hold a local reply draft, and an Outlook draft can be saved unsent. The worker also syncs each connected mailbox on a timer while the process is awake. Milestone F is the frontend connection: a Vite app can start Microsoft or Google login, land back on its own origin, read the connected mailbox, ask for a sync, and render the message list. PDF form templates can be uploaded, and the AcroForm fields are stored on the template. Posting field values fills that PDF, and the owner can download it. A job without values matches one of that user's templates from the message text and asks the model for field values. With no model key, that job stops for review and stores no PDF. A Microsoft Graph webhook can enqueue a mailbox sync. Suggesting the form on the message, and attaching the PDF to an Outlook draft, come later. Gmail access is read-only.

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
| 6 | Rule classification, priority scoring, and a correction route for each synced Outlook and Gmail message. Contract: [docs/phase-6-priority-scoring.md](docs/phase-6-priority-scoring.md) |
| Gmail | Read-only Gmail connect (`/auth/google/start`, callback, disconnect). One Outlook mailbox and one Gmail mailbox can both stay connected, and `GET /messages` lists both |
| 7 | Local reply drafts, including an unsent Outlook push. A Gmail message is not pushed |
| 8 | Form library: an upload stores AcroForm fields in `schema_json`. Templates can be listed, edited, and deleted |
| 9A | Fill an AcroForm from known values. The owner downloads the PDF |
| 9B | On-demand match: the message text picks a template, and a JSON extraction fills it. The stub stops for review |

Phase 6 classifies each synced Outlook or Gmail message with deterministic rules and scores it from that category, the due date, and the message text. List and detail return the score, the band, the reasons, and the extracted fields. `PATCH /messages/{id}/classification` records a user correction and re-scores. A later sync keeps each corrected field. The classifier does not call an LLM and does not write back to the mailbox. Gmail is implemented: read-only connect, encrypted refresh tokens, and the same inbox as Outlook.

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

Seed a local inbox after the migrations. This runs only when `APP_ENV` is `development`. It creates `demo@example.com` with password `seed-demo-password`, one Microsoft mailbox, three messages with different scores, and one AcroForm template. It does not connect Gmail and it does not print tokens.

```bash
python scripts/seed_demo.py
```

Inside Compose, that is `docker compose exec api python scripts/seed_demo.py`. Log in with that email and password.

The API allows the origins in `CORS_ORIGINS` and allows credentials. It does not set a session cookie. Browsers may send `Authorization`, `Content-Type`, and `Accept`. `X-Total-Count` is exposed so the app can read the message total. The local default is the Vite dev server at http://localhost:5173 and http://127.0.0.1:5173. Do not set `CORS_ORIGINS` to `*`.

Stop the stack with `Ctrl+C`, then:

```bash
docker compose down
```

## What is running

| Service    | Role                                                                 | Port on your machine |
| ---------- | -------------------------------------------------------------------- | -------------------- |
| `api`      | FastAPI, auto-reloads when you save a file                           | 8000                 |
| `worker`   | ARQ process for mailbox sync, the periodic inbox sync, form matching, form fill, and subscription renewal | none                 |
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
  api/messages.py         Message list, detail, local reply drafts, and sync
  api/graph.py            POST /webhooks/graph
  api/forms.py            Form template library and fill jobs
  api/deps.py             Current user, database session, ARQ pool
  models/                 SQLModel tables
  services/graph/         MSAL client, token save and refresh, inbox delta, subscriptions, and unsent Outlook drafts
  services/forms/         Template storage, AcroForm schema, matching, fill jobs, and PDF filling
  services/llm/           LLM protocol; stub unless openai or anthropic has a key
  worker/                 ARQ settings. Mail jobs, form jobs, and the remaining jobs are separate modules
  storage/                Local filesystem storage, with a seam for S3/R2 later
alembic/                  Schema migrations
docker/postgres/          Enables the vector extension on first database create
tests/                    Auth, isolation, and token-encryption tests
```

## Data model

Every row that belongs to a person hangs off `users.id`. Deleting a user cascades to their mailboxes, messages, reply drafts, templates, and fill jobs.

| Table                | Holds |
| -------------------- | ----- |
| `users`              | Email, optional display name, Argon2 password hash |
| `connected_accounts` | One active Outlook mailbox and one active Gmail mailbox: encrypted tokens, and Graph fields when the provider is Microsoft |
| `messages`           | A synced message (`graph_id`, subject, preview, received time, priority score, band, reasons, and classification fields). Sync fills those with the rule classifier and scorer |
| `reply_drafts`       | One local reply per message: status, body, source, Outlook draft id, and error. Deleting the message or the user deletes the row |
| `form_templates`     | An uploaded PDF: storage key, tags, description, and `schema_json` (`kind` plus AcroForm fields). A row stored as `{}` reads as kind `none` |
| `form_fill_jobs`     | One fill attempt: optional template, status, extracted values, dropped field names, confidence, error, and the filled-file path |

`schema_json` is written on upload. `kind` is `acroform` when the PDF has AcroForm fields and `none` when it does not. Each field is `name`, `type` (`text`, `checkbox`, `choice`, or `other`), and `options`. `label` is present only after a PATCH sets it. An XFA-only file with no AcroForm fields is kind `none`.

`form_fill_jobs.status` is a string (`pending`, `running`, `needs_review`, `completed`, `failed`, `rejected`), not a Postgres enum, so adding a status later is a normal migration.

Microsoft and Google tokens on `ConnectedAccount` are encrypted by the column type (`EncryptedText`), so code reads and writes plaintext as usual. This needs `TOKEN_ENCRYPTION_KEY`; see `.env.example`. Losing that key means every mailbox has to reconnect. It is separate from `SECRET_KEY`.

Connecting a mailbox writes the email, tenant id, scopes, and encrypted tokens. `delta_link`, `graph_subscription_id`, and `graph_subscription_expires_at` stay unset until a later phase. The message sync stores `graph_id`, subject, preview, received time, sender name, sender email, read state, and plain-text body, then classifies and scores that row. `conversation_id` and `suggested_form` stay null. List and detail return `priority_score`, `priority_band`, `priority_reasons`, `category`, `category_source`, `category_confidence`, `project_name`, `project_number`, `due_date`, `responsible_party`, `reference_number`, and `user_corrected`. `user_corrected` stays false until a user correction. A corrected field is left as the user saved it on the next sync. `priority_scored_at` is stored and is not on the response. Detail adds `body_text` and `draft`. `draft` is null until a local reply draft exists. The draft object is `id`, `message_id`, `status`, `body`, `source`, `graph_draft_id`, `error`, and `updated_at`. `created_at` is stored on the row and is not returned. The list has no `draft` key.

## Routes

| Route                        | Auth | Purpose |
| ---------------------------- | ---- | ------- |
| `GET /health`                | no   | API, database, pgvector, Redis, and storage |
| `POST /auth/register`        | no   | Create an account and return a token |
| `POST /auth/login`           | no   | Return a token |
| `POST /auth/forgot-password` | no   | `202 {}` whether or not the email exists. Sends a reset link asynchronously |
| `POST /auth/reset-password`  | no   | `204` when the code is valid. `400 {"detail":"reset_token_invalid"}` otherwise |
| `GET /auth/microsoft/start`  | yes  | Return `200` and `{"authorization_url": "..."}`. The Bearer token stays on this request. Redis down is `503` |
| `GET /auth/microsoft/callback` | no | Exchange the code, save or reject the mailbox, enqueue sync, then `302` to `{FRONTEND_URL}/inbox` |
| `POST /auth/microsoft/disconnect` | yes | Clear the active Outlook mailbox's tokens and hide its messages. `200` or `404` |
| `GET /auth/google/start` | yes | Return `200` and `{"authorization_url": "..."}` for Google. Redis down or missing Google config is `503` |
| `GET /auth/google/callback` | no | Exchange the code, save or reject the Gmail mailbox, enqueue sync, then `302` to `{FRONTEND_URL}/inbox` |
| `POST /auth/google/disconnect` | yes | Revoke the Google grant, clear that mailbox's tokens, and hide its messages. `200` or `404` |
| `POST /auth/logout`          | yes  | `204`. Revoke this app JWT on the server |
| `GET /users/me`              | yes  | Read your profile, including `mailbox` and `sync` |
| `PATCH /users/me`            | yes  | Update `display_name`. The response includes `mailbox` and `sync` too |
| `GET /messages`              | yes  | List messages for the active mailbox. `X-Total-Count` is that count |
| `GET /messages/{id}`         | yes  | One active-mailbox message, or `404` with detail `Message not found.` Detail includes `draft` or `null` |
| `POST /messages/{id}/draft`  | yes  | Create or reset the local draft to `pending`, keep `body`, clear `error` and `graph_draft_id`, and enqueue `generate_reply_draft` |
| `GET /messages/{id}/draft`   | yes  | The draft object, or `404` with detail `Draft not found.` |
| `PATCH /messages/{id}/draft` | yes  | Save trimmed text with `source` `user` and `status` `ready` |
| `DELETE /messages/{id}/draft` | yes | `204`. Removes only the local row. Does not change Outlook |
| `POST /messages/{id}/draft/outlook` | yes | Save an unsent Outlook draft. Does not send mail |
| `PATCH /messages/{id}/classification` | yes | Correct the category or an extracted field, log the change, and re-score. Another user's message is `404` |
| `POST /messages/sync`        | yes  | `202` with `enqueued` and `sync_status`, `404` when no mailbox is connected, `503` when the queue rejects the job |
| `POST /webhooks/graph`       | no   | Graph subscription handshake, or enqueue `sync_mailbox_delta` |
| `POST /forms/templates`      | yes  | Upload a PDF. `415` for any other type, `413` when the body is over 10 MB, `422` when the PDF cannot be read. The response adds `kind` and `field_count` |
| `GET /forms/templates`       | yes  | Newest first. Each item is `id`, `filename`, `mime_type`, `tags`, `description`, `kind`, `field_count`, and `created_at` |
| `GET /forms/templates/{id}`  | yes  | The list item plus `schema`. Another user's id is `404` with detail `Form template not found.` |
| `PATCH /forms/templates/{id}` | yes | Replace `tags` and `description`, and set `field_labels` on fields that already exist |
| `DELETE /forms/templates/{id}` | yes | `204`. Deletes the stored file and the row. Fill jobs that reference the template are removed with it |
| `POST /forms/jobs`           | yes  | Create a pending fill job. With `values`, `template_id` is required, confidence is `1.0`, and the job is `run_form_fill`. Without `values`, `message_id` is required, `template_id` is optional, and the job is `match_form_template`. Omitting both `values` and `message_id` is `422`. The response is `id` and `status` |
| `GET /forms/jobs/{job_id}`   | yes  | `id`, `template_id`, `message_id`, `status`, `confidence`, `extracted_values`, `dropped_fields`, `error`, and `file_ready`. Another user's id is `404` |
| `GET /forms/jobs/{job_id}/file` | yes | The filled PDF, or `404` when it is not ready or the job is not theirs |
| `POST /forms/jobs/{job_id}/refill` | yes | Replace `extracted_values`, set `pending`, and enqueue `run_form_fill` |

A new route is protected by adding `user: User = Depends(get_current_user)`, and it must filter its queries by `user.id`. Return 404, not 403, for another user's row, so a caller cannot tell the row exists. `/health`, `/auth/register`, `/auth/login`, `/auth/forgot-password`, `/auth/reset-password`, `/auth/microsoft/callback`, `/auth/google/callback`, `/auth/slack/callback`, and `/webhooks/graph` are public on purpose. Each callback is public because the provider redirects the browser there. The Redis state from the matching start route is what ties that request to a user.

Graph sends `validationToken` when a subscription is created. The webhook answers with that exact token as `text/plain`. Other calls are change notifications. A notification whose `clientState` matches `GRAPH_WEBHOOK_CLIENT_STATE` is accepted with `202` and enqueued with that notification's `subscriptionId`. A mismatch is logged and skipped. After a successful Outlook sync, when `GRAPH_WEBHOOK_URL` and `GRAPH_WEBHOOK_CLIENT_STATE` are both set and the account has no subscription id, the sync creates one. Empty settings are logged and skipped. Gmail does not create a subscription. See Background jobs for how the id is resolved.

## Background jobs

ARQ job names are the function names. Mail jobs are in `app/worker/mail_tasks.py`, form jobs are in `app/worker/form_tasks.py`, and `heartbeat` and `send_password_reset` stay in `app/worker/tasks.py`.

| Job                         | Started by | What it does today |
| --------------------------- | ---------- | ------------------ |
| `heartbeat`                 | nothing yet | Logs and returns `"ok"` |
| `sync_mailbox_delta`        | `GET /auth/microsoft/callback`, `GET /auth/google/callback`, `GET /auth/slack/callback`, and `POST /messages/sync` (account id), and `POST /webhooks/graph` (subscription id) | Refreshes the access token when it is due, reads the Outlook inbox delta, upserts changes, deletes removed rows, then classifies and scores each saved row. Google rows use Gmail, not Graph. Slack rows read DMs, group DMs, and personal @-mentions. None of those paths send Slack messages |
| `rescore_mailbox_priority`  | worker startup, once per connected mailbox | Classifies and scores messages already stored for one mailbox. It does not call Graph or Gmail and does not change sync status |
| `send_password_reset`       | `POST /auth/forgot-password` (user id and request id only) | Creates a reset code, stores its hash, and sends the link. The code is not a job argument |
| `match_form_template`       | `POST /forms/jobs` when `values` is omitted | Scores that user's AcroForm templates, or uses the template the request already set. A JSON object of field values is saved and `run_form_fill` is enqueued. The stub, and any reply that is not a JSON object, finishes `needs_review` with confidence `0` and no file. A missing model key stays on the stub. It does not call Graph or Gmail |
| `run_form_fill`             | `POST /forms/jobs` when `values` is sent, and `POST /forms/jobs/{id}/refill` | Writes the saved values into the AcroForm and stores `forms/{user_id}/{job_id}/filled.pdf`. A template with no fillable fields fails. An empty value map finishes `needs_review` and stores no file. An unexpected error stays `running` until the third try, then fails with a short error |
| `renew_graph_subscriptions` | cron at 00:00 and 12:00 | Renews Outlook subscriptions that expire within 24 hours. A 404 creates the subscription again. A 429 waits for Retry-After once, then the job fails so ARQ can retry |
| `generate_reply_draft`      | `POST /messages/{id}/draft` | Writes the local reply body. The stub stores a fixed sentence. A configured provider stores the model text. It does not call Graph |
| `sync_connected_mailboxes` | cron every 5 minutes by default | Syncs every connected Outlook and Gmail mailbox through `sync_mailbox_delta`. Skips a mailbox that is already pending or running. One account's token or provider error does not stop the others |

`sync_mailbox_delta` takes the account or subscription id, and an optional generation integer. It loads the `connected_accounts` row whose `graph_subscription_id` equals the first string. When no row matches and the string is a UUID, it loads that primary key. The connect callback and `POST /messages/sync` pass the account id and the generation they just stored. The webhook passes only a Graph subscription id. With `graph_subscription_id` empty, that lookup misses, the job logs that no account was found, and it returns. A disconnected mailbox or a generation that is no longer current returns without calling Graph.

When `token_expires_at` is missing or within five minutes, the job exchanges the stored refresh token and saves the new tokens only if the mailbox is still connected and the job's generation still matches. A failed refresh sets `sync_status` to `failed` and `sync.error_code` to `token_refresh_failed`. For Microsoft, the job calls `GraphClient.sync_inbox_delta`. With no stored `delta_link`, that GETs `https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta` with `$select=id,subject,bodyPreview,receivedDateTime,from,isRead,body`. The next run GETs the stored `@odata.deltaLink`. It follows `@odata.nextLink` only when the URL starts with `https://graph.microsoft.com/`, for at most 20 pages, and logs when that cap stops the walk. A `@removed` item deletes the local row. HTML bodies are stored as plain text. A disconnect or a newer sync generation makes the job return without writing tokens or messages. `last_sync_at` changes only after that write commits. Graph errors set `sync_error_code` to `graph_unavailable` and leave `last_sync_at` as it was. The job does not raise after it has recorded that failure, so ARQ does not retry it into a newer generation.

A `provider` of `google` never calls Graph. The job lists `users.messages` with `labelIds=INBOX` and `maxResults=25`, follows `nextPageToken` for at most four pages, then GETs each id with `format=full`. The Gmail id is stored in `graph_id`. `UNREAD` in `labelIds` means `is_read` is false. The snippet is `body_preview`. The plain part, or the HTML part with tags removed, is `body_text`. A 401 refreshes once. `invalid_grant` or a second 401 sets `token_refresh_failed`. A timeout, a network error, or an HTTP 5xx from the token endpoint sets `gmail_unavailable`, so a Google outage does not ask the user to reconnect. Other Gmail errors also set `gmail_unavailable`. The calls are read-only.

After each successful upsert, in that same transaction, the job runs the rule classifier and then the rule scorer. Categories stay in this database. The job does not label or move mail, and it does not call `get_llm()`. A field that already has a row in `message_classification_corrections` is left as the user saved it. A classifier or scorer exception is logged as the exception type and the message id. The message text still commits, and a successful provider sync still goes idle. `rescore_mailbox_priority` takes a connected-account id string. Nothing in the API enqueues it, and it is not on the cron. A missing or disconnected mailbox is skipped. It uses the same correction lock.

When the worker starts, it runs that job for every connected mailbox, Outlook and Gmail alike, unless `RESCORE_ON_STARTUP=0`. The default is on. The pass reads stored rows only. It does not call Graph, Gmail, or an LLM, and it does not change sync status. One mailbox failing is logged as the exception type and that account id, and the worker still starts. The summary line is `priority rescore on startup finished accounts=<n> messages=<n>`. `accounts` is connected mailboxes that finished. `messages` is stored rows on those mailboxes, including rows that already had a score. With the flag off, the line is `priority rescore on startup skipped`.

`sync_connected_mailboxes` is the periodic sync. `PERIODIC_MAILBOX_SYNC` defaults to on. Set it to `0` to skip. `MAILBOX_SYNC_INTERVAL_MINUTES` defaults to 5 and accepts 1 through 60; any other value uses 5. The job reuses `sync_mailbox_delta`, including that job's row lock and generation check, so a mailbox already pending or running is not started again. A stale pending or running row (older than 10 minutes) is claimed. A token refresh failure or a provider error, including HTTP 403, is stored on that account and the loop continues. The pass lists the same recent page the manual sync lists. It does not send mail. On the free Render plan the API and the worker share one service that sleeps after inactivity, so this cron runs only while that service is awake. A sleeping service does not sync until the next request wakes it.

`SCORING_TIMEZONE` defaults to `America/New_York`. Today, tomorrow, weekdays, and due or overdue use that zone. An unknown value falls back to `America/New_York`. None of these settings are required.

`LLM_PROVIDER=stub` (the default), an unknown provider, or `openai` / `anthropic` with an empty key returns `StubLLM`. `complete` still returns the fixed string `[stub llm]`. `openai` with a non-empty `OPENAI_API_KEY`, or `anthropic` with a non-empty `ANTHROPIC_API_KEY`, returns that provider. `LLM_MODEL` is used when it is set. A missing model key stays on the stub.

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
5. `d8e2b4c71a05` — password reset codes and `session_version`
6. `e4c1a9b82d30` — briefly, one active mailbox of either provider
7. `f5a2c8d14e60` — one active Gmail mailbox and one active Outlook mailbox per user
8. `c7e5a2b94d10` — priority band, reasons, scored-at, and classification columns on `messages`. No server defaults, so existing rows stay null
9. `d9a7c4e16f32` — `message_classification_corrections`, the log of user corrections
10. `d28d17cb4c64` — `reply_drafts`, one local reply per message
11. `e7b1c4a92d50` — `dropped_fields` on `form_fill_jobs`, a JSON array of names the filler did not write
12. `a4c9e2b71d08` — `form_fill_jobs.template_id` may be null until a match chooses a template

`b7d3e9a41c20` does not delete messages or tokens. If a user already has more than one Microsoft row, the one with the latest `updated_at` (then the greatest `id`) stays active. The others get `disconnected_at` set and stay in the database with their tokens and mail. The API hides those rows until that same Microsoft account is connected again. An explicit disconnect is different: it clears tokens and the delta link, and it keeps the messages. `e4c1a9b82d30` added one active row per user. `f5a2c8d14e60` drops that limit and keeps one active Outlook mailbox and one active Gmail mailbox. Mail from both is listed together.

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

`mailbox` is the active Outlook mailbox when one is connected, and otherwise the active Gmail mailbox. It stays on Outlook while both are connected, so a client that labels `mailbox` as Outlook and disconnects it does not clear Gmail. `mailboxes` lists every active mailbox. With both providers connected it contains one `google` entry and one `microsoft` entry, and each entry includes that mailbox's own `sync`. Neither object includes tokens, scopes, or the delta link. The top-level `sync` is `null` when nothing is connected. Otherwise it is the busier mailbox: `failed`, then `running`, then `pending`, then `idle`. `last_successful_at` on that top-level object is the latest success across them.

```json
{"status": "idle", "last_successful_at": null, "error_code": null}
```

`status` is `idle`, `pending`, `running`, or `failed`. `last_successful_at` is an ISO-8601 UTC timestamp (`2026-11-07T20:43:08.343229Z`) set only after a sync finishes writing messages. It stays null, or stays at the previous success, while a job is queued, running, or failed. `error_code` is `null`, `queue_unavailable`, `token_refresh_failed`, `graph_unavailable`, or `gmail_unavailable`. It is not an exception string and it never includes a token.

`PATCH /users/me` accepts `display_name` only; omitting it leaves the name unchanged, and sending `null` clears it. Sending `email` is `422`. The patch response uses the same shape, including `mailbox` and `sync`.

New login JWTs are HS256 with `sub`, `iat`, `exp`, `jti`, and `ver`. `ver` is `users.session_version` at the moment the token is minted. `POST /auth/logout` with that token returns `204` and stores the `jti` in `revoked_tokens` until `exp`. A later request with the same token is `401 {"detail":"Not authenticated"}`. A second login's token has a different `jti` and keeps working. Logout does not disconnect the mailbox, does not bump `session_version`, and does not affect another user.

Tokens issued before `jti` existed (only `sub`, `iat`, `exp`) still work until they expire, as long as `session_version` is still 0. A missing `ver` claim is treated as version 0. Logging one of those out sets `users.auth_valid_after` to that token's `iat`. Other legacy tokens with an `iat` at or before that instant stop working. Tokens that have a `jti` ignore `auth_valid_after`. Changing `SECRET_KEY` still logs everyone out. Outside `APP_ENV=development`, the API refuses to start with the placeholder secret or without `TOKEN_ENCRYPTION_KEY`.

Passwords are Argon2id hashes with the same 8–128 character rule on register and reset. Argon2 does not apply bcrypt's 72-byte truncation.

### Password reset

`POST /auth/forgot-password` takes `{"email": "ana@example.com"}`. The email is stored and rate-limited in lowercase, so `Ana@x.com` and `ana@x.com` share one bucket. A valid address always returns `202` and `{}`, whether or not an account exists. An address that is not an email is `422`. The fifth request in an hour for that email, and the fifth request in an hour for that client IP, is `429 {"detail":"rate_limited"}`. The next one is too. Both limits are counted for every well-formed request, including unknown emails. `X-Forwarded-For` is ignored unless `FORWARDED_FOR_HOPS` is at least 1. That number is how many proxies you trust to append the header. The address used is that many entries from the right, not the left-most value a client can set. If the rate-limit store is down, the response is `503 {"detail":"password_reset_unavailable"}` and the account is not looked up.

The worker then creates a code with `secrets.token_urlsafe(32)` (32 random bytes), stores only its SHA-256 hash, and sends mail. The job arguments are the user id and the request id. A newer request invalidates older unused codes. The link is `{FRONTEND_URL}/reset-password#token=<code>`. The fragment is not a query parameter. The subject is `Reset your redocu password`. The plain body is:

```text
Someone asked to reset the password for your redocu account. Use this link within 30 minutes: <link>. If it wasn't you, ignore this email; your password hasn't changed.
```

`POST /auth/reset-password` takes `{"token": "<code>", "password": "<new password>"}`. A valid code sets the password, marks the code used, increments `session_version`, and returns `204`. Every access token issued before that commit then gets `401`, including a legacy token with no `ver` and a token whose `iat` is the same second. A login after the commit works. An unknown, expired, or already used code is `400 {"detail":"reset_token_invalid"}`. A password outside 8–128 characters is `422`.

`EMAIL_PROVIDER` is `fake` in local development (the message stays in process memory and is not logged). `console` is also local-only. `APP_ENV=production` refuses both. Set `disabled` to boot without sending, or `resend` with `EMAIL_API_KEY` and `EMAIL_FROM` (for example `redocu <no-reply@redocu.app>`). The Resend from-domain has to be verified in that account before delivery works. This repository does not call Resend from the test suite. A delivery failure is logged as a status class, not the link, the code, or the request body.

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
| `GRAPH_SCOPES` | Comma-separated scopes sent on the authorize redirect, the code exchange, and token refresh. The default is `https://graph.microsoft.com/User.Read`, `https://graph.microsoft.com/Mail.Read`, `https://graph.microsoft.com/Mail.ReadWrite`, and `https://graph.microsoft.com/Mail.Send` |
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
| `mailbox_conflict` | This user already has a different active mailbox for this same provider. Disconnect that one first. The other provider is left connected |

The redirect target is only `FRONTEND_URL` from the server environment. The request cannot choose a different host. Provider text, the authorization code, and tokens are not placed in the URL. If Redis is down, start returns `503 {"detail":"Outlook connect is temporarily unavailable."}` and the callback returns `invalid_state` without calling Microsoft.

The callback finds `connected_accounts` by provider `microsoft` and that `oid`. The same user connecting that same mailbox again, including after disconnect, updates the email, tenant id (`tid`), and tokens and clears `disconnected_at`. A different Outlook account while one Outlook mailbox is active returns `mailbox_conflict` and does not write. A connected Gmail mailbox does not block Outlook, and Outlook does not block Gmail. The user row is locked for the duration of the decision, and the unique indexes `uq_connected_accounts_one_active_microsoft` and `uq_connected_accounts_one_active_google`, plus `(provider, provider_account_id)`, reject a second active account for the same provider. A lost race rolls back and redirects with `mailbox_conflict` or `mailbox_taken`. It does not return `500`.

On success the new tokens are committed before the worker is enqueued, because Microsoft may have rotated the refresh token and rolling that back would destroy the only working credential. The row is `sync.status` `pending`. If the enqueue fails, the tokens stay, `sync.status` becomes `failed`, `error_code` is `queue_unavailable`, `last_successful_at` is unchanged, and the browser still lands on `connected=1`.

`POST /auth/microsoft/disconnect` with the bearer token clears `access_token`, `refresh_token`, `scopes`, `token_expires_at`, and `delta_link`, sets `disconnected_at`, and bumps `sync_generation` so a queued or running job cannot write tokens or messages back. Messages and form rows stay. The list and detail routes hide that mail until the same Microsoft account connects again. Another user's mailbox is not touched. No active mailbox is `404 {"detail":"No mailbox is connected."}`. Success is `200 {"disconnected": true}`. This does not call Microsoft to revoke the grant. The stored refresh token is gone, so this API cannot refresh access. The user can still remove the app in their Microsoft account if they want the grant itself revoked. One Graph call already in flight can finish at Microsoft, and its result is discarded.

`GET /users/me` then shows the active mailboxes. `POST /messages/sync` locks each one. When a mailbox has no job in flight it bumps that generation, sets `pending`, and enqueues `sync_mailbox_delta` with the account id and that generation. Outlook and Gmail are both queued. A mailbox that is already pending or running is left alone:

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

Each item has `id`, `subject`, `body_preview`, `from_name`, `from_email`, `received_at`, `is_read`, `provider`, `conversation_id`, `priority_score`, `priority_band`, `priority_reasons`, and `suggested_form`. `provider` is `microsoft` or `google`. Messages from every active mailbox are in this one list, newest first. After a sync, `from_name`, `from_email`, and `is_read` come from that provider. Missing values stay `null` (`is_read` is not assumed false). `conversation_id` is still always `null`. `suggested_form` stays `null`. After a sync, `priority_score`, `priority_band`, `priority_reasons`, `category`, `category_source`, `category_confidence`, `project_name`, `project_number`, `due_date`, `responsible_party`, and `reference_number` come from the rule pass. `user_corrected` is false until a correction. A row that has not been scored still has a null score, a null band, and `priority_reasons` `[]`, and `sort=priority` puts that row after every number. `priority_scored_at` is not in this JSON. `PATCH /messages/{id}/classification` accepts `category`, `project_name`, `project_number`, `due_date`, `responsible_party`, and `reference_number`. Omitted keys stay as stored. An unknown key, an unknown category, or a due date that is not `YYYY-MM-DD` is `422`. The response is the message detail. `GET /messages/{id}` adds `body_text` (plain text, or `null`) and `draft` (`null` until a local reply exists). `body_text` is not on the list. Tags, script, and style are removed before storage, and the stored strings do not contain `<` or `>`. Empty text is `null`, so the client keeps showing `body_preview`. Text is capped at 100000 characters. Another user's id, a disconnected mailbox's id, or an unknown id is `404` with detail `Message not found.` A non-UUID is `422`.

Timestamps are UTC ISO-8601. Whole seconds look like `2024-06-01T00:00:00Z`. Values with microseconds look like `2026-11-07T20:43:08.343229Z`. A stored score of 50 is the JSON number `50.0`.

```bash
curl -D - 'http://localhost:8000/messages?limit=50&offset=0&sort=received_at' \
  -H 'Authorization: Bearer <token>'
```

Watch the sync with `docker compose logs -f worker`.

### Reply drafts

`POST /messages/{id}/draft` creates one local draft, or resets the existing row to `pending`. It clears `error` and `graph_draft_id`, keeps `body`, and enqueues `generate_reply_draft`. The response is `200` and the draft object: `id`, `message_id`, `status`, `body`, `source`, `graph_draft_id`, `error`, and `updated_at`. `GET /messages/{id}/draft` returns that object, or `404` with detail `Draft not found.` `PATCH /messages/{id}/draft` takes `{"body": "..."}`, stores the trimmed text, sets `source` to `user` and `status` to `ready`. `DELETE /messages/{id}/draft` is `204` and removes only the local row. It does not change the message and it does not change Outlook.

`POST /messages/{id}/draft/outlook` writes an unsent Outlook draft. The mailbox must be Microsoft, or the response is `422` with detail `Only an Outlook mailbox can save this draft.` The local draft must be `ready` with a non-empty body, or the response is `409` with detail `Draft is not ready.` When `graph_draft_id` is empty the route calls Graph `createReply`, saves that id, then PATCHes `{"body": {"contentType": "Text", "content": <local body>}}`. A later push skips `createReply` and PATCHes the same id. Success is `200`. `status` stays `ready` and `error` is cleared. A Graph failure is `502` with detail `Outlook draft could not be saved.` `status` stays `ready`. If `createReply` already returned an id, that id is kept. A token refresh failure is `502` and the local status stays `ready`. No route sends mail, and no request path includes `/send`.

Another user's message id is `404` with detail `Message not found.`

`generate_reply_draft` copies the subject, body, and preview inside a database session, then calls the LLM after that session closes. On the stub, including a missing model key, the stored body is `Thanks for your message. I will review it and reply shortly.` A configured provider stores the model text. Success sets `status` to `ready` and `source` to `llm`. `StubLLM.complete` still returns `[stub llm]`. A failed run sets `status` to `failed` and `error` to `generation failed`, and it keeps the existing body. If the queue rejects the job, the response is `503` with detail `Reply draft could not be queued.` The row is `failed`, the error is `generation failed`, and the body is kept.

## Production

Host layout, the free-demo limits, migration, rollback, and a no-send Outlook smoke test are in [docs/deployment.md](docs/deployment.md). That guide is not a claim that a host is already running.

The SPA origin is `https://redocu.app`. The API's public HTTPS hostname is not in this repository. Call it `<API_ORIGIN>` (scheme and host, no path) until it is known. Do not guess one, and do not put it in source. A free web host's default `https://<name>.onrender.com` hostname can be that origin until a custom domain is chosen. This repo does not create that service.

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

The API process does not terminate TLS and does not redirect HTTP to HTTPS. Whatever is in front of Uvicorn has to do that. `GRAPH_REDIRECT_URI` is that public `https://<API_ORIGIN>/auth/microsoft/callback`. The app does not build it from the `Host` header or from `X-Forwarded-Proto`. This repo does not choose that proxy, and it does not record a verified Entra registration.

CORS allows `GET`, `POST`, `PATCH`, `DELETE`, and `OPTIONS`. `DELETE /messages/{id}/draft` removes only the local reply draft. No route uses `PUT` or `HEAD`. No route sends mail. Allowed request headers are `Authorization`, `Content-Type`, and `Accept`. `X-Total-Count` is exposed. `allow_credentials` is off because the API does not set a session cookie. The bearer token is a header. `CORS_ORIGINS` is a comma-separated list of exact origins, with no `*` and no trailing slash. Production for this product is `https://redocu.app`.

Deploy:

1. Back up Postgres.
2. Set the environment above in `deploy.env`. Do not point `FRONTEND_URL` at a path. Do not commit that file.
3. Run `python -m app.deploy.migrate_once` once. It holds a Postgres advisory lock. The API and the worker do not migrate on their own.
4. Start the API and the worker as two processes. Restart them together so they share the new columns.
5. Existing JWTs without `jti` keep working until they expire. New logins get a `jti`. `POST /auth/logout` stores that `jti` in Postgres. A Redis restart does not undo a logout.
6. Users who already had two Microsoft rows keep both rows. Only the newest stays visible. Tokens and messages on the others are not deleted.

`APP_ENV=production` refuses the local database password, a database URL without `sslmode=require` (or stricter), a Redis URL without a password, an http frontend origin, a CORS list that omits that origin or contains `*`, a `GRAPH_REDIRECT_URI` whose path is not `/auth/microsoft/callback`, a `GOOGLE_REDIRECT_URI` whose path is not `/auth/google/callback`, and a webhook client state shorter than 16 characters. Both redirect URIs must be https with no query or userinfo. `SLACK_REDIRECT_URI` is checked the same way only when it is set. Leaving the Slack variables empty does not stop the process from booting. `APP_ENV=development` and `APP_ENV=test` do not apply those checks.

## Connecting a Gmail mailbox

Gmail uses the same start, callback, profile, sync, and message routes as Outlook. The provider value is `google`. Access is read-only: the app lists and reads INBOX mail and does not send, modify, or delete it. Outlook and Gmail can be connected at the same time. `GET /messages` returns both, newest first, with `provider` set to `google` or `microsoft`. A second account for the same provider is still rejected until that one is disconnected.

Create an OAuth client in Google Cloud:

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a project and enable the Gmail API.
2. Open **APIs & Services → OAuth consent screen**. Choose External. Add the scopes `openid`, `email`, `https://www.googleapis.com/auth/gmail.readonly`, and `https://www.googleapis.com/auth/gmail.send`. While the app is in **Testing**, only listed test users can connect. Add the Google account you will try. A mailbox connected before `gmail.send` was requested has to reconnect before approve can send.
3. Open **APIs & Services → Credentials → Create credentials → OAuth client ID**. Application type is **Web application**.
4. Under **Authorized redirect URIs**, add the API callback, not the Vite origin. Locally: `http://localhost:8000/auth/google/callback`. Production: `https://redocu-api.onrender.com/auth/google/callback`. Do not register `https://redocu.app`. That host is `FRONTEND_URL`.
5. Copy the client id and client secret into the environment. Leave them out of the frontend, git, logs, and tests.

| Variable | Role |
| -------- | ---- |
| `GOOGLE_CLIENT_ID` | OAuth client id |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret |
| `GOOGLE_REDIRECT_URI` | Redirect URI registered on the client, and the callback this API serves |
| `TOKEN_ENCRYPTION_KEY` | Fernet key for the token columns. The same key used for Microsoft |
| `FRONTEND_URL` | Where the callback sends the browser. Production `https://redocu.app` |

The authorize URL requests `openid`, `email`, `https://www.googleapis.com/auth/gmail.readonly`, and `https://www.googleapis.com/auth/gmail.send`, with `access_type=offline` and `prompt=consent`, so Google returns a refresh token. `GET /auth/google/start` with the Bearer token returns `{"authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?..."}`. The Bearer token is not copied into that URL. Missing client id or redirect URI, or Redis down, is `503 {"detail":"Gmail connect is temporarily unavailable."}`.

The callback is `GET /auth/google/callback`. It deletes the one-time Redis state, exchanges the code at `https://oauth2.googleapis.com/token`, and reads `sub` and `email` from `https://www.googleapis.com/oauth2/v3/userinfo`. Tokens are encrypted on `connected_accounts` with `provider` `google`. `provider_account_id` is the OpenID `sub`. Success and failure use the same inbox redirect as Microsoft.

| `error` | When |
| ------- | ---- |
| `google_denied` | Google sent an `error` query parameter (the user cancelled, or consent was denied), or the token's scopes do not include both `gmail.readonly` and `gmail.send`. The token is revoked and no mailbox is saved |
| `invalid_state` | `code` or `state` is missing, the state is unusable, or Redis could not be read |
| `token_exchange` | The code exchange failed, userinfo failed, the refresh token was missing, or an unexpected error happened before the row was committed |
| `mailbox_taken` | That Google `sub` is already stored for a different app user |
| `mailbox_conflict` | This user already has a different active Gmail mailbox. Disconnect it first. Outlook is left connected |

`POST /auth/google/disconnect` revokes the refresh token at `https://oauth2.googleapis.com/revoke`, then clears the local tokens and sets `disconnected_at`. Messages stay in the database and disappear from `GET /messages` until that same Google account connects again. Success is `200 {"disconnected": true}`. No active Gmail mailbox is `404 {"detail":"No mailbox is connected."}`. `POST /auth/microsoft/disconnect` does not clear a Gmail row. A revoke that Google refuses is logged as a status code only. The local tokens are still deleted.

`GET /users/me` lists both providers in `mailboxes` when both are connected. `POST /messages/sync` enqueues `sync_mailbox_delta` for each active mailbox. The worker does not send the Google token to Microsoft.

Check a test user against `https://redocu.app` only after the Render service has the three Google variables and has been restarted so the production redirect check passes:

1. Confirm the OAuth client is in Testing and the Google account is listed as a test user.
2. Confirm the authorized redirect URI is exactly `https://redocu-api.onrender.com/auth/google/callback`.
3. Sign in at `https://redocu.app` with an existing app account. Password login is still required. Gmail connect does not create the app user.
4. Start connect. The browser should land on Google, show the read-only Gmail consent screen, and return to `https://redocu.app/inbox?connected=1&email=...`.
5. `GET /users/me` shows `mailboxes` with `provider` `google` and that entry's `sync`. `sync.status` moves to `idle` with `last_successful_at` set. If Outlook is also connected, `mailboxes` contains both and `mailbox` stays the Outlook account.
6. `GET /messages` lists recent mail from every connected mailbox, newest first. Each item includes `provider` (`google` or `microsoft`). Open one and confirm `body_text` is plain text.
7. Cancel consent once and confirm the inbox URL is `connected=0&error=google_denied`, with no Google error text in the query.
8. Disconnect Gmail. Those messages leave the list. Outlook mail stays. Connecting the same Google account again shows the stored Gmail messages beside the Outlook mail.
9. A second Gmail account, while the first is still connected, returns `mailbox_conflict` and does not replace the stored Gmail tokens. Outlook is unchanged.

## Connecting Slack

Slack uses the same start, callback, profile, sync, and message list as Outlook and Gmail. The provider value is `slack`. The token is a **user token**. The authorize URL sends the scopes in `user_scope`, not `scope`, so Slack does not issue a bot token. Outlook and Gmail can stay connected at the same time.

Create a Slack app and add a user token redirect:

1. Create an app at [api.slack.com/apps](https://api.slack.com/apps).
2. Under **OAuth & Permissions → Redirect URLs**, add the API callback, not the Vite origin. Locally: `http://localhost:8000/auth/slack/callback`. Production: `https://redocu-api.onrender.com/auth/slack/callback`.
3. Under **User Token Scopes**, add exactly: `im:history`, `im:read`, `mpim:history`, `mpim:read`, `channels:history`, `channels:read`, `groups:history`, `groups:read`, `users:read`, `chat:write`.
4. Copy the client id and client secret into the environment. Leave them out of the frontend, git, logs, and tests.

| Variable | Role |
| -------- | ---- |
| `SLACK_CLIENT_ID` | Slack app client id |
| `SLACK_CLIENT_SECRET` | Slack app client secret |
| `SLACK_REDIRECT_URI` | Redirect URI registered on the app, and the callback this API serves |
| `SLACK_SEND_ENABLED` | `1` (default) allows explicit send. `0` refuses `chat.postMessage` and leaves sync on |
| `TOKEN_ENCRYPTION_KEY` | Fernet key for the token column. The same key used for Microsoft and Google |
| `FRONTEND_URL` | Where the callback sends the browser |

If any of `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, or `SLACK_REDIRECT_URI` is empty, `GET /auth/slack/start` returns `503 {"detail":"Slack is not configured."}`. Outlook and Gmail are unchanged. The callback in that state redirects to `{FRONTEND_URL}/inbox?connected=0&error=not_configured`.

`GET /auth/slack/start` with the Bearer token returns `{"authorization_url":"https://slack.com/oauth/v2/authorize?..."}`. The Bearer token is not copied into that URL.

The callback is `GET /auth/slack/callback`. It deletes the one-time Redis state and exchanges the code at `https://slack.com/api/oauth.v2.access`. Only `authed_user.access_token` with `token_type` `user` is stored. A bot token is not saved. `provider_account_id` is `{team_id}:{user_id}`. These scopes do not include an email address, so the `email` column stores a label, `{display name} ({team name})`. The user token is encrypted with the same Fernet column as the other mailboxes. There is no refresh token unless Slack token rotation is turned on later; this app does not use rotation.

| `error` | When |
| ------- | ---- |
| `not_configured` | Slack client id, secret, or redirect URI is empty |
| `slack_denied` | Slack sent an `error` query parameter, or the response had no user token |
| `invalid_state` | `code` or `state` is missing, the state is unusable, or Redis could not be read |
| `token_exchange` | The code exchange failed |
| `mailbox_taken` | That Slack user is already stored for a different app user |
| `mailbox_conflict` | This user already has a different active Slack workspace. Disconnect it first |

`POST /auth/slack/disconnect` calls `auth.revoke`, then clears the local token and sets `disconnected_at`. Messages stay in the database and disappear from `GET /messages` until that same Slack user connects again. Success is `200 {"disconnected": true}`. No active Slack connection is `404 {"detail":"No mailbox is connected."}`.

The 5-minute `sync_connected_mailboxes` cron includes Slack. A user token with no refresh token is still synced. The first sync of a conversation looks back 14 days. Later syncs continue from the last Slack `ts` stored for that conversation. What is stored:

- Every normal message in a DM or a group DM.
- A channel or private-channel message only when its text mentions the connected user as `<@user id>`. `@channel` and `@here` are not enough. Join, leave, and topic messages are dropped.
- The same classifier and priority scorer used for email. Slack text is the body and the subject is null. Bulk and no-reply penalties are not applied to Slack rows. Outlook and Gmail penalties are unchanged.

`GET /users/me` lists the Slack row in `mailboxes` with `provider` `slack`, the display label in `email`, that row's `sync`, and `chat_write` true when the stored scopes include `chat:write`. The legacy `mailbox` field stays Outlook, or Gmail when Outlook is not connected. It is null when Slack is the only connection.

`GET /messages` includes Slack rows with `provider` `slack`, `slack_channel_id`, `slack_thread_ts`, and `slack_permalink`. Those three are null on email. `subject` is null. `from_name` is the Slack display name. `from_email` is null.

Sending happens only when the signed-in user calls one of these routes. Sync, scoring, reply drafts, and `POST /messages/{id}/approve` do not post to Slack. Approve on a Slack row returns `409 {"detail":"Use the Slack reply endpoint."}`.

| Route | Body | What it does |
| ----- | ---- | ------------ |
| `POST /messages/{id}/slack-reply` | `{"text":"..."}` | `chat.postMessage` in that message's `thread_ts`, as the user |
| `POST /slack/send` | `{"channel":"D...","text":"...","thread_ts":null}` | `chat.postMessage` to a conversation the user can access |

Text must be non-empty and at most 40,000 characters. `SLACK_SEND_ENABLED=0` returns `403 {"detail":"Slack sending is turned off."}`. A missing `chat:write` scope, or Slack `missing_scope`, returns `409 {"detail":"Reconnect Slack to allow sending."}`. `not_in_channel` is 403. `channel_not_found` is 404. A 429 that survives the short retry is returned with `Retry-After`. Every attempt is written to `slack_sent_messages`. A successful send is also stored on `messages` so it shows up in the thread.

## Tests

The suite creates and uses a separate `inbox_test` database, so your dev data is not touched. It migrates that database from the Alembic chain, then covers registration and login, logout, per-user isolation, token encryption, the mailbox and OAuth rules, sync status, plain-text message fields, the form library, PDF fill, and on-demand form match. Microsoft, Google, Graph, Gmail, and LLM calls are mocked or not made. A green suite is not a production connect.

```bash
docker compose run --rm api sh -c "pip install -q -r requirements-dev.txt && pytest"
```

## Not built yet

Passwordless Microsoft or Google sign-in (an account with a null `hashed_password` still cannot log in). A job created with `values` fills the AcroForm and does not call a model. A job created from a message matches a template in the worker. `FORM_MATCH_ON_SYNC` defaults to off, so a mailbox sync does not enqueue that match. `suggested_form` stays null until a later phase. `openai` and `anthropic` run only when that provider's API key is set. A missing key stays on the stub. Microsoft disconnect does not revoke the Microsoft grant remotely. Gmail disconnect does. Sending mail is not implemented. The Outlook draft route creates or updates an unsent draft and does not call send. The classification contract is [docs/phase-6-priority-scoring.md](docs/phase-6-priority-scoring.md). Gmail connect is read-only and does not send, modify, or delete mail.
