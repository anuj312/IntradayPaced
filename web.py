
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
#
# Notes:
# - FNO prev-day OI seeding is done ONLY in app.py via fnoseed.
# - This module only READS fnoseed state.

import os
import time
import threading
from datetime import datetime, date, timedelta
from typing import Dict, Any, Tuple, Optional, List

import pandas as pd
from dash import dcc, html, Input, Output
import dash_bootstrap_components as dbc
import dash_ag_grid as dag
from dash.exceptions import PreventUpdate

from kiteconnect import KiteConnect

import fnoseed


# =============================================================================
# VOLM (Cash)
# =============================================================================

MIN_AVG_VOL_20 = 50_000
MIN_TODAY_VOL = 10_000
MIN_LTP = 20.0

BREAKOUT_PCT_TH = 0.60
BREAKDOWN_PCT_TH = -0.60
POS_NEAR_HIGH_TH = 0.80
POS_NEAR_LOW_TH = 0.20

RANGE_EXP_MULT = 1.20
RANGE_CONTR_MULT = 0.90
VOLM_FUT_TOPK = int(os.getenv("VOLM_FUT_TOPK", "60"))  # compute avg20 only for top-K FUT volumes

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
    Cash-volm dataframe (paced RVOL vs 20D avg volume):
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

            op = _safe_float((ohlc or {}).get("open"))
            hi = _safe_float((ohlc or {}).get("high"))
            lo = _safe_float((ohlc or {}).get("low"))

            if ltp is None or vol_today is None or op is None or op <= 0:
                continue
            if hi is None or lo is None:
                continue
            if avg_vol_20 is None or avg_range_20 is None:
                continue

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
# FNO REST (quotes + FUT avg20 volume) — prev-OI seed is read from fnoseed
# =============================================================================

FNO_MOVERS_TTL_SEC = float(os.getenv("FNO_MOVERS_TTL_SEC", "6"))
FNO_QUOTE_CHUNK = int(os.getenv("FNO_QUOTE_CHUNK", "350"))

_KITE_API_KEY = os.getenv("KITE_API_KEY", "").strip()
_KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "").strip()
if not _KITE_API_KEY or not _KITE_ACCESS_TOKEN:
    raise RuntimeError("Missing KITE_API_KEY / KITE_ACCESS_TOKEN env vars.")

kite_fno = KiteConnect(api_key=_KITE_API_KEY)
kite_fno.set_access_token(_KITE_ACCESS_TOKEN)

REST_LOCK = threading.Lock()
FNO_LOCK = threading.RLock()

# Local FUT universe fallback cache (prefer fnoseed.FNO_FUT_DF if present)
FNO_FUT_DF: Optional[pd.DataFrame] = None

# Movers payload cache
MOVERS_CACHE: Dict[str, Tuple[dict, float]] = {}

# FUT avg 20D volume cache (used by Volm FUT Momentum table)
FUT_AVGVOL20_CACHE: Dict[int, Tuple[Optional[float], float]] = {}


