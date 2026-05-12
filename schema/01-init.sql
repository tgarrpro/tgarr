-- tgarr schema v0
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Channels we monitor
CREATE TABLE IF NOT EXISTS channels (
    id              BIGSERIAL PRIMARY KEY,
    tg_chat_id      BIGINT UNIQUE NOT NULL,
    username        TEXT,
    title           TEXT,
    category        TEXT,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    joined_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_msg   BIGINT NOT NULL DEFAULT 0,
    backfilled      BOOLEAN NOT NULL DEFAULT FALSE
);

-- Raw indexed messages
CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    channel_id      BIGINT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    tg_message_id   BIGINT NOT NULL,
    tg_chat_id      BIGINT NOT NULL,
    file_unique_id  TEXT,
    file_name       TEXT,
    caption         TEXT,
    file_size       BIGINT,
    mime_type       TEXT,
    posted_at       TIMESTAMPTZ NOT NULL,
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (channel_id, tg_message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_msg ON messages (tg_chat_id, tg_message_id);
CREATE INDEX IF NOT EXISTS idx_messages_filename_trgm ON messages USING GIN (file_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_messages_posted ON messages (posted_at DESC);

-- Parsed releases (grouped + classified)
CREATE TABLE IF NOT EXISTS releases (
    id              BIGSERIAL PRIMARY KEY,
    guid            UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    name            TEXT NOT NULL,
    category        TEXT,
    series_title    TEXT,
    season          INTEGER,
    episode         INTEGER,
    movie_title     TEXT,
    movie_year      INTEGER,
    quality         TEXT,
    source          TEXT,
    codec           TEXT,
    size_bytes      BIGINT,
    posted_at       TIMESTAMPTZ,
    primary_msg_id  BIGINT REFERENCES messages(id),
    parse_score     REAL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_releases_title ON releases (movie_title, series_title);
CREATE INDEX IF NOT EXISTS idx_releases_posted ON releases (posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_releases_name_trgm ON releases USING GIN (name gin_trgm_ops);

-- Download jobs
CREATE TABLE IF NOT EXISTS downloads (
    id              BIGSERIAL PRIMARY KEY,
    release_id      BIGINT NOT NULL REFERENCES releases(id),
    status          TEXT NOT NULL DEFAULT 'pending',
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    local_path      TEXT,
    error_message   TEXT
);
