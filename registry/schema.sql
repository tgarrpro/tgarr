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

-- Honeypot detection layer. Some registry_channels rows have is_honeypot=TRUE.
-- Real Telegram channels by these names DON'T exist (or are seized squats);
-- they are tripwires. A legitimate tgarr client only contributes channels it
-- has actually joined — it has no way to know the honeypot names. So any
-- contribute call that references one outs the submitter as having scraped
-- our own registry rather than building from real Telegram membership.
ALTER TABLE registry_channels ADD COLUMN IF NOT EXISTS is_honeypot BOOLEAN NOT NULL DEFAULT FALSE;

-- Per-actor suspicion score. Both IP-hash and instance-hash get scored;
-- whichever is higher determines the degraded-response treatment.
CREATE TABLE IF NOT EXISTS registry_suspicion (
    actor_key            TEXT PRIMARY KEY,    -- ip:<hash> or inst:<hash>
    score                INTEGER NOT NULL DEFAULT 0,
    reasons              TEXT,                -- comma-joined list of triggers
    first_flagged_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_flagged_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_suspicion_score ON registry_suspicion (score DESC);

-- One row per GET /api/v1/registry call. Used to enforce per-IP-hash
-- daily pull cap. Pruned weekly (NOW() - 7 days) by the puller cleanup.
CREATE TABLE IF NOT EXISTS registry_pulls (
    id            BIGSERIAL PRIMARY KEY,
    ip_hash       TEXT NOT NULL,
    api_key_set   BOOLEAN NOT NULL DEFAULT FALSE,
    pulled_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pulls_ip_at ON registry_pulls (ip_hash, pulled_at DESC);

-- Seed pipeline. Candidate channels from YAML files (Claude-recall + tgstat
-- scrape) land here. seed_validator task picks them one-by-one, calls
-- Pyrogram get_chat to validate, and on success promotes them to
-- registry_channels with seeded=true.
CREATE TABLE IF NOT EXISTS seed_candidates (
    username           TEXT PRIMARY KEY,
    title              TEXT,
    category           TEXT,
    region             TEXT,
    language           TEXT,
    audience_hint      TEXT,    -- from YAML, may be overridden after validate
    tags               TEXT[],
    source             TEXT,    -- claude-knowledge / tgstat-scrape / curated
    added_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    validated_at       TIMESTAMPTZ,
    validation_status  TEXT     -- pending|alive|dead|banned|csam|forbidden|err
);
CREATE INDEX IF NOT EXISTS idx_seed_status ON seed_candidates (validation_status, added_at);

-- Channel-intelligence columns on registry_channels (set by validator + ongoing
-- meta refresher). Lets clients judge "is this channel worth subscribing".
ALTER TABLE registry_channels ADD COLUMN IF NOT EXISTS seeded BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE registry_channels ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE registry_channels ADD COLUMN IF NOT EXISTS first_msg_at TIMESTAMPTZ;
ALTER TABLE registry_channels ADD COLUMN IF NOT EXISTS last_msg_at TIMESTAMPTZ;
ALTER TABLE registry_channels ADD COLUMN IF NOT EXISTS health_status TEXT;
ALTER TABLE registry_channels ADD COLUMN IF NOT EXISTS health_checked_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_reg_health ON registry_channels (health_status, health_checked_at);
CREATE INDEX IF NOT EXISTS idx_reg_seeded ON registry_channels (seeded) WHERE seeded;
