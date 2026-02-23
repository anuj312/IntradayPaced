# web.py
#
# Dash page plugins for your main Dash app (app.py)
#
# Exposes:
#   - volm_page(BASE) -> layout
#   - register_volm(dash_app, BASE, ctx) -> registers callbacks
#   - fno_movers_page(BASE) -> layout
#   - register_fno_movers(dash_app, BASE, ctx) -> registers callbacks
#
# ctx contracts:
#   register_volm ctx must include:
#     LOCK, ALL_SYMBOLS, symbol_to_token, DAILY_STATS, get_live_or_eod_state, IST
#
#   register_fno_movers ctx must include:
#     ALL_SYMBOLS, IST

import os
import time
import threading
from datetime import datetime, date, timedelta
from typing import Dict, Any, Tuple, Optional, List

import pandas as pd
import dash
from dash import dcc, html, Input, Output
import dash_bootstrap_components as dbc
import dash_ag_grid as dag
from dash.exceptions import PreventUpdate

from kiteconnect import KiteConnect


# =============================================================================
# VOLM (Volume Shockers)  -- keep old logic as-is
# =============================================================================

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
    OLD VOLM DF (paced RVOL):
      RVOL = vol_today / (avg_vol_20 * time_factor)
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

            rng = max(0.0, hi - lo)
            day_range_pct = (rng / op) * 100.0

            pos_in_range = 0.5
            if rng > 1e-9:
                pos_in_range = (ltp - lo) / rng
                pos_in_range = float(min(max(pos_in_range, 0.0), 1.0))

            avg_range_pct_20 = (avg_range_20 / op) * 100.0 if op > 0 else 0.0

            range_exp_ok = day_range_pct >= (RANGE_EXP_MULT * max(0.0001, avg_range_pct_20))
            range_tight_ok = day_range_pct <= (RANGE_CONTR_MULT * max(0.0001, avg_range_pct_20))

            expected_vol = avg_vol_20 * tf
            rvol_paced = vol_today / (expected_vol + 1e-9)

            rows.append(
                {
                    "Symbol": sym,
                    "%Change": round(float(pct_open), 2),
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
    OLD VOLM TABLES (unchanged):
      breakout_rows, breakdown_rows, buy_rvol_rows, sell_rvol_rows, shock_th, extreme_th
    """
    df = _compute_volm_df(ctx)
    if df.empty:
        return [], [], [], [], 2.0, 3.0

    df["RVOL"] = pd.to_numeric(df["RVOL"], errors="coerce")
    df["%Change"] = pd.to_numeric(df["%Change"], errors="coerce")
    df = df.dropna(subset=["RVOL", "%Change"])
    if df.empty:
        return [], [], [], [], 2.0, 3.0

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


# =============================================================================
# NEW (VOLM PAGE): RVOL20 (UNPACED) Momentum + OI% column
# =============================================================================

_OI_PCT_CACHE: Dict[int, Tuple[Optional[float], float]] = {}
_OI_PCT_TTL_SEC = 6.0


def _compute_rvol20_unpaced_df(ctx: Dict[str, Any]) -> pd.DataFrame:
    """
    Build df with UNPACED momentum:
      Momentum = vol_today / avg_vol_20  (no time-factor)
    """
    LOCK = ctx["LOCK"]
    ALL_SYMBOLS = ctx["ALL_SYMBOLS"]
    symbol_to_token = ctx["symbol_to_token"]
    DAILY_STATS = ctx["DAILY_STATS"]
    get_live_or_eod_state = ctx["get_live_or_eod_state"]

    rows = []
    with LOCK:
        for sym in ALL_SYMBOLS:
            tok = symbol_to_token.get(sym)
            if not tok:
                continue

            st = DAILY_STATS.get(tok) or {}
            avg_vol_20 = _safe_float(st.get("avg_vol_20"))
            if avg_vol_20 is None:
                continue

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

            # same quality filters as volm
            if avg_vol_20 < MIN_AVG_VOL_20:
                continue
            if vol_today < MIN_TODAY_VOL:
                continue
            if ltp < MIN_LTP:
                continue

            pct_open = (ltp - op) / op * 100.0
            momentum = float(vol_today) / (float(avg_vol_20) + 1e-9)

            rows.append(
                {
                    "Symbol": sym,
                    "%Change": round(float(pct_open), 2),
                    "Momentum": float(momentum),
                    "_tok": int(tok),
                }
            )

    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _oi_pct_for_underlyings(ctx: Dict[str, Any], underlyings: List[str]) -> Dict[str, Optional[float]]:
    """
    For a list of NSE cash symbols, compute FUT OI% (near expiry) if available.
    Returns mapping: underlying -> oi_pct (float) or None.
    """
    if not underlyings:
        return {}

    # Load FUT universe (filtered to allowed symbols)
    futdf = _load_fno_futures_once(ctx)
    if futdf is None or futdf.empty:
        return {s: None for s in underlyings}

    near = _near_expiry_from_df(futdf, ctx["IST"])
    if not near:
        return {s: None for s in underlyings}

    dfe = futdf[futdf["expiry"] == near].copy()
    if dfe.empty:
        return {s: None for s in underlyings}

    # Ensure prev OI seeding is running (or done) for that expiry
    _ensure_prev_oi_seed(ctx, near)

    expiry_s = str(near)
    with FNO_LOCK:
        prev_oi_map = dict(PREV_OI_BY_EXPIRY.get(expiry_s) or {})

    # Map underlying -> (contract, fut_token)
    dfe = dfe[dfe["name"].isin(set(underlyings))].copy()
    if dfe.empty:
        return {s: None for s in underlyings}

    u2contract = dict(zip(dfe["name"].astype(str), dfe["tradingsymbol"].astype(str)))
    u2token = dict(zip(dfe["name"].astype(str), dfe["instrument_token"].astype(int)))

    # Use cache per FUT token (short TTL)
    now = time.time()
    out: Dict[str, Optional[float]] = {}
    need_quote: List[str] = []
    need_under: List[str] = []

    for u in underlyings:
        tok = u2token.get(u)
        if not tok:
            out[u] = None
            continue

        cached = _OI_PCT_CACHE.get(int(tok))
        if cached and cached[1] > now:
            out[u] = cached[0]
            continue

        c = u2contract.get(u)
        if not c:
            out[u] = None
            continue

        need_quote.append("NFO:" + c)
        need_under.append(u)

    if need_quote:
        q = _quote_many(need_quote, chunk_size=FNO_QUOTE_CHUNK)

        for u, key in zip(need_under, need_quote):
            tok = int(u2token.get(u) or 0)
            v = q.get(key) or {}
            oi_now = v.get("oi")
            oi_prev = prev_oi_map.get(tok)

            oi_pct = None
            if oi_now is not None and oi_prev is not None and int(oi_prev) != 0:
                try:
                    oi_chg = int(int(oi_now) - int(oi_prev))
                    oi_pct = (float(oi_chg) / float(int(oi_prev))) * 100.0
                except Exception:
                    oi_pct = None

            out[u] = oi_pct
            _OI_PCT_CACHE[tok] = (oi_pct, now + _OI_PCT_TTL_SEC)

    # fill any missing
    for u in underlyings:
        out.setdefault(u, None)

    return out


def _rvol20_momentum_tables(ctx: Dict[str, Any], top_n: int = 15) -> Tuple[list, list]:
    """
    NEW tables on Volm page:

      RVOL20 Momentum (UNPACED) + OI%

    Momentum = vol_today / avg_vol_20 (unpaced)
    OI% = near-month FUT OI% (prev-day OI -> current OI), if available.
    """
    df = _compute_rvol20_unpaced_df(ctx)
    if df.empty:
        return [], []

    df["Momentum"] = pd.to_numeric(df["Momentum"], errors="coerce")
    df["%Change"] = pd.to_numeric(df["%Change"], errors="coerce")
    df = df.dropna(subset=["Momentum", "%Change"])
    if df.empty:
        return [], []

    gainers_df = df[df["%Change"] > 0].sort_values("Momentum", ascending=False).head(int(top_n)).copy()
    losers_df = df[df["%Change"] < 0].sort_values("Momentum", ascending=False).head(int(top_n)).copy()

    # fetch OI% only for the displayed symbols (small set)
    symbols_needed = list(dict.fromkeys(gainers_df["Symbol"].tolist() + losers_df["Symbol"].tolist()))
    oi_map = _oi_pct_for_underlyings(ctx, symbols_needed)

    def _attach(d: pd.DataFrame) -> List[dict]:
        if d.empty:
            return []
        d["Momentum"] = d["Momentum"].astype(float).round(2)
        d["OI%"] = d["Symbol"].map(oi_map)
        d["OI%"] = pd.to_numeric(d["OI%"], errors="coerce").round(2)
        # keep None instead of NaN
        d["OI%"] = d["OI%"].where(pd.notnull(d["OI%"]), None)
        return d[["Symbol", "%Change", "Momentum", "OI%"]].to_dict("records")

    return _attach(gainers_df), _attach(losers_df)


# =============================================================================
# VOLM PAGE UI
# =============================================================================

def volm_page(BASE: str):
    # Existing Volm tables columns (unchanged)
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

    # NEW momentum tables columns: Symbol | %Change | Momentum | OI%
    pct_fmt = {
        "function": (
            "params.value==null ? '—' : "
            "((Number(params.value)>0?'+':'') + Number(params.value).toFixed(2) + '%')"
        )
    }
    oi_color = {
        "function": (
            "params.value==null ? {} : "
            "(Number(params.value)>0 ? {color:'var(--good)', fontWeight:'800'} : "
            "(Number(params.value)<0 ? {color:'var(--bad)', fontWeight:'800'} : {color:'rgba(255,255,255,0.9)', fontWeight:'800'}))"
        )
    }

    mom_cols = [
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
            "colId": "mom",
            "field": "Momentum",
            "headerName": "MOMENTUM (20D)",
            "type": "rightAligned",
            "minWidth": 165,
            "maxWidth": 185,
            "suppressSizeToFit": True,
            "headerClass": "ag-right-aligned-header h-right",
            "cellClass": "ag-right-aligned-cell cell-num c-right",
            "valueFormatter": {"function": "params.value==null ? '—' : (Number(params.value).toFixed(2) + 'x')"},
            # white + bold (as requested)
            "cellStyle": {"function": "params.value==null ? {} : ({color:'rgba(255,255,255,0.92)', fontWeight:'800'})"},
        },
        {
            "colId": "oi",
            "field": "OI%",
            "headerName": "OI%",
            "type": "rightAligned",
            "minWidth": 110,
            "maxWidth": 125,
            "suppressSizeToFit": True,
            "headerClass": "ag-right-aligned-header h-right",
            "cellClass": "ag-right-aligned-cell cell-num c-right",
            "valueFormatter": pct_fmt,
            "cellStyle": oi_color,
        },
    ]

    ROW_H = 34
    HDR_H = 34
    GRID_10ROWS_HEIGHT = f"{HDR_H + (10 * ROW_H) + 4}px"

    grid_opts = {
        "immutableData": True,
        "getRowId": {"function": "params.data.Symbol"},
        "alwaysShowVerticalScroll": False,
        "animateRows": False,
        "rowHeight": ROW_H,
        "headerHeight": HDR_H,
        "onGridReady": {"function": "params.api.sizeColumnsToFit();"},
        "onGridSizeChanged": {"function": "params.api.sizeColumnsToFit();"},
    }

    def grid(id_, coldefs_, height="min(420px, 42vh)"):
        return dag.AgGrid(
            id=id_,
            className="ag-theme-alpine-dark grid-wrap compact-grid",
            columnDefs=coldefs_,
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

            # NEW: RVOL20 (UNPACED) Momentum + OI%
            dbc.Row(
                [
                    dbc.Col(
                        [
                            html.H6("RVOL20 Momentum (Unpaced) — Gainers", className="mt-1"),
                            grid("volm-mom-gainers", mom_cols, height=GRID_10ROWS_HEIGHT),
                        ],
                        md=6,
                    ),
                    dbc.Col(
                        [
                            html.H6("RVOL20 Momentum (Unpaced) — Losers", className="mt-1"),
                            grid("volm-mom-losers", mom_cols, height=GRID_10ROWS_HEIGHT),
                        ],
                        md=6,
                    ),
                ],
                className="g-2",
            ),

            html.Hr(),

            # OLD: BUY / SELL RVOL
            dbc.Row(
                [
                    dbc.Col(
                        [
                            html.H6("Top 15 BUYING RVOL (RVOL high + %CHG ≥ 0)", className="mt-1"),
                            grid("volm-buy-rvol", cols, height=GRID_10ROWS_HEIGHT),
                        ],
                        md=6,
                    ),
                    dbc.Col(
                        [
                            html.H6("Top 15 SELLING RVOL (RVOL high + %CHG < 0)", className="mt-1"),
                            grid("volm-sell-rvol", cols, height=GRID_10ROWS_HEIGHT),
                        ],
                        md=6,
                    ),
                ],
                className="g-2",
            ),

            html.Hr(),

            # OLD: BREAKOUT / BREAKDOWN shockers
            dbc.Row(
                [
                    dbc.Col([html.H6("Breakout Vol Shockers", className="mt-1"), grid("volm-breakout", cols)], md=6),
                    dbc.Col([html.H6("Breakdown Vol Shockers", className="mt-1"), grid("volm-breakdown", cols)], md=6),
                ],
                className="g-2",
            ),
        ],
        className="page-wrap",
    )


def register_volm(dash_app, BASE: str, ctx: Dict[str, Any]) -> None:
    @dash_app.callback(
        Output("volm-mom-gainers", "rowData"),
        Output("volm-mom-losers", "rowData"),
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
            mom_g, mom_l = _rvol20_momentum_tables(ctx, top_n=15)

            b1, b2, buy15, sell15, th_shock, th_extreme = _volm_tables(ctx)
            now_txt = datetime.now(ctx["IST"]).strftime("%H:%M:%S")
            hint = f"[{now_txt}] Thresholds (dynamic): Shock RVOL ≥ {th_shock:.2f} | Extreme RVOL ≥ {th_extreme:.2f}"

            return mom_g, mom_l, b1, b2, buy15, sell15, hint
        except Exception:
            return [], [], [], [], [], [], "Volm loading…"


# =============================================================================
# F&O MOVERS (FUT) — Top Gainers / Losers / All
# =============================================================================

FNO_MOVERS_TTL_SEC = float(os.getenv("FNO_MOVERS_TTL_SEC", "6"))
FNO_PREV_OI_PACE_SEC = float(os.getenv("FNO_PREV_OI_PACE_SEC", "0.35"))
FNO_QUOTE_CHUNK = int(os.getenv("FNO_QUOTE_CHUNK", "350"))
FNO_MOVERS_TOP_N = int(os.getenv("FNO_MOVERS_TOP_N", "15"))

_KITE_API_KEY = os.getenv("KITE_API_KEY", "").strip()
_KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "").strip()
if not _KITE_API_KEY or not _KITE_ACCESS_TOKEN:
    raise RuntimeError("Missing KITE_API_KEY / KITE_ACCESS_TOKEN env vars (required for FNO movers).")

kite_fno = KiteConnect(api_key=_KITE_API_KEY)
kite_fno.set_access_token(_KITE_ACCESS_TOKEN)

REST_LOCK = threading.Lock()
FNO_LOCK = threading.RLock()

FNO_FUT_DF: Optional[pd.DataFrame] = None
PREV_OI_BY_EXPIRY: Dict[str, Dict[int, int]] = {}
PREV_OI_PROGRESS: Dict[str, Dict[str, Any]] = {}
MOVERS_CACHE: Dict[str, Tuple[dict, float]] = {}


def _chunk_list(xs: List[str], n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _quote_many(keys: List[str], chunk_size: int = FNO_QUOTE_CHUNK) -> dict:
    out: dict = {}
    for ch in _chunk_list(keys, chunk_size):
        with REST_LOCK:
            out.update(kite_fno.quote(ch))
    return out


def _load_fno_futures_once(ctx: Dict[str, Any]) -> pd.DataFrame:
    """Load NFO FUT instruments filtered to your Dash universe (ctx['ALL_SYMBOLS'])."""
    global FNO_FUT_DF
    with FNO_LOCK:
        if FNO_FUT_DF is not None and not FNO_FUT_DF.empty:
            return FNO_FUT_DF

    with REST_LOCK:
        df = pd.DataFrame(kite_fno.instruments("NFO"))

    df = df[(df["segment"] == "NFO-FUT") & (df["instrument_type"] == "FUT")].copy()
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date

    allowed = set(ctx["ALL_SYMBOLS"])
    if "name" in df.columns:
        df = df[df["name"].isin(allowed)].copy()
    else:
        df = df.iloc[0:0].copy()

    with FNO_LOCK:
        FNO_FUT_DF = df

    return df


def _near_expiry_from_df(df: pd.DataFrame, ist) -> Optional[date]:
    if df is None or df.empty:
        return None
    today = datetime.now(ist).date()
    exps = sorted({e for e in df["expiry"].dropna().tolist() if e >= today})
    return exps[0] if exps else None


def _fetch_prevday_oi(token: int, ist) -> Optional[int]:
    end_date = datetime.now(ist).date()
    frm = end_date - timedelta(days=12)
    to = end_date - timedelta(days=1)

    with REST_LOCK:
        candles = kite_fno.historical_data(
            instrument_token=int(token),
            from_date=frm,
            to_date=to,
            interval="day",
            oi=True,
        )

    if not candles:
        return None
    oi = candles[-1].get("oi")
    return int(oi) if oi is not None else None


def _build_up_label(price_change: float, oi_change: Optional[int]) -> str:
    if oi_change is None:
        return "NO CLEAR"
    if price_change > 0 and oi_change > 0:
        return "LONG BUILDUP"
    if price_change < 0 and oi_change > 0:
        return "SHORT BUILDUP"
    if price_change > 0 and oi_change < 0:
        return "SHORT COVERING"
    if price_change < 0 and oi_change < 0:
        return "LONG UNWINDING"
    return "NO CLEAR"


def _ensure_prev_oi_seed(ctx: Dict[str, Any], expiry_: date):
    """Background seed prev-day OI for all FUT tokens of this expiry (paced)."""
    expiry_s = str(expiry_)
    df = _load_fno_futures_once(ctx)
    tokens = [int(x) for x in df[df["expiry"] == expiry_]["instrument_token"].tolist()]

    with FNO_LOCK:
        prog = PREV_OI_PROGRESS.get(expiry_s) or {}
        if prog.get("running"):
            return
        if prog.get("done") and prog.get("total") and int(prog["done"]) >= int(prog["total"]):
            return

        PREV_OI_PROGRESS[expiry_s] = {
            "running": True,
            "done": 0,
            "total": len(tokens),
            "errors": 0,
            "updated_at": datetime.now(ctx["IST"]).isoformat(),
        }

    def _run():
        cache: Dict[int, int] = {}
        done = 0
        err = 0
        for tok in tokens:
            try:
                oi_prev = _fetch_prevday_oi(tok, ctx["IST"])
                if oi_prev is not None:
                    cache[int(tok)] = int(oi_prev)
            except Exception:
                err += 1

            done += 1
            with FNO_LOCK:
                PREV_OI_PROGRESS[expiry_s]["done"] = done
                PREV_OI_PROGRESS[expiry_s]["errors"] = err
                PREV_OI_PROGRESS[expiry_s]["updated_at"] = datetime.now(ctx["IST"]).isoformat()

            time.sleep(FNO_PREV_OI_PACE_SEC)

        with FNO_LOCK:
            PREV_OI_BY_EXPIRY[expiry_s] = cache
            PREV_OI_PROGRESS[expiry_s]["running"] = False
            PREV_OI_PROGRESS[expiry_s]["updated_at"] = datetime.now(ctx["IST"]).isoformat()

    threading.Thread(target=_run, daemon=True).start()


def _rows_for_expiry(ctx: Dict[str, Any], expiry_: date) -> Tuple[List[dict], Dict[str, Any]]:
    df = _load_fno_futures_once(ctx)
    dfe = df[df["expiry"] == expiry_].copy()
    if dfe.empty:
        return [], {}

    _ensure_prev_oi_seed(ctx, expiry_)

    keys = ["NFO:" + s for s in dfe["tradingsymbol"].tolist()]
    q = _quote_many(keys, chunk_size=FNO_QUOTE_CHUNK)

    expiry_s = str(expiry_)
    with FNO_LOCK:
        prev_oi_map = dict(PREV_OI_BY_EXPIRY.get(expiry_s) or {})
        prog = dict(PREV_OI_PROGRESS.get(expiry_s) or {})

    rows: List[dict] = []
    for _, r in dfe.iterrows():
        tsym = str(r["tradingsymbol"])
        v = q.get("NFO:" + tsym) or {}

        ltp = v.get("last_price")
        ohlc = v.get("ohlc") or {}
        prev_close = ohlc.get("close")
        vol = v.get("volume")
        oi = v.get("oi")

        if ltp is None or prev_close is None:
            continue

        try:
            ltp_f = float(ltp)
            prev_close_f = float(prev_close)
        except Exception:
            continue
        if prev_close_f == 0:
            continue

        price_chg = ltp_f - prev_close_f
        price_pct = (price_chg / prev_close_f) * 100.0

        tok = int(r["instrument_token"])
        oi_prev = prev_oi_map.get(tok)

        oi_pct = None
        build_up = "NO CLEAR"
        if oi is not None and oi_prev is not None and int(oi_prev) != 0:
            try:
                oi_now = int(oi)
                oi_prev_i = int(oi_prev)
                oi_chg = int(oi_now - oi_prev_i)
                oi_pct = (float(oi_chg) / float(oi_prev_i)) * 100.0
                build_up = _build_up_label(price_chg, oi_chg)
            except Exception:
                oi_pct = None
                build_up = "NO CLEAR"

        underlying = str(r.get("name") or "")
        rows.append(
            {
                "Symbol": underlying if underlying else tsym,
                "Contract": tsym,
                "Price": round(ltp_f, 2),
                "%Chg": round(price_pct, 2),
                "Contracts": int(vol or 0),
                "OI%": (round(float(oi_pct), 2) if oi_pct is not None else None),
                "BuildUp": build_up,
            }
        )

    return rows, prog


def compute_fno_movers_payload(ctx: Dict[str, Any], expiry_: date, top_n: int = FNO_MOVERS_TOP_N) -> dict:
    cache_key = f"{expiry_}:{int(top_n)}"
    now = time.time()

    with FNO_LOCK:
        cached = MOVERS_CACHE.get(cache_key)
        if cached and cached[1] > now:
            return cached[0]

    rows, prog = _rows_for_expiry(ctx, expiry_)
    if not rows:
        payload = {
            "expiry": str(expiry_),
            "updated_at": datetime.now(ctx["IST"]).isoformat(),
            "rows": [],
            "gainers": [],
            "losers": [],
            "prev_oi_progress": prog,
        }
        with FNO_LOCK:
            MOVERS_CACHE[cache_key] = (payload, now + FNO_MOVERS_TTL_SEC)
        return payload

    dfr = pd.DataFrame(rows)

    full_sorted = dfr.sort_values(["Contracts", "%Chg"], ascending=[False, False]).to_dict("records")
    gainers = (
        dfr[dfr["%Chg"] > 0]
        .sort_values(["Contracts", "%Chg"], ascending=[False, False])
        .head(int(top_n))
        .to_dict("records")
    )
    losers = (
        dfr[dfr["%Chg"] < 0]
        .sort_values(["Contracts", "%Chg"], ascending=[False, True])
        .head(int(top_n))
        .to_dict("records")
    )

    payload = {
        "expiry": str(expiry_),
        "updated_at": datetime.now(ctx["IST"]).isoformat(),
        "rows": full_sorted,
        "gainers": gainers,
        "losers": losers,
        "prev_oi_progress": prog,
    }

    with FNO_LOCK:
        MOVERS_CACHE[cache_key] = (payload, now + FNO_MOVERS_TTL_SEC)

    return payload


def fno_movers_page(BASE: str):
    pct_fmt = {
        "function": (
            "params.value==null ? '—' : "
            "((Number(params.value)>0?'+':'') + Number(params.value).toFixed(2) + '%')"
        )
    }
    int_fmt = {"function": "params.value==null?'—':Number(params.value).toLocaleString('en-IN')"}
    signed_color = {
        "function": (
            "params.value==null ? {} : "
            "(Number(params.value)>0 ? {color:'var(--good)'} : "
            "(Number(params.value)<0 ? {color:'var(--bad)'} : {}))"
        )
    }

    ROW_H = 34
    HDR_H = 34
    TOP_VISIBLE_ROWS = 15
    TOP_GRID_HEIGHT = f"{HDR_H + (TOP_VISIBLE_ROWS * ROW_H) + 6}px"

    top_coldefs = [
        {"field": "Symbol", "headerName": "SYMBOL", "pinned": "left", "minWidth": 170, "flex": 1, "cellRenderer": "SymbolCell"},
        {"field": "%Chg", "headerName": "%CHANGE", "type": "rightAligned", "minWidth": 120, "valueFormatter": pct_fmt, "cellStyle": signed_color},
        {"field": "Contracts", "headerName": "CONTRACTS", "type": "rightAligned", "minWidth": 140, "valueFormatter": int_fmt},
        {"field": "OI%", "headerName": "OI%", "type": "rightAligned", "minWidth": 100, "valueFormatter": pct_fmt, "cellStyle": signed_color},
    ]

    all_coldefs = top_coldefs + [
        {"field": "BuildUp", "headerName": "BUILD UP", "minWidth": 170, "flex": 1},
        {"field": "Contract", "headerName": "CONTRACT", "minWidth": 190, "flex": 1},
        {"field": "Price", "headerName": "PRICE", "type": "rightAligned", "minWidth": 110,
         "valueFormatter": {"function": "params.value==null?'—':Number(params.value).toFixed(2)"}},
    ]

    grid_opts = {
        "immutableData": True,
        "getRowId": {"function": "params.data.Contract || params.data.Symbol"},
        "alwaysShowVerticalScroll": True,
        "animateRows": False,
        "rowHeight": ROW_H,
        "headerHeight": HDR_H,
        "onGridReady": {"function": "params.api.sizeColumnsToFit();"},
        "onGridSizeChanged": {"function": "params.api.sizeColumnsToFit();"},
    }

    def grid(id_, coldefs, height):
        return dag.AgGrid(
            id=id_,
            className="ag-theme-alpine-dark grid-wrap compact-grid",
            columnDefs=coldefs,
            rowData=[],
            defaultColDef={"sortable": True, "filter": True, "resizable": True},
            dashGridOptions=grid_opts,
            style={"height": height, "width": "100%"},
        )

    return html.Div(
        [
            dcc.Interval(id="refresh_fno_movers", interval=4000, n_intervals=0),
            dcc.Interval(id="refresh_fno_expiries", interval=30000, n_intervals=0),

            dbc.Row(
                [
                    dbc.Col(html.Div([html.Div("F&O MOVERS", className="fno-title-kicker")], className="fno-title-wrap"), width=True),
                    dbc.Col(dbc.Button("Back", href=f"{BASE}", color="secondary", outline=True, className="btn-back"), width="auto"),
                ],
                className="align-items-center g-2",
            ),

            dbc.Row(
                [
                    dbc.Col(
                        html.Div(
                            [
                                html.Div("EXPIRY", className="fno-meta-label"),
                                dbc.Select(
                                    id="fno_expiry_sel",
                                    options=[{"label": "Loading…", "value": ""}],
                                    value="",
                                    className="fno-expiry-select",
                                ),
                            ],
                            className="fno-expiry-wrap",
                        ),
                        md=3,
                    ),
                    dbc.Col(html.Div(id="fno_seed_status", className="fno-meta-chip"), md=5),
                    dbc.Col(html.Div(id="fno_updated_at", className="fno-meta-chip"), md=4),
                ],
                className="g-2 mb-2 fno-meta-row",
            ),

            dbc.Row(
                [
                    dbc.Col([html.H6("Top Gainers (30 rows, scroll)", className="mt-1 fno-section-title"),
                             grid("fno_gainers_grid", top_coldefs, TOP_GRID_HEIGHT)], md=6),
                    dbc.Col([html.H6("Top Losers (30 rows, scroll)", className="mt-1 fno-section-title"),
                             grid("fno_losers_grid", top_coldefs, TOP_GRID_HEIGHT)], md=6),
                ],
                className="g-2",
            ),

            html.Hr(),

            html.H6("All (sorted by Contracts)", className="mt-1 fno-section-title"),
            grid("fno_all_grid", all_coldefs, "min(560px, 52vh)"),
        ],
        className="page-wrap",
    )


def register_fno_movers(dash_app, BASE: str, ctx: Dict[str, Any]) -> None:
    @dash_app.callback(
        Output("fno_expiry_sel", "options"),
        Output("fno_expiry_sel", "value"),
        Input("refresh_fno_expiries", "n_intervals"),
        Input("url", "pathname"),
        prevent_initial_call=False,
    )
    def _load_expiries(_n, pathname):
        pn = (pathname or "").strip()
        if pn not in (f"{BASE}fnomovers", f"{BASE}fnomovers/"):
            raise PreventUpdate

        df = _load_fno_futures_once(ctx)
        near = _near_expiry_from_df(df, ctx["IST"])

        today = datetime.now(ctx["IST"]).date()
        exps = sorted({e for e in df["expiry"].dropna().tolist() if e >= today})
        opts = [{"label": str(e), "value": str(e)} for e in exps]

        val = str(near) if near else (opts[0]["value"] if opts else "")
        return (opts if opts else [{"label": "No expiries", "value": ""}]), val

    def _fmt_updated_ist(iso: Optional[str]) -> Tuple[str, str]:
        if not iso:
            return "—", ""
        try:
            dt = datetime.fromisoformat(str(iso))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ctx["IST"])
            dt_ist = dt.astimezone(ctx["IST"])
            pretty = dt_ist.strftime("%d %b %H:%M:%S") + " IST"
            return pretty, str(iso)
        except Exception:
            return str(iso), str(iso)

    @dash_app.callback(
        Output("fno_gainers_grid", "rowData"),
        Output("fno_losers_grid", "rowData"),
        Output("fno_all_grid", "rowData"),
        Output("fno_seed_status", "children"),
        Output("fno_updated_at", "children"),
        Input("refresh_fno_movers", "n_intervals"),
        Input("fno_expiry_sel", "value"),
        Input("url", "pathname"),
        prevent_initial_call=False,
    )
    def _refresh(_n, expiry_s, pathname):
        pn = (pathname or "").strip()
        if pn not in (f"{BASE}fnomovers", f"{BASE}fnomovers/"):
            raise PreventUpdate

        if not expiry_s:
            return [], [], [], "—", "—"

        try:
            exp = date.fromisoformat(expiry_s)
        except Exception:
            return [], [], [], "Invalid expiry", "—"

        payload = compute_fno_movers_payload(ctx, exp, top_n=30)

        prog = payload.get("prev_oi_progress") or {}
        running = bool(prog.get("running"))
        done = int(prog.get("done") or 0)
        total = int(prog.get("total") or 0)
        errors = int(prog.get("errors") or 0)

        if total <= 0:
            seed_children = html.Span("—")
        else:
            if errors > 0:
                pill = html.Span(f"ERR {errors}", className="fno-status-pill bad")
            elif running:
                pill = html.Span("SEEDING…", className="fno-status-pill warn")
            else:
                pill = html.Span("READY", className="fno-status-pill good")

            seed_children = html.Span(
                [
                    html.Span(f"{done:,}/{total:,}", className="fno-meta-value"),
                    pill,
                ],
                className="fno-meta-inline",
            )

        updated_iso = payload.get("updated_at")
        updated_txt, updated_title = _fmt_updated_ist(updated_iso)
        updated_children = html.Span(updated_txt, title=updated_title)

        return (
            payload.get("gainers", []),
            payload.get("losers", []),
            payload.get("rows", []),
            seed_children,
            updated_children,
        )