-- Study Buddy tables. Paste this whole file into Neon's SQL Editor and press Run.
-- It is safe to run more than once.

-- one row per study plan (the plan itself is stored as JSON text)
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY,
  chat_id BIGINT,
  data TEXT NOT NULL,
  updated DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS plans_chat ON plans (chat_id);

-- text read from the files a student uploaded (the browser extracts it; the files themselves are never stored)
CREATE TABLE IF NOT EXISTS materials (
  plan_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  body TEXT NOT NULL,
  PRIMARY KEY (plan_id, kind, name)
);

-- Telegram updates already handled (Telegram re-sends an update if we answer slowly)
CREATE TABLE IF NOT EXISTS updates (
  update_id BIGINT PRIMARY KEY,
  at DOUBLE PRECISION
);

-- short-lived locks so one chat is handled by one request at a time
CREATE TABLE IF NOT EXISTS locks (
  key TEXT PRIMARY KEY,
  until DOUBLE PRECISION NOT NULL
);

-- prepared lessons/quizzes and shorter versions of lessons
CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  at DOUBLE PRECISION
);

-- AI usage per day, for the daily spending cap
CREATE TABLE IF NOT EXISTS usage (
  day TEXT PRIMARY KEY,
  calls INTEGER DEFAULT 0,
  cost DOUBLE PRECISION DEFAULT 0
);

-- abuse limits (for example, plans created per visitor per day)
CREATE TABLE IF NOT EXISTS counters (
  key TEXT PRIMARY KEY,
  n INTEGER DEFAULT 0,
  until DOUBLE PRECISION
);

-- The Breaking Into Wall Street guide, loaded once from your own copy by import_guide.py (private: only shown with an access code)
CREATE TABLE IF NOT EXISTS guide_docs (
  key TEXT PRIMARY KEY,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guide_images (
  path TEXT PRIMARY KEY,
  data BYTEA NOT NULL
)
