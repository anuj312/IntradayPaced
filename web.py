# web.py
# Volm page plugin for your main Dash app (app.py)
#
# Exposes:
#   volm_page(BASE) -> layout
#   register_volm(dash_app, BASE, ctx) -> registers callbacks

from datetime import datetime
from typing import Dict, Any, Tuple

import pandas as pd
import dash
from dash import dcc, html, Input, Output
import dash_bootstrap_components as dbc
import dash_ag_grid as dag


# -----------------------------------------------------------------------------
# Volm filters (tune if needed)
# -----------------------------------------------------------------------------
MIN_AVG_VOL_20 = 50_000
MIN_TODAY_VOL = 10_000
MIN_LTP = 20.0

BREAKOUT_PCT_TH = 0.60
BREAKDOWN_PCT_TH = -0.60
POS_NEAR_HIGH_TH = 0.80
POS_NEAR_LOW_TH = 0.20

RANGE_EXP_MULT = 1.20     # range expansion vs avg
RANGE_CONTR_MULT = 0.90   # tight range vs avg (kept for reference)

TOP_N = 15


def _time_factor_ist(now_ist: datetime) -> float:
    """Fraction of session completed (9:15-15:30). Clamped."""
    m_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    m_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)

    total_mins = 375.0
    if now_ist < m_open:
        mins_passed = 1.0
    elif now_ist > m_close:
        mins_passed = total_mins
    else:
        mins_passed = max(1.0, (now_ist - m_open).total_seconds() / 60.0)

    return max(0.01, mins_passed / total_mins)