def _chunk_list(xs: List[str], n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _quote_many(keys: List[str], chunk_size: int = FNO_QUOTE_CHUNK) -> dict:
    out: dict = {}
    for ch in _chunk_list(keys, chunk_size):
        with REST_LOCK:
            out.update(kite_fno.quote(ch))
    return out


def _cache_expiry_eod_ist(ist) -> float:
    now = datetime.now(ist)
    eod = now.replace(hour=23, minute=59, second=0, microsecond=0)
    return eod.timestamp()


def fut_avg_vol_20d(token: int, ist) -> Optional[float]:
    """Avg daily volume of last 20 completed sessions for this FUT token (min 5 sessions)."""
    now = time.time()
    cached = FUT_AVGVOL20_CACHE.get(int(token))
    if cached and cached[1] > now:
        return cached[0]

    avg20: Optional[float]
    try:
        to_dt = datetime.now(ist)
        from_dt = to_dt - timedelta(days=120)

        with REST_LOCK:
            candles = kite_fno.historical_data(
                instrument_token=int(token),
                from_date=from_dt,
                to_date=to_dt,
                interval="day",
                continuous=False,
                oi=False,
            )

        df = pd.DataFrame(candles or [])
        if df.empty:
            avg20 = None
        else:
            df["date"] = pd.to_datetime(df["date"])
            df["d"] = df["date"].dt.date
            today = datetime.now(ist).date()
            if len(df) and df.iloc[-1]["d"] == today:
                df = df.iloc[:-1].copy()

            vols = pd.to_numeric(df["volume"], errors="coerce").dropna().tail(20)
            avg20 = float(vols.mean()) if len(vols) >= 5 else None
    except Exception:
        avg20 = None

    FUT_AVGVOL20_CACHE[int(token)] = (avg20, _cache_expiry_eod_ist(ist))
    return avg20


def _load_fno_futures_once(ctx: Dict[str, Any]) -> pd.DataFrame:
    """
    Load NFO FUT instruments filtered to allowed underlyings.
    Prefer fnoseed.FNO_FUT_DF if already loaded by app.py, else load here.
    """
    global FNO_FUT_DF

    with fnoseed.state_lock:
        df_seed = fnoseed.FNO_FUT_DF
        if df_seed is not None and not df_seed.empty:
            return df_seed

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


def _is_page(pathname: Optional[str], slug: str) -> bool:
    """
    Robust pathname check because Dash can sometimes report /fnomovers instead of /dash/fnomovers
    depending on routing configuration.
    """
    pn = (pathname or "").strip().rstrip("/")
    return pn.endswith("/" + slug) or pn == ("/" + slug)


# =============================================================================
# VOLM PAGE — FUT Momentum + OI% (reads prev-oi from fnoseed)
# =============================================================================

def _compute_rvol20_unpaced_df(ctx: Dict[str, Any]) -> pd.DataFrame:
    """
    Momentum(20D) = near-month FUT volume(today) / avg20 FUT daily volume
    OI% = near-month FUT OI% vs prev-day OI (from fnoseed)
    %Change = CASH % from open
    Optimization: only consider TOP-K near-expiry futures by current FUT volume,
    which massively reduces historical_data() calls on first load.
    """
    IST = ctx["IST"]
    ALL_SYMBOLS = ctx["ALL_SYMBOLS"]
    symbol_to_token = ctx["symbol_to_token"]
    get_live_or_eod_state = ctx["get_live_or_eod_state"]
    LOCK = ctx["LOCK"]

    futdf = _load_fno_futures_once(ctx)
    near = _near_expiry_from_df(futdf, IST)
    if not near:
        return pd.DataFrame()

    dfe = futdf[futdf["expiry"] == near].copy()
    if dfe.empty:
        return pd.DataFrame()

    expiry_s = str(near)
    with fnoseed.state_lock:
        prev_oi_map = dict(fnoseed.PREV_OI_BY_EXPIRY.get(expiry_s) or {})

    # Quote all near-expiry futures once
    keys = ["NFO:" + s for s in dfe["tradingsymbol"].astype(str).tolist()]
    q = _quote_many(keys, chunk_size=FNO_QUOTE_CHUNK)

    # Build (vol, underlying, fut_token, fut_tsym) list, then keep TOP-K by FUT volume
    fut_rows: List[Tuple[int, str, int, str]] = []
    for _, r in dfe.iterrows():
        underlying = str(r.get("name") or "")
        fut_token = int(r["instrument_token"])
        fut_tsym = str(r["tradingsymbol"])

        v = q.get("NFO:" + fut_tsym) or {}
        fut_vol = int(v.get("volume") or 0)

        if underlying:
            fut_rows.append((fut_vol, underlying, fut_token, fut_tsym))

    if not fut_rows:
        return pd.DataFrame()

    fut_rows.sort(key=lambda x: x[0], reverse=True)
    fut_rows = fut_rows[: max(5, int(VOLM_FUT_TOPK))]

    # Maps for quick lookup
    u2fut_token: Dict[str, int] = {u: tok for _vol, u, tok, _tsym in fut_rows}
    u2fut_sym: Dict[str, str] = {u: tsym for _vol, u, _tok, tsym in fut_rows}

    rows = []
    with LOCK:
        # Only loop underlyings that we actually kept (top-K)
        for sym in u2fut_token.keys():
            # CASH state
            cash_token = symbol_to_token.get(sym)
            if not cash_token:
                continue

            st = get_live_or_eod_state(cash_token)
            if not st:
                continue

            ltp, _vol_cash, ohlc = st
            op = _safe_float((ohlc or {}).get("open"))
            ltp = _safe_float(ltp)
            if op is None or op <= 0 or ltp is None or ltp <= 0:
                continue
            if ltp < MIN_LTP:
                continue

            pct_open = (ltp - op) / op * 100.0

            # FUT quote
            fut_token = u2fut_token.get(sym)
            fut_tsym = u2fut_sym.get(sym)
            if not fut_token or not fut_tsym:
                continue

            v = q.get("NFO:" + fut_tsym) or {}
            fut_vol = v.get("volume")
            fut_oi_now = v.get("oi")
            if fut_vol is None:
                continue

            # Avg20 FUT volume (cached for the day)
            avg20_fut = fut_avg_vol_20d(int(fut_token), IST)
            if not avg20_fut or avg20_fut <= 0:
                continue

            momentum = float(fut_vol) / (float(avg20_fut) + 1e-9)

            # OI% vs prev OI seeded by fnoseed
            oi_pct = None
            oi_prev = prev_oi_map.get(int(fut_token))
            if fut_oi_now is not None and oi_prev is not None and int(oi_prev) != 0:
                try:
                    oi_chg = int(int(fut_oi_now) - int(oi_prev))
                    oi_pct = (float(oi_chg) / float(int(oi_prev))) * 100.0
                except Exception:
                    oi_pct = None

            rows.append(
                {
                    "Symbol": sym,
                    "%Change": round(float(pct_open), 2),
                    "Momentum": float(momentum),
                    "OI%": (round(float(oi_pct), 2) if oi_pct is not None else None),
                }
            )

    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _rvol20_momentum_tables(ctx: Dict[str, Any], top_n: int = 15) -> Tuple[list, list]:
    df = _compute_rvol20_unpaced_df(ctx)
    if df.empty:
        return [], []

    df["Momentum"] = pd.to_numeric(df["Momentum"], errors="coerce")
    df["%Change"] = pd.to_numeric(df["%Change"], errors="coerce")
    df = df.dropna(subset=["Momentum", "%Change"])
    if df.empty:
        return [], []

    gainers = df[df["%Change"] > 0].sort_values("Momentum", ascending=False).head(int(top_n)).copy()
    losers = df[df["%Change"] < 0].sort_values("Momentum", ascending=False).head(int(top_n)).copy()

    gainers["Momentum"] = gainers["Momentum"].astype(float).round(2)
    losers["Momentum"] = losers["Momentum"].astype(float).round(2)

    gainers["OI%"] = gainers["OI%"].where(pd.notnull(gainers["OI%"]), None)
    losers["OI%"] = losers["OI%"].where(pd.notnull(losers["OI%"]), None)

    return (
        gainers[["Symbol", "%Change", "Momentum", "OI%"]].to_dict("records"),
        losers[["Symbol", "%Change", "Momentum", "OI%"]].to_dict("records"),
    )


# =============================================================================
# VOLM PAGE UI
# =============================================================================

def volm_page(BASE: str):
    cols = [
        {"colId": "stock", "field": "Symbol", "headerName": "STOCK", "cellRenderer": "SymbolCell",
         "flex": 1, "minWidth": 160, "headerClass": "h-left", "cellClass": "c-left"},
        {"colId": "pct", "field": "%Change", "headerName": "%CHG", "cellRenderer": "PctPill",
         "minWidth": 130, "maxWidth": 150, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right", "cellClass": "ag-right-aligned-cell cell-num c-right"},
        {"colId": "rvol", "field": "RVOL", "headerName": "RVOL", "cellRenderer": "RfactorPill",
         "minWidth": 120, "maxWidth": 150, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right", "cellClass": "ag-right-aligned-cell cell-num c-right",
         "valueFormatter": {"function": "fmt2(params.value)"}},
        {"colId": "vol", "field": "Vol", "headerName": "VOLUME", "cellRenderer": "VolPill",
         "minWidth": 140, "maxWidth": 180, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right", "cellClass": "ag-right-aligned-cell cell-num c-right"},
    ]

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
            "(Number(params.value)<0 ? {color:'var(--bad)', fontWeight:'800'} : "
            "{color:'rgba(255,255,255,0.9)', fontWeight:'800'}))"
        )
    }

    mom_cols = [
        {"colId": "stock", "field": "Symbol", "headerName": "STOCK", "cellRenderer": "SymbolCell",
         "flex": 1, "minWidth": 160, "headerClass": "h-left", "cellClass": "c-left"},
        {"colId": "pct", "field": "%Change", "headerName": "%CHG", "cellRenderer": "PctPill",
         "minWidth": 130, "maxWidth": 150, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right", "cellClass": "ag-right-aligned-cell cell-num c-right"},
        {"colId": "mom", "field": "Momentum", "headerName": "MOMENTUM (FUT 20D)", "type": "rightAligned",
         "minWidth": 185, "maxWidth": 205, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right", "cellClass": "ag-right-aligned-cell cell-num c-right",
         "valueFormatter": {"function": "params.value==null ? '—' : (Number(params.value).toFixed(2) + 'x')"},
         "cellStyle": {"function": "params.value==null ? {} : ({color:'rgba(255,255,255,0.92)', fontWeight:'800'})"}},
        {"colId": "oi", "field": "OI%", "headerName": "OI%", "type": "rightAligned",
         "minWidth": 110, "maxWidth": 125, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right", "cellClass": "ag-right-aligned-cell cell-num c-right",
         "valueFormatter": pct_fmt, "cellStyle": oi_color},
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
                    dbc.Col(dbc.Button("Back", href=f"{BASE}", color="secondary", outline=True, className="btn-back"), width="auto"),
                ],
                className="align-items-center g-2",
            ),
            html.Div(id="volm-thresholds", className="hint", style={"marginBottom": "10px"}),

            dbc.Row(
                [
                    dbc.Col([html.H6("RVOL20 Momentum (FUT Unpaced) — Gainers", className="mt-1"),
                             grid("volm-mom-gainers", mom_cols, height=GRID_10ROWS_HEIGHT)], md=6),
                    dbc.Col([html.H6("RVOL20 Momentum (FUT Unpaced) — Losers", className="mt-1"),
                             grid("volm-mom-losers", mom_cols, height=GRID_10ROWS_HEIGHT)], md=6),
                ],
                className="g-2",
            ),

            html.Hr(),

            dbc.Row(
                [
                    dbc.Col([html.H6("Top 15 BUYING RVOL (RVOL high + %CHG ≥ 0)", className="mt-1"),
                             grid("volm-buy-rvol", cols, height=GRID_10ROWS_HEIGHT)], md=6),
                    dbc.Col([html.H6("Top 15 SELLING RVOL (RVOL high + %CHG < 0)", className="mt-1"),
                             grid("volm-sell-rvol", cols, height=GRID_10ROWS_HEIGHT)], md=6),
                ],
                className="g-2",
            ),

            html.Hr(),

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
# FNO MOVERS PAGE (Top Gainers/Losers only — NO "All" table)
# =============================================================================

def fno_movers_page(BASE: str):
    pct_fmt = {
        "function": (
            "params.value==null ? '—' : "
            "((Number(params.value)>0?'+':'') + Number(params.value).toFixed(2) + '%')"
        )
    }
    int_fmt = {"function": "params.value==null?'—':Number(params.value).toLocaleString('en-IN')"}

    signed_bold = {
        "function": (
            "params.value==null ? {} : "
            "(Number(params.value)>0 ? {color:'var(--good)', fontWeight:'800'} : "
            "(Number(params.value)<0 ? {color:'var(--bad)', fontWeight:'800'} : "
            "{color:'rgba(255,255,255,0.90)', fontWeight:'600'}))"
        )
    }
    white_bold = {"function": "params.value==null ? {} : ({color:'rgba(255,255,255,0.92)', fontWeight:'800'})"}

    ROW_H = 34
    HDR_H = 34
    TOP_VISIBLE_ROWS = 15
    TOP_GRID_HEIGHT = f"{HDR_H + (TOP_VISIBLE_ROWS * ROW_H) + 6}px"

    top_coldefs = [
        {"field": "Symbol", "headerName": "SYMBOL", "pinned": "left", "minWidth": 170, "flex": 1, "cellRenderer": "SymbolCell"},
        {"field": "%Chg", "headerName": "%CHANGE", "type": "rightAligned", "minWidth": 120, "valueFormatter": pct_fmt, "cellStyle": signed_bold},
        {"field": "Contracts", "headerName": "CONTRACTS", "type": "rightAligned", "minWidth": 140, "valueFormatter": int_fmt, "cellStyle": white_bold},
        {"field": "OI%", "headerName": "OI%", "type": "rightAligned", "minWidth": 100, "valueFormatter": pct_fmt, "cellStyle": signed_bold},
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
        if not _is_page(pathname, "fnomovers"):
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
        Output("fno_seed_status", "children"),
        Output("fno_updated_at", "children"),
        Input("refresh_fno_movers", "n_intervals"),
        Input("fno_expiry_sel", "value"),
        Input("url", "pathname"),
        prevent_initial_call=False,
    )
    def _refresh(_n, expiry_s, pathname):
        if not _is_page(pathname, "fnomovers"):
            raise PreventUpdate

        if not expiry_s:
            return [], [], "—", "—"

        try:
            exp = date.fromisoformat(expiry_s)
        except Exception:
            return [], [], "Invalid expiry", "—"

        payload = _compute_fno_movers_payload_internal(ctx, exp, top_n=30)

        # Seed status (single source: fnoseed)
        with fnoseed.state_lock:
            prog = dict(fnoseed.PREV_OI_PROGRESS.get(str(exp)) or {})
            last_err = fnoseed.LAST_ERROR

        if last_err and not prog:
            seed_children = html.Span(
                f"ERR: {str(last_err)[:140]}",
                style={"color": "var(--bad)", "fontWeight": "800"},
            )
        else:
            running = bool(prog.get("running"))
            done = int(prog.get("done") or 0)
            total = int(prog.get("total") or 0)
            errors = int(prog.get("errors") or 0)

            if total <= 0:
                seed_children = html.Span("NOT SEEDED", style={"color": "rgba(255,255,255,0.65)", "fontWeight": "800"})
            else:
                if errors > 0:
                    pill = html.Span(f"ERR {errors}", className="fno-status-pill bad")
                elif running:
                    pill = html.Span("SEEDING…", className="fno-status-pill warn")
                else:
                    pill = html.Span("READY", className="fno-status-pill good")

                seed_children = html.Span(
                    [html.Span(f"{done:,}/{total:,}", className="fno-meta-value"), pill],
                    className="fno-meta-inline",
                )

        updated_txt, updated_title = _fmt_updated_ist(payload.get("updated_at"))
        updated_children = html.Span(updated_txt, title=updated_title)

        return payload.get("gainers", []), payload.get("losers", []), seed_children, updated_children


def _compute_fno_movers_payload_internal(ctx: Dict[str, Any], expiry_: date, top_n: int = 30) -> dict:
    """
    Top gainers/losers payload only (NO All table).
    Fast: uses quote() + (optional) fnoseed baseline map.
    """
    cache_key = f"{expiry_}:{int(top_n)}"
    now = time.time()

    with FNO_LOCK:
        cached = MOVERS_CACHE.get(cache_key)
        if cached and cached[1] > now:
            return cached[0]

    df = _load_fno_futures_once(ctx)
    dfe = df[df["expiry"] == expiry_].copy()
    if dfe.empty:
        payload = {
            "expiry": str(expiry_),
            "updated_at": datetime.now(ctx["IST"]).isoformat(),
            "gainers": [],
            "losers": [],
            "note": "No FUT rows for this expiry.",
        }
        with FNO_LOCK:
            MOVERS_CACHE[cache_key] = (payload, now + FNO_MOVERS_TTL_SEC)
        return payload

    expiry_s = str(expiry_)
    with fnoseed.state_lock:
        prev_oi_map = dict(fnoseed.PREV_OI_BY_EXPIRY.get(expiry_s) or {})

    keys = ["NFO:" + s for s in dfe["tradingsymbol"].astype(str).tolist()]
    q = _quote_many(keys, chunk_size=FNO_QUOTE_CHUNK)

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

        fut_token = int(r["instrument_token"])
        oi_prev = prev_oi_map.get(fut_token)

        oi_pct = None
        if oi is not None and oi_prev is not None and int(oi_prev) != 0:
            try:
                oi_now = int(oi)
                oi_prev_i = int(oi_prev)
                oi_chg = int(oi_now - oi_prev_i)
                oi_pct = (float(oi_chg) / float(oi_prev_i)) * 100.0
            except Exception:
                oi_pct = None

        underlying = str(r.get("name") or "")
        rows.append(
            {
                "Symbol": underlying if underlying else tsym,
                "Contract": tsym,
                "%Chg": round(float(price_pct), 2),
                "Contracts": int(vol or 0),
                "OI%": (round(float(oi_pct), 2) if oi_pct is not None else None),
            }
        )

    if not rows:
        payload = {
            "expiry": str(expiry_),
            "updated_at": datetime.now(ctx["IST"]).isoformat(),
            "gainers": [],
            "losers": [],
            "note": "No quote rows yet.",
        }
        with FNO_LOCK:
            MOVERS_CACHE[cache_key] = (payload, now + FNO_MOVERS_TTL_SEC)
        return payload

    dfr = pd.DataFrame(rows)

    gainers_df = (
        dfr[dfr["%Chg"] > 0]
        .sort_values(["Contracts", "%Chg"], ascending=[False, False])
        .head(int(top_n))
        .copy()
    )
    losers_df = (
        dfr[dfr["%Chg"] < 0]
        .sort_values(["Contracts", "%Chg"], ascending=[False, True])
        .head(int(top_n))
        .copy()
    )

    gainers_df = gainers_df.where(pd.notnull(gainers_df), None)
    losers_df = losers_df.where(pd.notnull(losers_df), None)

    payload = {
        "expiry": str(expiry_),
        "updated_at": datetime.now(ctx["IST"]).isoformat(),
        "gainers": gainers_df.to_dict("records"),
        "losers": losers_df.to_dict("records"),
    }

    with FNO_LOCK:
        MOVERS_CACHE[cache_key] = (payload, now + FNO_MOVERS_TTL_SEC)

    return payload
