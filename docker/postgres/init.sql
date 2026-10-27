-- Runs once, the first time the Postgres volume is created.
-- pgvector ships in the image; this turns the extension on for the `inbox` database.
CREATE EXTENSION IF NOT EXISTS vector;