def _safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _compute_volm_df(ctx: Dict[str, Any]) -> pd.DataFrame:
    """
    Build dataframe with:
      Symbol, %Change (from open), Vol, RVOL(paced), pos_in_range, day_range_pct, flags...
    """
    LOCK = ctx["LOCK"]
    ALL_SYMBOLS = ctx["ALL_SYMBOLS"]
    symbol_to_token = ctx["symbol_to_token"]
    DAILY_STATS = ctx["DAILY_STATS"]
    get_live_or_eod_state = ctx["get_live_or_eod_state"]
    IST = ctx["IST"]

    now_ist = datetime.now(IST)
    tf = _time_factor_ist(now_ist)

    rows = []
    with LOCK:
        for sym in ALL_SYMBOLS:
            tok = symbol_to_token.get(sym)
            if not tok:
                continue

            st = DAILY_STATS.get(tok) or {}
            avg_vol_20 = _safe_float(st.get("avg_vol_20"))
            avg_range_20 = _safe_float(st.get("avg_range_20"))

            state = get_live_or_eod_state(tok)
            if not state:
                continue

            ltp, vol_today, ohlc = state
            ltp = _safe_float(ltp)
            vol_today = _safe_float(vol_today)

            op = _safe_float(ohlc.get("open"))
            hi = _safe_float(ohlc.get("high"))
            lo = _safe_float(ohlc.get("low"))

            if ltp is None or vol_today is None or op is None or op <= 0:
                continue
            if hi is None or lo is None:
                continue
            if avg_vol_20 is None or avg_range_20 is None:
                continue

            # Quality filters
            if avg_vol_20 < MIN_AVG_VOL_20:
                continue
            if vol_today < MIN_TODAY_VOL:
                continue
            if ltp < MIN_LTP:
                continue

            pct_open = (ltp - op) / op * 100.0

            # Intraday range context
            rng = max(0.0, hi - lo)
            day_range_pct = (rng / op) * 100.0

            pos_in_range = 0.5
            if rng > 1e-9:
                pos_in_range = (ltp - lo) / rng
                pos_in_range = float(min(max(pos_in_range, 0.0), 1.0))

            # Average range pct from avg_range_20 (points) -> pct of open
            avg_range_pct_20 = (avg_range_20 / op) * 100.0 if op > 0 else 0.0

            range_exp_ok = day_range_pct >= (RANGE_EXP_MULT * max(0.0001, avg_range_pct_20))
            range_tight_ok = day_range_pct <= (RANGE_CONTR_MULT * max(0.0001, avg_range_pct_20))

            # Time-paced RVOL
            expected_vol = avg_vol_20 * tf
            rvol_paced = vol_today / (expected_vol + 1e-9)

            rows.append(
                {
                    "Symbol": sym,
                    "%Change": round(pct_open, 2),
                    "Vol": int(vol_today),
                    "RVOL": float(rvol_paced),
                    "_pos": float(pos_in_range),
                    "_day_range_pct": float(day_range_pct),
                    "_avg_range_pct_20": float(avg_range_pct_20),
                    "_range_exp_ok": bool(range_exp_ok),
                    "_range_tight_ok": bool(range_tight_ok),
                }
            )

    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _volm_tables(ctx: Dict[str, Any]) -> Tuple[list, list, list, list, float, float]:
    """
    Returns:
      breakout_rows, breakdown_rows, buy_rvol_rows, sell_rvol_rows, shock_th, extreme_th
    """
    df = _compute_volm_df(ctx)
    if df.empty:
        return [], [], [], [], 2.0, 3.0

    # Dynamic thresholds with floors (used for breakout/breakdown + hint)
    if len(df) >= 20:
        q95 = float(df["RVOL"].quantile(0.95))
        q97 = float(df["RVOL"].quantile(0.97))
    else:
        q95, q97 = 2.0, 3.0

    rvol_shock_th = max(2.0, q95)
    rvol_extreme_th = max(3.0, q97)

    breakout = (
        df[
            (df["RVOL"] >= rvol_shock_th)
            & (df["%Change"] >= BREAKOUT_PCT_TH)
            & (df["_pos"] >= POS_NEAR_HIGH_TH)
            & (df["_range_exp_ok"])
        ]
        .sort_values("RVOL", ascending=False)
        .head(TOP_N)[["Symbol", "%Change", "RVOL", "Vol"]]
        .to_dict("records")
    )

    breakdown = (
        df[
            (df["RVOL"] >= rvol_shock_th)
            & (df["%Change"] <= BREAKDOWN_PCT_TH)
            & (df["_pos"] <= POS_NEAR_LOW_TH)
            & (df["_range_exp_ok"])
        ]
        .sort_values("RVOL", ascending=False)
        .head(TOP_N)[["Symbol", "%Change", "RVOL", "Vol"]]
        .to_dict("records")
    )

    # Buying vs Selling RVOL (proxy by sign of %Change from open)
    buy_rvol = (
        df[df["%Change"] >= 0]
        .sort_values("RVOL", ascending=False)
        .head(TOP_N)[["Symbol", "%Change", "RVOL", "Vol"]]
        .to_dict("records")
    )

    sell_rvol = (
        df[df["%Change"] < 0]
        .sort_values("RVOL", ascending=False)
        .head(TOP_N)[["Symbol", "%Change", "RVOL", "Vol"]]
        .to_dict("records")
    )

    return breakout, breakdown, buy_rvol, sell_rvol, rvol_shock_th, rvol_extreme_th


