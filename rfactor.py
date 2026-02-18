# rfactor.py
# Dash page plugin: /dash/rfactor
#
# Shows:
#   1) Top 15 / Bottom 15 by DirR (signed paced RFactor)
#   2) Build-up UP/DOWN (since open): High RVOLm + Low abs(%Chg from Open)
#      - Option A: ignore first 5 minutes after open (09:15–09:20 IST)
#   3) ΔR(60s) acceleration column (rolling)
#
# Exposes:
#   rfactor_page(BASE) -> layout
#   register_rfactor(dash_app, BASE, ctx) -> callbacks
#
# ctx must include:
#   LOCK, ALL_SYMBOLS, symbol_to_token, compute_rfactor_row_for_token_paced, IST(optional)

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, time as dtime
from typing import Dict, Any, List, Tuple, Optional
from zoneinfo import ZoneInfo

from dash import dcc, html, Input, Output
import dash_bootstrap_components as dbc
import dash_ag_grid as dag

log = logging.getLogger("turbotrades.rfactor")

TOP_N = 15

# Build-up filter (since open)
BUILD_RVOLM_MIN = 2.0          # activity >= 2x expected for current time
BUILD_ABS_PCTOPEN_MAX = 0.50   # still within +/-0.50% of open

# Ignore first 5 minutes after open (Option A)
BUILDUP_START_TIME_IST = dtime(9, 20)

# Acceleration window (rolling)
ACCEL_WINDOW_SEC = 60
HIST_KEEP_SEC = 5 * 60

IST_FALLBACK = ZoneInfo("Asia/Kolkata")

# token -> deque[(epoch, rfactor)]
_RHIST: Dict[int, deque] = {}


def _push_hist(token: int, epoch: float, rfactor_val: float) -> None:
    dq = _RHIST.get(token)
    if dq is None:
        dq = deque()
        _RHIST[token] = dq

    dq.append((float(epoch), float(rfactor_val)))

    cutoff = float(epoch) - float(HIST_KEEP_SEC)
    while dq and dq[0][0] < cutoff:
        dq.popleft()


def _delta_over_window(token: int, epoch: float, window_sec: float) -> Optional[float]:
    dq = _RHIST.get(token)
    if not dq or len(dq) < 2:
        return None

    cutoff = float(epoch) - float(window_sec)
    base = None
    for t, v in dq:
        if t <= cutoff:
            base = (t, v)
        else:
            break

    if base is None:
        return None

    return float(dq[-1][1] - base[1])


def _build_rows(ctx: Dict[str, Any]) -> List[dict]:
    ALL_SYMBOLS = ctx["ALL_SYMBOLS"]
    symbol_to_token = ctx["symbol_to_token"]
    compute_paced = ctx["compute_rfactor_row_for_token_paced"]

    now = time.time()

    rows: List[dict] = []
    for sym in ALL_SYMBOLS:
        tok = symbol_to_token.get(sym)
        if not tok:
            continue

        rr = compute_paced(tok)
        if not rr:
            continue

        rfac = float(rr["rfactor"])
        _push_hist(tok, now, rfac)
        dR = _delta_over_window(tok, now, ACCEL_WINDOW_SEC)

        pct_open = float(rr["pct_open"])
        rvolm = rr.get("rvolm")
        rvolm_f = float(rvolm) if rvolm is not None else None

        build_score = None
        if rvolm_f is not None:
            # High volume + tight price around open => higher score
            build_score = rvolm_f / (abs(pct_open) + 0.10)

        rows.append(
            {
                "Symbol": sym,
                "%Chg(O)": round(pct_open, 2),
                "RVOLm": (round(rvolm_f, 2) if rvolm_f is not None else None),
                "RFactor": round(rfac, 2),
                "DirR": round(float(rr["dirr"]), 2),
                "ΔR60s": (round(float(dR), 2) if dR is not None else None),
                "BuildScore": (round(float(build_score), 2) if build_score is not None else None),
                "Vol": int(rr["vol_today"]),
            }
        )

    return rows


