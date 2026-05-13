-- tgarr central registry schema
-- Runs on the tgarr.me host (1.37). Separate from per-instance tgarr tables;
-- prefixed `registry_*` so we can share a Postgres if convenient.

CREATE TABLE IF NOT EXISTS registry_channels (
    username             TEXT PRIMARY KEY,
    title                TEXT,
    members_count        INTEGER,
    media_count          INTEGER,
    audience             TEXT NOT NULL DEFAULT 'sfw',
    language             TEXT,
    category             TEXT,
    first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    contribution_count   INTEGER NOT NULL DEFAULT 1,
    distinct_contributors INTEGER NOT NULL DEFAULT 1,
    verified             BOOLEAN NOT NULL DEFAULT FALSE,
    blocked              BOOLEAN NOT NULL DEFAULT FALSE,
    block_reason         TEXT
);
CREATE INDEX IF NOT EXISTS idx_reg_audience ON registry_channels (audience);
CREATE INDEX IF NOT EXISTS idx_reg_contrib ON registry_channels (distinct_contributors DESC);
CREATE INDEX IF NOT EXISTS idx_reg_last_seen ON registry_channels (last_seen_at DESC);

-- Each POST /api/v1/contribute call. Aggregate stats; no per-channel detail here.
CREATE TABLE IF NOT EXISTS registry_contributions (
    id                   BIGSERIAL PRIMARY KEY,
    instance_hash        TEXT NOT NULL,
    tgarr_version        TEXT,
    channels_accepted    INTEGER NOT NULL DEFAULT 0,
    channels_rejected    INTEGER NOT NULL DEFAULT 0,
    submitted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    remote_ip_hash       TEXT
);
CREATE INDEX IF NOT EXISTS idx_reg_contrib_inst ON registry_contributions (instance_hash, submitted_at DESC);

-- Tracks which instance has ever submitted each channel — used to compute
-- distinct_contributors. The combo is a privacy-friendly pseudonym; raw
-- instance UUIDs from clients are SHA-256 hashed before storage.
CREATE TABLE IF NOT EXISTS registry_contributor_seen (
    instance_hash        TEXT NOT NULL,
    username             TEXT NOT NULL,
    first_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (instance_hash, username)
);