def volm_page(BASE: str):
    cols = [
        {
            "colId": "stock",
            "field": "Symbol",
            "headerName": "STOCK",
            "cellRenderer": "SymbolCell",
            "flex": 1,
            "minWidth": 160,
            "headerClass": "h-left",
            "cellClass": "c-left",
        },
        {
            "colId": "pct",
            "field": "%Change",
            "headerName": "%CHG",
            "cellRenderer": "PctPill",
            "minWidth": 130,
            "maxWidth": 150,
            "suppressSizeToFit": True,
            "headerClass": "ag-right-aligned-header h-right",
            "cellClass": "ag-right-aligned-cell cell-num c-right",
        },
        {
            "colId": "rvol",
            "field": "RVOL",
            "headerName": "RVOL",
            "cellRenderer": "RfactorPill",
            "minWidth": 120,
            "maxWidth": 150,
            "suppressSizeToFit": True,
            "headerClass": "ag-right-aligned-header h-right",
            "cellClass": "ag-right-aligned-cell cell-num c-right",
            "valueFormatter": {"function": "fmt2(params.value)"},
        },
        {
            "colId": "vol",
            "field": "Vol",
            "headerName": "VOLUME",
            "cellRenderer": "VolPill",
            "minWidth": 140,
            "maxWidth": 180,
            "suppressSizeToFit": True,
            "headerClass": "ag-right-aligned-header h-right",
            "cellClass": "ag-right-aligned-cell cell-num c-right",
        },
    ]

    # Make it show ~10 rows and scroll to 15
    ROW_H = 34
    HDR_H = 34
    GRID_10ROWS_HEIGHT = f"{HDR_H + (10 * ROW_H) + 4}px"

    grid_opts = {
        "getRowId": {"function": "params.data.Symbol"},
        "alwaysShowVerticalScroll": False,
        "animateRows": False,
        "rowHeight": ROW_H,
        "headerHeight": HDR_H,
        "onGridReady": {"function": "params.api.sizeColumnsToFit();"},
        "onGridSizeChanged": {"function": "params.api.sizeColumnsToFit();"},
    }

    def grid(id_, height="min(420px, 42vh)"):
        return dag.AgGrid(
            id=id_,
            className="ag-theme-alpine-dark grid-wrap compact-grid",
            columnDefs=cols,
            rowData=[],
            defaultColDef={"sortable": True, "filter": True, "resizable": True},
            dashGridOptions=grid_opts,
            style={"height": height, "width": "100%"},
        )

    return html.Div(
        [
            dcc.Interval(id="refresh_volm", interval=2000, n_intervals=0),
            dbc.Row(
                [
                    dbc.Col(html.H4("Volm (Volume Shockers)", className="page-title"), width=True),
                    dbc.Col(
                        dbc.Button("Back", href=f"{BASE}", color="secondary", outline=True, className="btn-back"),
                        width="auto",
                    ),
                ],
                className="align-items-center g-2",
            ),
            html.Div(id="volm-thresholds", className="hint", style={"marginBottom": "10px"}),

            dbc.Row(
                [
                    dbc.Col([html.H6("Breakout Vol Shockers", className="mt-1"), grid("volm-breakout")], md=6),
                    dbc.Col([html.H6("Breakdown Vol Shockers", className="mt-1"), grid("volm-breakdown")], md=6),
                ],
                className="g-2",
            ),
            html.Hr(),
            dbc.Row(
                [
                    dbc.Col(
                        [html.H6("Top 15 BUYING RVOL (RVOL high + %CHG ≥ 0)", className="mt-1"),
                         grid("volm-buy-rvol", height=GRID_10ROWS_HEIGHT)],
                        md=6,
                    ),
                    dbc.Col(
                        [html.H6("Top 15 SELLING RVOL (RVOL high + %CHG < 0)", className="mt-1"),
                         grid("volm-sell-rvol", height=GRID_10ROWS_HEIGHT)],
                        md=6,
                    ),
                ],
                className="g-2",
            ),
        ],
        className="page-wrap",
    )


def register_volm(dash_app, BASE: str, ctx: Dict[str, Any]) -> None:
    @dash_app.callback(
        Output("volm-breakout", "rowData"),
        Output("volm-breakdown", "rowData"),
        Output("volm-buy-rvol", "rowData"),
        Output("volm-sell-rvol", "rowData"),
        Output("volm-thresholds", "children"),
        Input("refresh_volm", "n_intervals"),
        prevent_initial_call=False,
    )
    def _update_volm(_n):
        try:
            b1, b2, buy15, sell15, th_shock, th_extreme = _volm_tables(ctx)
            hint = f"Thresholds (dynamic): Shock RVOL ≥ {th_shock:.2f} | Extreme RVOL ≥ {th_extreme:.2f}"
            return b1, b2, buy15, sell15, hint
        except Exception:
            return [], [], [], [], "Volm loading…"