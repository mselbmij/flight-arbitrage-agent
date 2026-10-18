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
src/alerts/              telegram / discord                  (Phase 6)
src/runner/              `fares` CLI, cron entrypoint        (Phase 2+)
dashboard/app.py         Streamlit, read-only                (Phase 3)
agents/                  role files for the AI build crew + lessons.md
```

## Agents

Built with a small crew of Claude agents routed by cost: see `agents/README.md`.
