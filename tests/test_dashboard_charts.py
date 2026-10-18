"""Phase 3: dashboard/charts.py figure rules (ops/phase-3-brief.md, chart rules)."""

from __future__ import annotations

import pandas as pd
import pytest

from dashboard import charts

T = pd.date_range("2026-09-01", periods=6, freq="D", tz="UTC")


def _series(dtds=(50, 40, 30, 20, 12, 8)) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "observed_at": T,
            "dtd": list(dtds),
            "price_cents": [24000, 23000, 16000, 25000, 30000, 12500],
            "fare_brand": ["main_cabin"] * 5 + ["basic_economy"],
            "stops": [0] * 6,
            "provider": ["mock"] * 6,
            "risk_passed": [True] * 5 + [False],
            "risk_rejections": [""] * 5 + ["basic_economy"],
            "brand_unverified": [False] * 6,
            "itinerary_key": ["k"] * 6,
        }
    )


def _bands() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "observed_at": T,
            "typical_low_cents": pd.array([20000] * 6, dtype="Int64"),
            "typical_high_cents": pd.array([27000] * 6, dtype="Int64"),
            "lowest_price_cents": pd.array([16000] * 6, dtype="Int64"),
            "price_level": ["typical"] * 6,
        }
    )


KW = {"ceiling_cents": 24000, "strike_cents": 21000, "strike_label": "typical_low x 1"}


def _by_name(fig):
    return {t.name: t for t in fig.data if t.name}


@pytest.mark.parametrize("dark", [False, True])
def test_chart1_entities_use_fixed_palette_and_styles(dark):
    fig = charts.price_vs_time(_series(), _bands(), dark=dark, **KW)
    pal = charts.palette(dark)
    t = _by_name(fig)
    assert set(t) == {"Passed", "Rejected", "Typical range", "Ceiling", "Strike (typical_low x 1)"}
    assert t["Passed"].marker.color == pal.passed and t["Passed"].marker.symbol == "circle"
    assert t["Rejected"].marker.color == pal.passed and t["Rejected"].marker.symbol == "circle-open"
    assert t["Passed"].marker.size >= 8 and t["Rejected"].marker.size >= 8
    assert t["Typical range"].fill == "tonexty" and t["Typical range"].fillcolor == pal.ribbon
    assert t["Ceiling"].line.color == "#d03b3b" and t["Ceiling"].line.dash == "dash"
    assert t["Strike (typical_low x 1)"].line.color == pal.strike
    assert t["Strike (typical_low x 1)"].line.dash == "dot"
    assert t["Ceiling"].line.width == 2
    assert fig.layout.showlegend is True


def test_palette_values():
    assert (charts.LIGHT.passed, charts.LIGHT.strike) == ("#2a78d6", "#1baf7a")
    assert (charts.DARK.passed, charts.DARK.strike) == ("#3987e5", "#199e70")


def test_single_y_axis_and_no_secondary():
    fig = charts.price_vs_time(_series(), _bands(), **KW)
    assert all(t.yaxis in (None, "y") for t in fig.data)
    assert "yaxis2" not in fig.to_dict()["layout"]
    assert fig.layout.yaxis.tickprefix == "$"


def test_values_are_dollars_and_hover_has_reasons():
    t = _by_name(charts.price_vs_time(_series(), _bands(), **KW))
    assert 125.0 in list(t["Rejected"].y)
    assert t["Rejected"].customdata[0][3] == "basic_economy"
    assert "customdata[3]" in t["Rejected"].hovertemplate


def test_dip_is_the_only_annotation_on_chart1():
    fig = charts.price_vs_time(_series(), _bands(), **KW)
    assert [a.text for a in fig.layout.annotations] == ["Lowest passed $160"]  # not the $125 trap


def test_show_rejected_false_drops_hollow_points():
    fig = charts.price_vs_time(_series(), _bands(), show_rejected=False, **KW)
    assert "Rejected" not in _by_name(fig)


def test_chart1_without_band_or_reference_draws_only_what_exists():
    fig = charts.price_vs_time(_series(), _bands().iloc[0:0], ceiling_cents=None,
                               strike_cents=None, strike_label="-")  # fmt: skip
    assert set(_by_name(fig)) == {"Passed", "Rejected"}


def test_legend_hidden_with_a_single_series():
    s = _series()
    fig = charts.price_vs_time(s[s["risk_passed"]], _bands().iloc[0:0], ceiling_cents=None,
                               strike_cents=None, strike_label="-")  # fmt: skip
    assert fig.layout.showlegend is False


def test_chart2_x_reversed_and_fences_limited_to_data_range():
    fig = charts.price_vs_dtd(_series(dtds=(54, 50, 40, 30, 20, 10)), **KW)
    assert fig.layout.xaxis.autorange == "reversed"
    drawn = sorted(int(s.x0) for s in fig.layout.shapes)
    assert drawn == [14, 21]  # 65 is above 54+2, 7 is below 10-2
    assert all(
        s.line.dash == "dash" and s.line.color == charts.LIGHT.fence for s in fig.layout.shapes
    )
    labels = {a.text for a in fig.layout.annotations}
    assert labels == {"21 | fence", "14 | late"}


def test_chart2_draws_all_fences_when_range_spans_them():
    fig = charts.price_vs_dtd(_series(dtds=(70, 50, 40, 30, 20, 5)), **KW)
    assert sorted(int(s.x0) for s in fig.layout.shapes) == [7, 14, 21, 65]


def test_chart2_fence_margin_is_two_days():
    fig = charts.price_vs_dtd(_series(dtds=(55, 50, 40, 30, 20, 9)), **KW)
    assert 7 in [int(s.x0) for s in fig.layout.shapes]  # 7 >= 9 - 2
    fig = charts.price_vs_dtd(_series(dtds=(55, 50, 40, 30, 20, 10)), **KW)
    assert 7 not in [int(s.x0) for s in fig.layout.shapes]