def _top_bottom_dirr(rows: List[dict], n: int = TOP_N) -> Tuple[List[dict], List[dict]]:
    rows2 = [r for r in rows if r.get("DirR") is not None]
    top = sorted(rows2, key=lambda r: float(r["DirR"]), reverse=True)[:n]
    bottom = sorted(rows2, key=lambda r: float(r["DirR"]))[:n]
    return top, bottom


def _build_up_down_since_open(rows: List[dict], n: int = TOP_N) -> Tuple[List[dict], List[dict]]:
    """
    Build-up since open:
      RVOLm >= threshold AND abs(%Chg(O)) <= threshold
    Split:
      - UP build-up: %Chg(O) >= 0
      - DOWN build-up: %Chg(O) < 0
    Rank by BuildScore desc, then ΔR60s desc.
    """
    filt = []
    for r in rows:
        rvolm = r.get("RVOLm")
        pct = r.get("%Chg(O)")
        if rvolm is None or pct is None:
            continue
        if float(rvolm) < BUILD_RVOLM_MIN:
            continue
        if abs(float(pct)) > BUILD_ABS_PCTOPEN_MAX:
            continue
        filt.append(r)

    up = [r for r in filt if float(r["%Chg(O)"]) >= 0]
    dn = [r for r in filt if float(r["%Chg(O)"]) < 0]

    def key(r):
        return (float(r.get("BuildScore") or 0.0), float(r.get("ΔR60s") or 0.0))

    up = sorted(up, key=key, reverse=True)[:n]
    dn = sorted(dn, key=key, reverse=True)[:n]
    return up, dn


