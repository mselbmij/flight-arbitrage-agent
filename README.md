# flight-arbitrage-agent

Risk-aware airline fare tracker. Polls a few routes twice a day, rejects trap fares
(Basic Economy, long/overnight/co-terminal layovers, bad hours), alerts when a clean fare
hits the strike zone for its days-to-departure regime, and after purchase watches the exact
itinerary for a re-fare credit.

Read `ARCHITECTURE.md` first. It is the contract; the code follows it.

## Status

Phases 1–6 done: schema, DB + breaker, dashboard, providers + risk filter, rules + runner, ntfy alerts + cron scripts. Nothing has run against a live API yet; see Ops below for the go-live order. ARCHITECTURE.md §9 has the phase plan.

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
and runs `fares health` at 09:00 and 21:00. Cron uses the host's local time zone. Dry-run any
notifier with `--notifier log` (prints titles instead of sending) and `fares poll --notifier null`
stores alerts undelivered.

### Failure notifications

Every failure below reaches the ntfy topic with a `Fix:` line. Operational pings are priority 4;
`PROVIDER_ERROR` alerts stay at 2 (one failed call is often transient) and the health check
escalates at 4 if failures continue.

| Failure | Who notices | When | ntfy title |
|---|---|---|---|
| Provider call fails (bad key, quota, 400, timeout) | `fares poll` | that run | `serpapi failed for <watchlist>` (body says what to fix) |
| API budget tripped | `fares poll` | that run | `API budget tripped (...)` |
| Poll cannot start: invalid `watchlists.yaml`, no provider keys, Amadeus test host on the live DB | `fares poll` | that run | `fare tracker: poll failed` |
| Poll crashes: DB locked or corrupt, disk full, permissions, bug | `fares poll` | that run | `fare tracker: poll failed` |
| `fares` will not start (venv missing) | cron line → `scripts/notify.sh` | that run | `fare tracker: poll could not start` |
| Backup fails | `backup_db.sh` → `scripts/notify.sh` | 03:00 | `fare tracker: backup failed` |
| No run in 36 h (host down, cron removed) | `fares health` | 09:00 / 21:00 | `fare tracker: no run in 36h` |
| Newest run errored | `fares health` | 09:00 / 21:00 | `fare tracker: last poll failed` |
| A run still `running` after 2 h (killed: OOM, reboot) | `fares health` | 09:00 / 21:00 | `fare tracker: poll killed mid-run` |
| Every API call in 36 h failed | `fares health` | 09:00 / 21:00 | `fare tracker: every API call failed in 36h` |
| Calls succeed but no fares stored in 36 h | `fares health` | 09:00 / 21:00 | `fare tracker: no fares stored in 36h` |
| Alerts not delivered (topic unset, ntfy down) | `fares health` | 09:00 / 21:00 | `fare tracker: N alert(s) not delivered` |
| Newest backup older than 30 h | `fares health` | 09:00 / 21:00 | `fare tracker: database backup is stale` |
| Under 200 MB free on the DB disk | `fares health` | 09:00 / 21:00 | `fare tracker: disk almost full` |

`fares health` sends one message listing every problem it found (the title is the most urgent),
exits 1, keeps no state and writes nothing to the DB. It repeats at each check until the problem is
fixed. Limits: `--max-age-hours` (36), `--backup-max-age-hours` (30), `--backup-dir`
(`$BACKUP_DIR`, else `data/backups`), `--min-free-mb` (200). `scripts/notify.sh TITLE [BODY]` reads
`NTFY_*` from the environment or `.env` and posts with curl, or python3 when curl is missing.

Nothing can tell you that ntfy itself is unreachable from the host: those pings fail, `fares
health` exits 1 and the reason is in `ops/health.log` (and cron mail, if the host sends it).

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

## License

GNU Affero General Public License v3.0 (`AGPL-3.0-only`). Modified versions must publish their source, including when run as a network service. See `LICENSE`.
