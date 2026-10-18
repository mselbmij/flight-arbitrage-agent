"""Streamlit shell (ARCHITECTURE.md section 8). Layout and widgets only: frames come from
dashboard/data.py, figures from dashboard/charts.py. Run: ``streamlit run dashboard/app.py``."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `dashboard` / `src` importable

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from dashboard import charts, data  # noqa: E402
from src.db import BudgetLimits  # noqa: E402

DEFAULT_DB = "data/seed.db"
DEFAULT_CONFIG = "config/watchlists.demo.yaml"
ZONE = {True: "✓", False: "–", None: "?"}

st.set_page_config(page_title="Fare tracker", layout="wide")


def usd(cents: object, digits: int = 0) -> str:
    return "—" if cents is None or pd.isna(cents) else f"${cents / 100:,.{digits}f}"


def is_dark() -> bool:
    try:
        return st.context.theme.type == "dark"
    except Exception:  # older Streamlit or no session context
        return st.get_option("theme.base") == "dark"


def limits_from_env() -> BudgetLimits:
    load_dotenv()
    return BudgetLimits(
        max_per_run=int(os.environ.get("MAX_CALLS_PER_RUN", "3")),
        max_per_day=int(os.environ.get("MAX_DAILY_API_CALLS", "4")),
    )


def table_for_display(df: pd.DataFrame) -> pd.DataFrame:
    """Dollars as numbers (so columns sort numerically; formatted by column_config)."""
    out = pd.DataFrame(
        {
            "Route": df["origin"] + "→" + df["destination"],
            "Depart": df["depart"],
            "Return": df["return_"],
            "Regime": df["regime"],
            "DTD": df["dtd"],
            "Cheapest passed": df["cheapest_passed_cents"].astype("Float64") / 100,
            "Strike ref": df["strike_reference_cents"].astype("Float64") / 100,
            "Ref": df["strike_reference_name"],
            "x": df["strike_multiplier"],
            "In zone": [ZONE[None if pd.isna(v) else bool(v)] for v in df["in_strike_zone"]],
            "Last polled": df["last_polled"],
            "Obs": df["n_obs"],
            "Rejected": df["n_rejected"],
        }
    )
    return out


with st.sidebar:
    st.header("Source")
    db_path = st.text_input("Database", os.environ.get("FARES_DB_PATH", DEFAULT_DB))
    config_path = st.text_input("Watchlists", os.environ.get("FARES_CONFIG_PATH", DEFAULT_CONFIG))

st.title("Fare tracker")
try:
    loaded = data.load(data.DashboardSource(db_path=Path(db_path), config_path=Path(config_path)))
except (FileNotFoundError, ValueError, OSError) as e:
    st.error(f"Cannot load the data: {e}")
    st.caption("For demo data run `fares seed`, then reload.")
    st.stop()

try:
    now = datetime.now(UTC)
    if not loaded.resolved:
        st.info("The watchlist file has no watchlists.")
        st.stop()
    table = data.target_table(loaded, now)

    with st.sidebar:
        wl_id = st.selectbox("Watchlist", [w.id for w in loaded.resolved])
        wl_table = table[table["watchlist_id"] == wl_id].reset_index(drop=True)
        by_label = {
            f"{r.origin}\u2192{r.destination} {r.depart:%b %d} ({r.regime}, {r.dtd}d)": r.target_id
            for r in wl_table.itertuples()
        }
        target_id = by_label[st.selectbox("Target", list(by_label))]
        show_rejected = st.toggle("Show rejected fares", value=True)

    wl = next(w for w in loaded.resolved if w.id == wl_id)
    target = next(t for t in wl.targets if t.target_id == target_id)
    row = wl_table[wl_table["target_id"] == target_id].iloc[0]

    # 1. status strip
    budget = data.budget_today(loaded, limits_from_env(), now)
    last_run = data.last_run_at(loaded)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Targets watched", sum(len(w.pollable_targets) for w in loaded.resolved), delta=None)
    c2.metric(
        f"Cheapest passed ({row['regime']})",
        usd(row["cheapest_passed_cents"], 2),
        delta=None,
        help=f"Target {target_id}",
    )
    c3.metric(
        "API budget today",
        f"{budget['used']} / {budget['limit']}",
        delta=None,
        help=f"The config asks for {budget['calls_per_day']} calls/day.",
    )
    c4.metric("Last run (UTC)", f"{last_run:%b %d %H:%M}" if last_run else "—", delta=None)

    # 2 + 3. charts
    series = data.price_series(loaded, target_id)
    ref, mult, ref_name = data.strike_reference(
        wl, target, loaded.db.latest_band(target_id), loaded.db.lowest_seen(target_id), now=now
    )
    strike = None if ref is None else round(ref * mult)
    label = f"{ref_name} × {mult:g}"
    common = {
        "ceiling_cents": wl.ceiling_cents,
        "strike_cents": strike,
        "strike_label": label,
        "dark": is_dark(),
        "show_rejected": show_rejected,
    }
    st.subheader("Price vs time")
    if series.empty:
        st.info("No observations for this target yet.")
    else:
        fig = charts.price_vs_time(series, data.band_series(loaded, target_id), **common)
        st.plotly_chart(fig, theme=None, width="stretch")
        st.subheader("Price vs days to departure")
        st.plotly_chart(charts.price_vs_dtd(series, **common), theme=None, width="stretch")

    # 4. target table
    st.subheader(f"Targets in {wl_id}")
    money = st.column_config.NumberColumn(format="$%.2f")
    st.dataframe(
        table_for_display(wl_table),
        hide_index=True,
        width="stretch",
        column_config={
            "Cheapest passed": money,
            "Strike ref": money,
            "x": st.column_config.NumberColumn(format="%.2f", width="small"),
            "Last polled": st.column_config.DatetimeColumn(format="MMM D, HH:mm"),
        },
    )

    # 5. recent alerts
    st.subheader("Recent alerts")
    alerts = data.alerts_recent(loaded)
    if alerts.empty:
        st.caption("No alerts yet.")
    else:
        shown = alerts.assign(
            price=[usd(c, 2) for c in alerts["price_cents"]],
            reference=[usd(c, 2) for c in alerts["reference_cents"]],
        ).drop(columns=["price_cents", "reference_cents"])
        st.dataframe(shown, hide_index=True, width="stretch")
finally:
    loaded.close()
