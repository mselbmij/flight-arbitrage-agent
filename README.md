# flight-arbitrage-agent

Risk-aware airline fare tracker. Polls a few routes twice a day, rejects trap fares
(Basic Economy, long/overnight/co-terminal layovers, bad hours), alerts when a clean fare
hits the strike zone for its days-to-departure regime, and after purchase watches the exact
itinerary for a re-fare credit.

Read `ARCHITECTURE.md` first. It is the contract; the code follows it.

## Status

Phase 1 (schema + contract) done. Nothing runs yet. See ARCHITECTURE.md §9 for the plan.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,dashboard]"
cp .env.example .env            # fill in keys when you reach Phase 4
ruff check . && pytest -m "not live"
```

## Layout

```
config/watchlists.yaml   routes, dates, ceilings, constraints  (source of truth)
src/models.py            Pydantic v2 schemas — the one-way door
src/db.py                SQLite + circuit breaker           (Phase 2)
src/ingestion/           serpapi, amadeus, mock providers    (Phase 4)
src/engine/              risk_filters, rules                 (Phase 4–5)
src/alerts/              ntfy.sh notifier                    (Phase 6)
scripts/                 install.sh, backup_db.sh, crontab.example (Phase 6)
src/runner/              `fares` CLI, cron entrypoint        (Phase 2+)
dashboard/app.py         Streamlit, read-only                (Phase 3)
agents/                  role files for the AI build crew + lessons.md
```

## Agents

Built with a small crew of Claude agents routed by cost: see `agents/README.md`.

## Ops

Everything below is run on the host that owns the cron (Pi / VPS / Mac mini), from the checkout.

### Environment

Read from `.env` (never committed) or the process environment; `.env.example` has the full list.

| Variable | Default | Meaning |
|---|---|---|
| `SERPAPI_KEY`, `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET`, `AMADEUS_HOST` | – | providers; `AMADEUS_HOST=production` on the live DB |
| `MAX_CALLS_PER_RUN`, `MAX_DAILY_API_CALLS` | 2, 3 | the circuit breaker; `fares budget` checks the config against it |
| `FARES_DB_PATH` | `data/fares.db` | the live SQLite file |
| `NTFY_SERVER` | `https://ntfy.sh` | ntfy server |
| `NTFY_TOPIC` | – | topic your phone subscribes to; unset means `fares poll` stores alerts undelivered and warns |
| `NTFY_TOKEN` | – | bearer token, only for reserved topics / self-hosted auth |

### Install and cron

```bash
scripts/install.sh              # venv, pip install -e ".[dashboard]", fares migrate, prints the crontab
cp .env.example .env            # then fill in the keys and NTFY_TOPIC
.venv/bin/fares alert-test      # must print "ntfy" and buzz your phone
crontab -e                      # paste the lines install.sh printed (scripts/crontab.example)
```

**Do not install the cron against the live DB until `fares alert-test` succeeds.** Once-only
alerts (`FENCE_WARNING`, `DOT_24H`) are spent on insertion; if nobody is subscribed to the topic
the first time they fire, you do not get a second chance (undelivered alerts are retried for 24 h
only). Subscribe the ntfy phone app to the topic first, then test.

The crontab polls at 06:50 and 17:20 (plus up to 9 minutes of jitter), backs the DB up at 03:00,
and runs `fares health` at 09:00 and 21:00: if no run has started in 36 h it sends a low-priority
`fare tracker: no run in 36h` ping to the same topic and exits 1 (it keeps no state and writes
nothing to the DB; pass `--max-age-hours N` to change the limit). Cron uses the host's local time
zone. Dry-run any notifier with `--notifier log` (prints titles instead of sending) and
`fares poll --notifier null` stores alerts undelivered.

An alert whose ntfy POST did not return 2xx stays undelivered and is retried at the start of the
next run if it is under 24 h old, then left alone (the run logs how many).

### Backup and restore

`scripts/backup_db.sh` takes an online SQLite backup (safe during a poll) to
`data/backups/fares-YYYYmmdd-HHMM.db` and keeps the newest 14 (`BACKUP_KEEP`, `BACKUP_DIR`).
It exits non-zero on failure, so a cron mail or `ops/backup.log` shows it. It lives on the same disk
as the DB: copy `data/backups/` somewhere else (second disk, `rclone` to a bucket) if the host
itself is the risk.

To restore: comment out the cron lines, stop any running poll, then

```bash
cp data/backups/fares-20261018-0300.db data/fares.db
rm -f data/fares.db-wal data/fares.db-shm
.venv/bin/fares migrate && .venv/bin/fares health
```

### Marking a purchase

Post-purchase tracking (re-fare credits, the DOT 24 h window) follows the exact itinerary you
bought.

1. Find the observation you booked: the alert row's `observation_id` (`SELECT observation_id FROM
   alerts ORDER BY id DESC LIMIT 5`, with any SQLite client) and run `fares key <observation_id>`.
   It prints `itinerary_key` and `target_id`.
2. Edit `config/watchlists.yaml` for that watchlist: set `status: purchased` and add

   ```yaml
   purchase:
     purchased_at: 2026-11-02T09:14:00-08:00   # with your local UTC offset
     price_cents: 41800
     itinerary_key: <from fares key>
     target_id: <from fares key>
     carrier: AS
     changeable: true                          # false for Basic Economy
   ```

3. From the next run on, only that target is polled (first, every run) and the engine alerts on
   `CREDIT_DROP` and `DOT_24H`.