def rfactor_page(BASE: str):
    cols_main = [
        {"colId": "stock", "field": "Symbol", "headerName": "STOCK", "cellRenderer": "SymbolCell", "minWidth": 120, "flex": 1},
        {"colId": "pct", "field": "%Chg(O)", "headerName": "%CHG(O)", "cellRenderer": "PctPill", "minWidth": 140, "maxWidth": 160},
        {"colId": "rvolm", "field": "RVOLm", "headerName": "RVOLm", "type": "rightAligned",
         "valueFormatter": {"function": "fmt2(params.value)"}, "minWidth": 110, "maxWidth": 130},
        {"colId": "rf", "field": "RFactor", "headerName": "RFACTOR", "cellRenderer": "RfactorPill", "minWidth": 125, "maxWidth": 170},
        {"colId": "dirr", "field": "DirR", "headerName": "DIR R", "type": "rightAligned",
         "valueFormatter": {"function": "fmtSigned2(params.value)"}, "minWidth": 110, "maxWidth": 130},
        {"colId": "dr", "field": "ΔR60s", "headerName": "ΔR(60s)", "type": "rightAligned",
         "valueFormatter": {"function": "fmtSigned2(params.value)"}, "minWidth": 120, "maxWidth": 140},
        {"colId": "vol", "field": "Vol", "headerName": "VOLUME", "cellRenderer": "VolPill", "minWidth": 140, "maxWidth": 190},
    ]

    cols_build = [
        {"colId": "stock", "field": "Symbol", "headerName": "STOCK", "cellRenderer": "SymbolCell", "minWidth": 120, "flex": 1},
        {"colId": "score", "field": "BuildScore", "headerName": "BUILD SCORE", "type": "rightAligned",
         "valueFormatter": {"function": "fmt2(params.value)"}, "minWidth": 140, "maxWidth": 160},
        {"colId": "rvolm", "field": "RVOLm", "headerName": "RVOLm", "type": "rightAligned",
         "valueFormatter": {"function": "fmt2(params.value)"}, "minWidth": 110, "maxWidth": 130},
        {"colId": "pct", "field": "%Chg(O)", "headerName": "%CHG(O)", "cellRenderer": "PctPill", "minWidth": 140, "maxWidth": 160},
        {"colId": "dr", "field": "ΔR60s", "headerName": "ΔR(60s)", "type": "rightAligned",
         "valueFormatter": {"function": "fmtSigned2(params.value)"}, "minWidth": 120, "maxWidth": 140},
        {"colId": "rf", "field": "RFactor", "headerName": "RFACTOR", "cellRenderer": "RfactorPill", "minWidth": 125, "maxWidth": 170},
    ]

    grid_opts = {
        "getRowId": {"function": "params.data.Symbol"},
        "alwaysShowVerticalScroll": False,
        "animateRows": False,
        "suppressMenuHide": False,
        "onGridReady": {"function": "params.api.sizeColumnsToFit();"},
        "onGridSizeChanged": {"function": "params.api.sizeColumnsToFit();"},
    }

    return html.Div(
        [
            dcc.Interval(id="refresh_rfactor", interval=2000, n_intervals=0),
            dbc.Row(
                [
                    dbc.Col(html.H4("RFactor (PACED) + Build-up (since open)", className="page-title"), width=True),
                    dbc.Col(dbc.Button("Back", href=f"{BASE}", color="secondary", outline=True, className="btn-back"), width="auto"),
                ],
                className="align-items-center g-2",
            ),
            html.Div(
                f"Build-up filter: RVOLm ≥ {BUILD_RVOLM_MIN:.1f}x and |%Chg(O)| ≤ {BUILD_ABS_PCTOPEN_MAX:.2f}%. "
                f"ΔR(60s) is rolling acceleration. Build-up tables are hidden until {BUILDUP_START_TIME_IST.strftime('%H:%M')} IST.",
                className="hint",
            ),
            html.Hr(),

            dbc.Row(
                [
                    dbc.Col(
                        [
                            html.H6("Top 15 (DirR strongest +)", className="mt-1"),
                            dag.AgGrid(
                                id="rf-top-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=cols_main,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(520px, 50vh)", "width": "100%"},
                            ),
                        ],
                        md=6,
                    ),
                    dbc.Col(
                        [
                            html.H6("Bottom 15 (DirR strongest −)", className="mt-1"),
                            dag.AgGrid(
                                id="rf-bottom-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=cols_main,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(520px, 50vh)", "width": "100%"},
                            ),
                        ],
                        md=6,
                    ),
                ],
                className="g-2",
            ),

            html.Hr(),

            dbc.Row(
                [
                    dbc.Col(
                        [
                            html.H6("Build-up UP (since open)", className="mt-1"),
                            dag.AgGrid(
                                id="build-up-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=cols_build,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(520px, 42vh)", "width": "100%"},
                            ),
                        ],
                        md=6,
                    ),
                    dbc.Col(
                        [
                            html.H6("Build-up DOWN (since open)", className="mt-1"),
                            dag.AgGrid(
                                id="build-down-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=cols_build,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(520px, 42vh)", "width": "100%"},
                            ),
                        ],
                        md=6,
                    ),
                ],
                className="g-2",
            ),
        ],
        className="page-wrap",
    )


def register_rfactor(dash_app, BASE: str, ctx: Dict[str, Any]) -> None:
    @dash_app.callback(
        Output("rf-top-grid", "rowData"),
        Output("rf-bottom-grid", "rowData"),
        Output("build-up-grid", "rowData"),
        Output("build-down-grid", "rowData"),
        Input("refresh_rfactor", "n_intervals"),
        prevent_initial_call=False,
    )
    def _update(_n):
        try:
            with ctx["LOCK"]:
                rows = _build_rows(ctx)

            top, bottom = _top_bottom_dirr(rows, n=TOP_N)

            ist = ctx.get("IST") or IST_FALLBACK
            now_ist = datetime.now(ist).time()

            # Option A: hide build-up tables for first 5 minutes (09:15–09:20)
            if now_ist < BUILDUP_START_TIME_IST:
                bup, bdn = [], []
            else:
                bup, bdn = _build_up_down_since_open(rows, n=TOP_N)

            return top, bottom, bup, bdn

        except Exception:
            log.exception("rfactor page update crashed")
            return [], [], [], []