-- 001_initial: runs, api_calls, observations, benchmark_bands, alerts (ARCHITECTURE.md section 2).
-- Datetimes are ISO-8601 text; instants are stored normalised to UTC so text order is time order.
-- Money is INTEGER cents. Enums are stored as their string values.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    "trigger"   TEXT NOT NULL,
    calls_made  INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    error       TEXT
);

CREATE TABLE api_calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL REFERENCES runs (run_id),
    called_at    TEXT NOT NULL,
    provider     TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    watchlist_id TEXT,
    target_id    TEXT,
    status       TEXT NOT NULL,
    cost_units   INTEGER NOT NULL DEFAULT 1,
    latency_ms   INTEGER,
    error        TEXT
);
CREATE INDEX idx_api_calls_target_called ON api_calls (target_id, called_at);
CREATE INDEX idx_api_calls_called ON api_calls (called_at);

CREATE TABLE observations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    watchlist_id          TEXT NOT NULL,
    target_id             TEXT NOT NULL,
    observed_at           TEXT NOT NULL,
    provider              TEXT NOT NULL,
    run_id                TEXT NOT NULL REFERENCES runs (run_id),
    itinerary_key         TEXT NOT NULL,
    itinerary_json        TEXT NOT NULL,
    price_cents           INTEGER NOT NULL,
    currency              TEXT NOT NULL,
    fare_brand            TEXT NOT NULL,
    fare_brand_raw        TEXT,
    changeable            INTEGER,
    refundable            INTEGER,
    carry_on_included     INTEGER,
    seats_remaining       INTEGER,
    days_to_departure     INTEGER NOT NULL,
    booking_url           TEXT,
    raw_payload_ref       TEXT,
    risk_passed           INTEGER,
    risk_rejections_json  TEXT,
    risk_brand_unverified INTEGER,
    risk_config_hash      TEXT
);
CREATE INDEX idx_observations_target_observed ON observations (target_id, observed_at);
CREATE INDEX idx_observations_itinerary_observed ON observations (itinerary_key, observed_at);

CREATE TABLE benchmark_bands (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    watchlist_id       TEXT NOT NULL,
    target_id          TEXT NOT NULL,
    observed_at        TEXT NOT NULL,
    provider           TEXT NOT NULL,
    run_id             TEXT NOT NULL REFERENCES runs (run_id),
    lowest_price_cents INTEGER,
    typical_low_cents  INTEGER,
    typical_high_cents INTEGER,
    price_level        TEXT NOT NULL,
    currency           TEXT NOT NULL
);
CREATE INDEX idx_benchmark_bands_target_observed ON benchmark_bands (target_id, observed_at);

-- alert_key is deliberately NOT unique: dedup is a time window enforced in Database.insert_alert.
CREATE TABLE alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    watchlist_id   TEXT NOT NULL,
    target_id      TEXT,
    kind           TEXT NOT NULL,
    regime         TEXT NOT NULL,
    observation_id INTEGER REFERENCES observations (id),
    itinerary_key  TEXT,
    price_cents    INTEGER,
    reference_cents INTEGER,
    message        TEXT NOT NULL,
    alert_key      TEXT NOT NULL,
    delivered_at   TEXT,
    channel        TEXT
);
CREATE INDEX idx_alerts_key_created ON alerts (alert_key, created_at);
