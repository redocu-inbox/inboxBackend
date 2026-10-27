# Inbox API

Backend for an AI-powered inbox. This repository currently contains the Phase 1 skeleton: a FastAPI app, an ARQ worker, Postgres 16 with pgvector, and Redis, all started with Docker Compose.

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or another Docker engine with Compose v2). You do not need a local Python install to run the stack.

## Run it

From this directory:

```bash
cp .env.example .env
docker compose up --build
```

Leave that terminal open. The API listens on http://localhost:8000.

In a second terminal, check that every dependency is connected:

```bash
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

Stop the stack with `Ctrl+C`, then:

```bash
docker compose down
```

## What is running

| Service    | Role                                                                 | Port on your machine |
| ---------- | -------------------------------------------------------------------- | -------------------- |
| `api`      | FastAPI, auto-reloads when you save a file                           | 8000                 |
| `worker`   | ARQ process that will run background jobs (sync, scoring, drafts)   | none                 |
| `postgres` | PostgreSQL 16 with the pgvector extension                            | 5433                 |
| `redis`    | Queue and cache used by ARQ                                          | 6380                 |

The `api` and `worker` containers mount this folder, so code changes apply without rebuilding the image. Rebuild when `requirements.txt` changes:

```bash
docker compose up --build
```

## Layout

```
app/
  main.py            FastAPI app, CORS, lifespan
  config.py          Settings loaded from .env / environment
  db.py              SQLModel engine (connection only, no tables yet)
  logging_config.py  Log format for app.* loggers
  api/health.py      GET /health
  worker/            ARQ settings and jobs
  storage/           Local filesystem storage, with a seam for S3/R2 later
docker/postgres/     Enables the vector extension on first database create
```

## Why SQLModel

Database models will use [SQLModel](https://sqlmodel.tiangolo.com/). It is SQLAlchemy 2.0 plus Pydantic v2: one class is both the table and the validation model. That is less to learn than writing a SQLAlchemy table and a separate Pydantic schema for every resource. The same author maintains FastAPI and SQLModel, so the tutorials line up.

`DATABASE_URL` uses the form `postgresql+psycopg://...`. The `+psycopg` part tells SQLAlchemy to use the psycopg v3 driver.

## Useful commands

```bash
docker compose ps                  # status of each service
docker compose logs -f api         # follow API logs
docker compose logs -f worker      # follow worker logs
docker compose down -v             # stop and delete the database volume
```

`docker compose down -v` wipes local Postgres data. The next `up` runs `docker/postgres/init.sql` again, which creates the `vector` extension.

## Hostnames inside vs outside Docker

Inside Compose, containers find each other by service name: `postgres` and `redis`. Those names are what `.env.example` uses, and they only work inside the containers.

Published ports are for tools on your machine (a database GUI, `redis-cli`). From your laptop the host is `localhost`, and the ports are **5433** (Postgres) and **6380** (Redis). Those are offset from the usual 5432 and 6379 so a Postgres or Redis you already have installed does not block `docker compose up`. Inside the containers the ports are still 5432 and 6379.

## Not in this phase

No email sync, no auth, no tables, and no LLM calls yet. `SECRET_KEY` is loaded so it is ready, and nothing signs tokens with it.
