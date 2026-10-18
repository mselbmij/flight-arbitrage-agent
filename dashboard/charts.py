"""Plotly figures for the dashboard (ARCHITECTURE.md section 8). Pure: frames in, ``go.Figure`` out.

Chart rules (ops/phase-3-brief.md): one y-axis, colour by entity from fixed slots, text in ink
(never series colour), markers >= 8px, lines 2px, recessive grid, legend whenever >= 2 series are
drawn, only the dip is direct-labelled, ``graph_objects`` with an explicit palette.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import plotly.graph_objects as go

MARKER_PX = 9
LINE_PX = 2
HEIGHT = 420

#: Regime boundaries drawn on the days-to-departure chart, with the label for the side they open.
FENCES: list[tuple[int, str]] = [(65, "prime"), (21, "fence"), (14, "late"), (7, "DOT cutoff")]
FENCE_MARGIN = 2  # draw a boundary only if it lies within the data's DTD range +/- this


@dataclass(frozen=True)
class Palette:
    passed: str  # slot 1: passed + rejected markers, benchmark ribbon (12% alpha)
    strike: str  # slot 3
    ceiling: str  # status "critical"
    fence: str  # text-secondary
    ink: str
    grid: str
    ribbon: str
    label_bg: str


LIGHT = Palette(
    passed="#2a78d6",
    strike="#1baf7a",
    ceiling="#d03b3b",
    fence="#52514e",
    ink="#1f1e1d",
    grid="#e8e7e4",
    ribbon="rgba(42,120,214,0.12)",
    label_bg="rgba(255,255,255,0.85)",
)
# The brief gives dark values for slots 1 and 3 only. text-secondary #52514e is ~2:1 on a dark
# page, so the fence colour is lifted to a lighter grey of the same hue family.
DARK = Palette(
    passed="#3987e5",
    strike="#199e70",
    ceiling="#d03b3b",
    fence="#a3a29d",
    ink="#ecebe8",
    grid="#3a3a38",
    ribbon="rgba(57,135,229,0.12)",
    label_bg="rgba(14,17,23,0.85)",
)


def palette(dark: bool) -> Palette:
    return DARK if dark else LIGHT


def _dollars(cents: pd.Series) -> list[float]:
    return [c / 100 for c in cents]


def _base(pal: Palette, *, x_title: str, n_series: int) -> go.Figure:
    axis = {
        "gridcolor": pal.grid,
        "gridwidth": 1,
        "zeroline": False,
        "showline": False,
        "ticks": "outside",
        "tickcolor": pal.ink,
        "tickfont": {"color": pal.ink},
        "title": {"font": {"color": pal.ink}},
    }
    fig = go.Figure()
    fig.update_layout(
        template="plotly_white",
        height=HEIGHT,
        margin={"l": 60, "r": 20, "t": 60, "b": 50},
        paper_bgcolor="rgba(0,0,0,0)",  # follow the page in light and dark
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": pal.ink},
        hovermode="closest",
        showlegend=n_series >= 2,
        legend={"orientation": "h", "y": 1.02, "yanchor": "bottom", "x": 0,
                "font": {"color": pal.ink}},
        xaxis={**axis, "title": {"text": x_title, "font": {"color": pal.ink}}},
        yaxis={**axis, "title": {"text": "Price (USD)", "font": {"color": pal.ink}},
               "tickprefix": "$", "tickformat": ",.0f"},
    )  # fmt: skip
    return fig


def _hover(df: pd.DataFrame) -> list[list[object]]:
    return df[["fare_brand", "stops", "provider", "risk_rejections"]].to_numpy().tolist()


def _points(fig: go.Figure, df: pd.DataFrame, x: str, pal: Palette, *, show_rejected: bool) -> None:
    """Passed fares: filled markers. Rejected: open markers, same hue."""
    where = "%{x|%b %d %H:%M}" if x == "observed_at" else "DTD %{x}"
    detail = "%{customdata[0]} | %{customdata[1]} stop(s) | %{customdata[2]}"
    passed = df[df["risk_passed"]]
    rejected = df[~df["risk_passed"]]
    if not passed.empty:
        fig.add_trace(
            go.Scatter(
                x=passed[x].tolist(), y=_dollars(passed["price_cents"]), mode="markers",
                name="Passed", customdata=_hover(passed),
                marker={"color": pal.passed, "size": MARKER_PX, "symbol": "circle"},
                hovertemplate=f"<b>$%{{y:,.2f}}</b> passed<br>{where}<br>{detail}<extra></extra>",
            )
        )  # fmt: skip
    if show_rejected and not rejected.empty:
        fig.add_trace(
            go.Scatter(
                x=rejected[x].tolist(), y=_dollars(rejected["price_cents"]), mode="markers",
                name="Rejected", customdata=_hover(rejected),
                marker={"color": pal.passed, "size": MARKER_PX, "symbol": "circle-open",
                        "line": {"color": pal.passed, "width": LINE_PX}},
                hovertemplate=(
                    f"<b>$%{{y:,.2f}}</b> rejected: %{{customdata[3]}}<br>{where}<br>{detail}"
                    "<extra></extra>"
                ),
            )
        )  # fmt: skip


def _hline(fig: go.Figure, cents: int | None, x0: object, x1: object, name: str, color: str,
           dash: str) -> None:  # fmt: skip
    if cents is None or x0 is None:
        return
    fig.add_trace(
        go.Scatter(
            x=[x0, x1], y=[cents / 100, cents / 100], mode="lines", name=name,
            line={"color": color, "width": LINE_PX, "dash": dash},
            hovertemplate=f"{name}: <b>$%{{y:,.2f}}</b><extra></extra>",
        )
    )  # fmt: skip


def price_vs_time(
    series: pd.DataFrame,
    bands: pd.DataFrame,
    *,
    ceiling_cents: int | None,
    strike_cents: int | None,
    strike_label: str,
    dark: bool = False,
    show_rejected: bool = True,
) -> go.Figure:
    """Chart 1: every observation against the poll time, with the benchmark band as a ribbon,
    the ceiling (dashed) and the current strike price (dotted). ``strike_cents`` is already
    reference x multiplier."""
    pal = palette(dark)
    fig = _base(pal, x_title="Observed (UTC)", n_series=0)
    usable = bands.dropna(subset=["typical_low_cents", "typical_high_cents"])
    if not usable.empty:
        x = usable["observed_at"].tolist()
        fig.add_trace(  # upper edge: invisible, only anchors the fill
            go.Scatter(x=x, y=_dollars(usable["typical_high_cents"]), mode="lines",
                       line={"width": 0}, showlegend=False, hoverinfo="skip")
        )  # fmt: skip
        fig.add_trace(
            go.Scatter(
                x=x, y=_dollars(usable["typical_low_cents"]), mode="lines", line={"width": 0},
                fill="tonexty", fillcolor=pal.ribbon, name="Typical range",
                hovertemplate="typical low <b>$%{y:,.2f}</b><extra></extra>",
            )
        )  # fmt: skip
    _points(fig, series, "observed_at", pal, show_rejected=show_rejected)
    if not series.empty:
        x0, x1 = series["observed_at"].min(), series["observed_at"].max()
        _hline(fig, ceiling_cents, x0, x1, "Ceiling", pal.ceiling, "dash")
        _hline(fig, strike_cents, x0, x1, f"Strike ({strike_label})", pal.strike, "dot")
    _label_dip(fig, series, "observed_at", pal)
    fig.update_layout(showlegend=sum(1 for t in fig.data if t.showlegend is not False) >= 2)
    return fig


def _label_dip(fig: go.Figure, series: pd.DataFrame, x: str, pal: Palette) -> None:
    """Direct label on the global minimum passed fare, the only point labelled."""
    passed = series[series["risk_passed"]]
    if passed.empty:
        return
    row = passed.loc[passed["price_cents"].idxmin()]
    fig.add_annotation(
        x=row[x], y=row["price_cents"] / 100, text=f"Lowest passed ${row['price_cents'] / 100:,.0f}",
        showarrow=True, arrowhead=2, arrowcolor=pal.ink, ax=80, ay=12,
        font={"color": pal.ink, "size": 12}, bgcolor=pal.label_bg,
    )  # fmt: skip


def price_vs_dtd(
    series: pd.DataFrame,
    *,
    ceiling_cents: int | None,
    strike_cents: int | None,
    strike_label: str,
    dark: bool = False,
    show_rejected: bool = True,
) -> go.Figure:
    """Chart 2: the same observations against days-to-departure, x reversed so time runs left to
    right, with dashed verticals at the regime boundaries the data spans."""
    pal = palette(dark)
    fig = _base(pal, x_title="Days to departure", n_series=0)
    fig.update_xaxes(autorange="reversed")
    _points(fig, series, "dtd", pal, show_rejected=show_rejected)
    if not series.empty:
        lo, hi = int(series["dtd"].min()), int(series["dtd"].max())
        _hline(fig, ceiling_cents, hi, lo, "Ceiling", pal.ceiling, "dash")
        _hline(fig, strike_cents, hi, lo, f"Strike ({strike_label})", pal.strike, "dot")
        for dtd, name in FENCES:
            if lo - FENCE_MARGIN <= dtd <= hi + FENCE_MARGIN:
                fig.add_shape(
                    type="line", x0=dtd, x1=dtd, xref="x", y0=0, y1=1, yref="paper",
                    line={"color": pal.fence, "width": LINE_PX, "dash": "dash"}, layer="below",
                )  # fmt: skip
                fig.add_annotation(
                    x=dtd, y=1, xref="x", yref="paper", text=f"{dtd} | {name}", showarrow=False,
                    textangle=-90, xanchor="right", yanchor="top",
                    font={"color": pal.ink, "size": 11},
                )  # fmt: skip
    fig.update_layout(showlegend=sum(1 for t in fig.data if t.showlegend is not False) >= 2)
    return fig
