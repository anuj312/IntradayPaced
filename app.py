import os, time, threading
from collections import deque
from datetime import datetime, timedelta
from urllib.parse import unquote

import pandas as pd
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from starlette.middleware.wsgi import WSGIMiddleware

import dash
from dash import dcc, html, Input, Output
import dash_bootstrap_components as dbc
import dash_ag_grid as dag

from kiteconnect import KiteConnect, KiteTicker


# ------------------- MOUNT PATH -------------------
BASE = "/dash/"  # Browser URL: http://host:port/dash/


# ------------------- KITE CONFIG -------------------
API_KEY = os.getenv("KITE_API_KEY", "")
ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "")
if not API_KEY or not ACCESS_TOKEN:
    raise RuntimeError("Set KITE_API_KEY and KITE_ACCESS_TOKEN environment variables.")


# ------------------- SECTORS -------------------
SECTOR_DEFINITIONS = {
    "METAL": ["ADANIENT","HINDALCO","JSWSTEEL","HINDZINC","APLAPOLLO","TATASTEEL","JINDALSTEL","VEDL","SAIL","NATIONALUM","NMDC"],
    "PSUS": ["BANKINDIA","PNB","INDIANB","SBIN","UNIONBANK","BANKBARODA","CANBK"],
    "REALTY": ["PHOENIXLTD","GODREJPROP","LODHA","OBEROIRLTY","DLF","PRESTIGE","NBCC","NCC"],
    "ENERGY": ["CGPOWER","RELIANCE","GMRAIRPORT","JSWENERGY","ONGC","POWERGRID","BLUESTARCO","COALINDIA","SUZLON","IREDA","IOC","IGL","TATAPOWER","INOXWIND","MAZDOCK","PETRONET","SOLARINDS","ADANIGREEN","NTPC","OIL","BDL","BPCL","NHPC","POWERINDIA","ADANIENSOL","TORRENTPOWER"],
    "AUTO": ["BOSCHLTD","TIINDIA","HEROMOTOCO","M&M","EICHERMOT","EXIDEIND","BAJAJ-AUTO","ASHOKLEY","MARUTI","TITAGARH","TVSMOTOR","MOTHERSON","SONACOMS","UNOMINDA","TATAMOTORS","BHARATFORG"],
    "IT": ["KAYNES","TATATECH","LTIM","CYIENT","MPHASIS","TCS","CAMS","OFSS","HFCL","TECHM","TATAELXSI","HCLTECH","WIPRO","KPITTECH","COFORGE","PERSISTENT","INFY"],
    "PHARMA": ["CIPLA","ALKEM","BIOCON","DRREDDY","MANKIND","TORNTPHARM","ZYDUSLIFE","DIVISLAB","LUPIN","PPLPHARMA","LAURUSLABS","FORTIS","AUROPHARMA","GLENMARK","SUNPHARMA"],
    "FMCG": ["ETERNAL","MARICO","NYKAA","NESTLEIND","VBL","COLPAL","HINDUNILVR","PATANJALI","DMART","DABUR","GODREJCP","BRITANNIA","UNITDSPR","ITC","TATACONSUM","KALYANKJIL","SUPREMEIND"],
    "CEMENT": ["SHREECEM","DALBHARAT","AMBUJACEM","ULTRACEMCO"],
    "FINSERVICE": ["PNBHOUSING","BAJAJFINSV","ICICIPRULI","NUVAMA","HDFCLIFE","SAMMAANCAP","ANGELONE","RECLTD","BAJFINANCE","BSE","MAXHEALTH","ICICIGI","HUDCO","CHOLAFIN","PFC","HDFCAMC","MUTHOOTFIN","PAYTM","JIOFIN","SHRIRAMFIN","SBICARD","POLICYBZR","SBILIFE","LICHSGFIN","LICI","MANAPPURAM","IRFC","IIFL","CDSL"],
    "BANK": ["IDFCFIRSTB","FEDERALBNK","INDUSINDBK","HDFCBANK","SBIN","KOTAKBANK","AUBANK","CANBK","BANDHANBNK","RBLBANK","ICICIBANK","AXISBANK"],
    "NIFTY_50": ["ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK","BAJAJ-AUTO","BAJFINANCE","BAJAJFINSV","BEL","BHARTIARTL","CIPLA","COALINDIA","DRREDDY","EICHERMOT","GRASIM","HCLTECH","HDFCBANK","HDFCLIFE","HINDALCO","HINDUNILVR","ICICIBANK","INFY","INDIGO","ITC","JIOFIN","JSWSTEEL","KOTAKBANK","LT","M&M","MARUTI","MAXHEALTH","NESTLEIND","NTPC","ONGC","POWERGRID","RELIANCE","SBILIFE","SHRIRAMFIN","SBIN","SUNPHARMA","TCS","TATACONSUM","TATASTEEL","TECHM","TITAN","TRENT","ULTRACEMCO","WIPRO","TATAMOTORS","ETERNAL"],
    "MIDCAP": ["RVNL","MPHASIS","HINDPETRO","PAGEIND","POLYCAB","LUPIN","IDFCFIRSTB","CONCOR","CUMMINSIND","VOLTAS","BHARATFORG","FEDERALBNK","INDHOTEL","COFORGE","ASHOKLEY","PERSISTENT","UPL","GODREJPROP","AUROPHARMA","AUBANK","ASTRAL","HDFCAMC","JUBLFOOD","PIIND"],
}
ALL_SYMBOLS = sorted(set(sum(SECTOR_DEFINITIONS.values(), [])))


# ------------------- KITE INIT -------------------
kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)

ins = pd.DataFrame(kite.instruments("NSE"))
ins = ins[ins["tradingsymbol"].isin(ALL_SYMBOLS)].copy()

symbol_to_token = dict(zip(ins["tradingsymbol"], ins["instrument_token"]))
symbol_to_name = dict(zip(ins["tradingsymbol"], ins["name"]))
TOKENS = sorted(symbol_to_token.values())


# ------------------- LIVE STATE (5m aggregation) -------------------
LOCK = threading.Lock()

CUR = {}          # token -> current 5m candle {bucket,open,high,low,close,vol_5m,synthetic?}
BARS = {}         # token -> deque of completed 5m candles
LAST_CUMVOL = {}  # token -> last seen cumulative day volume

DAY_OPEN = {}     # token -> day open (from tick.ohlc.open)
DAY_VOL = {}      # token -> day cumulative volume (volume_traded)

# tick stats
LAST_TICK_TS = 0.0
LAST_TICK_DT = None
TOTAL_TICKS = 0

TPS_WINDOW_SEC = 1.0
TPS_BUCKETS = deque()  # (time.time(), count)

# history seeding status
_seed_started = False
SEED_DONE = False
SEED_PROGRESS = {"done": 0, "total": len(TOKENS)}
SEED_ERRORS = 0


def floor_5m(dt: datetime):
    return dt.replace(second=0, microsecond=0, minute=dt.minute - (dt.minute % 5))


def ensure(token):
    if token not in BARS:
        BARS[token] = deque(maxlen=300)


def _record_tick_batch(count: int, last_dt: datetime | None):
    """Call only under LOCK."""
    global LAST_TICK_TS, LAST_TICK_DT, TOTAL_TICKS

    now = time.time()
    TOTAL_TICKS += int(count)

    TPS_BUCKETS.append((now, int(count)))
    cutoff = now - TPS_WINDOW_SEC
    while TPS_BUCKETS and TPS_BUCKETS[0][0] < cutoff:
        TPS_BUCKETS.popleft()

    LAST_TICK_TS = now
    LAST_TICK_DT = last_dt or datetime.now()


def _get_tps():
    """Call only under LOCK."""
    if not TPS_BUCKETS:
        return 0.0
    return sum(c for _, c in TPS_BUCKETS) / TPS_WINDOW_SEC


def update_from_tick(tick: dict):
    """Update day values + build 5m candle from ticks."""
    token = tick["instrument_token"]
    ensure(token)

    ts = tick.get("exchange_timestamp") or datetime.now()
    ltp = tick.get("last_price")
    cumvol = tick.get("volume_traded")  # day cumulative
    ohlc = tick.get("ohlc") or {}

    if ltp is None or cumvol is None:
        return

    DAY_OPEN[token] = ohlc.get("open") or DAY_OPEN.get(token)
    DAY_VOL[token] = cumvol

    bucket = floor_5m(ts)

    last_c = LAST_CUMVOL.get(token)
    LAST_CUMVOL[token] = cumvol

    # IMPORTANT: handle new-day reset of cumulative volume
    if last_c is not None and cumvol < last_c:
        vol_delta = cumvol
    else:
        vol_delta = max(0, (cumvol - last_c)) if last_c is not None else 0

    if token not in CUR:
        CUR[token] = {
            "bucket": bucket, "open": ltp, "high": ltp, "low": ltp,
            "close": ltp, "vol_5m": vol_delta
        }
        return

    c = CUR[token]
    if bucket != c["bucket"]:
        BARS[token].append({
            "bucket": c["bucket"], "open": c["open"], "high": c["high"], "low": c["low"],
            "close": c["close"], "vol_5m": c["vol_5m"]
        })
        CUR[token] = {
            "bucket": bucket, "open": ltp, "high": ltp, "low": ltp,
            "close": ltp, "vol_5m": vol_delta
        }
        return

    # same bucket
    if c.get("synthetic"):
        # replace the synthetic candle open with first real tick
        c["open"] = ltp
        c["synthetic"] = False

    c["high"] = max(c["high"], ltp)
    c["low"] = min(c["low"], ltp)
    c["close"] = ltp
    c["vol_5m"] += vol_delta


def true_range(h, l, prev_c):
    if prev_c is None:
        return h - l
    return max(h - l, abs(h - prev_c), abs(l - prev_c))


def compute_spike_for_token(
    token,
    atr_n=14,
    rvol_n=20,
    use_close_quality=True,
    use_completed_bar=True,  # Option A: use last completed 5m candle (stable within each 5m)
):
    """
    Spike (signed) = sign(C-O) * RVOL * (Range/ATR) * close_quality_factor
    close_quality_factor in [0.5..1.0]

    If use_completed_bar=True:
      - Spike is computed on the last COMPLETED 5m candle (BARS[-1])
      - So it won't "reset" at the start of each new 5m candle.
    """
    ensure(token)
    bars = list(BARS[token])

    if use_completed_bar:
        # Score last completed bar. Use earlier bars as history.
        if len(bars) < max(atr_n + 2, rvol_n + 1):
            return None
        cur = bars[-1]
        hist = bars[:-1]
    else:
        # Score current forming bar (original behavior)
        cur = CUR.get(token)
        if cur is None:
            return None
        if len(bars) < max(atr_n + 1, rvol_n):
            return None
        hist = bars

    # ATR from history (need atr_n + 1 bars to incorporate prev_close)
    last_completed = hist[-(atr_n + 1):]
    trs = []
    prev_close = None
    for b in last_completed:
        trs.append(true_range(b["high"], b["low"], prev_close))
        prev_close = b["close"]

    atr = sum(trs[-atr_n:]) / atr_n if atr_n else None
    if not atr or atr == 0:
        return None

    # RVOL baseline from history (exclude the bar being scored when use_completed_bar=True)
    vols = [b["vol_5m"] for b in hist[-rvol_n:]]
    avg_vol = (sum(vols) / len(vols)) if vols else None
    if not avg_vol or avg_vol == 0:
        return None
    rvol = cur["vol_5m"] / avg_vol

    rng = cur["high"] - cur["low"]
    range_by_atr = rng / atr

    sign = 1 if cur["close"] >= cur["open"] else -1

    cq = 1.0
    if use_close_quality and rng > 0:
        cq_raw = ((cur["close"] - cur["low"]) / rng) if sign > 0 else ((cur["high"] - cur["close"]) / rng)
        cq_raw = max(0.0, min(1.0, cq_raw))
        cq = 0.5 + 0.5 * cq_raw

    spike = sign * (rvol * range_by_atr) * cq
    return {
        "atr": atr,
        "rvol": rvol,
        "range": rng,
        "range_by_atr": range_by_atr,
        "spike": spike,
        "spike_abs": abs(spike),
    }


def compute_sector_strength_signed():
    out = {}
    for sector, syms in SECTOR_DEFINITIONS.items():
        vals = []
        for s in syms:
            tok = symbol_to_token.get(s)
            if not tok:
                continue
            # Option A: stable spike (last completed candle)
            sp = compute_spike_for_token(tok, use_completed_bar=True)
            if sp and sp["spike"] is not None:
                vals.append(sp["spike"])
        out[sector] = (sum(vals) / len(vals)) if vals else 0.0
    return out


def sector_rows_sorted_both_sides(sector: str):
    rows = []
    for s in SECTOR_DEFINITIONS.get(sector, []):
        tok = symbol_to_token.get(s)
        if not tok:
            continue

        cur = CUR.get(tok)
        if not cur:
            continue

        ltp = cur["close"]
        day_open = DAY_OPEN.get(tok)

        chg = (ltp - day_open) if day_open else None
        chg_pct = ((ltp - day_open) / day_open * 100.0) if day_open else None

        # Option A: stable spike (last completed candle)
        sp = compute_spike_for_token(tok, use_completed_bar=True)

        rows.append({
            "Symbol": s,
            "Company": symbol_to_name.get(s, ""),
            "Price": ltp,
            "Change": chg,
            "Change%": chg_pct,
            "Range/ATR": (sp["range_by_atr"] if sp else None),
            "Spike": (sp["spike"] if sp else None),
            "SpikeAbs": (sp["spike_abs"] if sp else None),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return []
    df = df.sort_values("SpikeAbs", ascending=False, na_position="last")
    return df.to_dict("records")


# ------------------- HISTORY SEEDING (so Spike works quickly after 9:15) -------------------
def seed_history_once(days_back: int = 7, interval: str = "5minute", per_req_sleep: float = 0.35):
    global _seed_started, SEED_DONE, SEED_ERRORS

    if _seed_started:
        return
    _seed_started = True

    def _run():
        global SEED_DONE, SEED_ERRORS
        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=days_back)

        total = len(TOKENS)
        SEED_PROGRESS["total"] = total
        SEED_PROGRESS["done"] = 0

        for i, tok in enumerate(TOKENS, start=1):
            try:
                candles = kite.historical_data(
                    instrument_token=tok,
                    from_date=from_dt,
                    to_date=to_dt,
                    interval=interval,
                    continuous=False,
                    oi=False,
                )
            except Exception:
                SEED_ERRORS += 1
                candles = []

            with LOCK:
                ensure(tok)
                BARS[tok].clear()

                for c in candles[-300:]:
                    dt = c["date"]
                    BARS[tok].append({
                        "bucket": floor_5m(dt),
                        "open": c["open"],
                        "high": c["high"],
                        "low": c["low"],
                        "close": c["close"],
                        "vol_5m": c.get("volume", 0) or 0,
                    })

                # keep synthetic CUR (not required for Option A spike, but still useful for showing Price before ticks)
                if candles and tok not in CUR:
                    last_close = candles[-1]["close"]
                    CUR[tok] = {
                        "bucket": floor_5m(datetime.now()),
                        "open": last_close,
                        "high": last_close,
                        "low": last_close,
                        "close": last_close,
                        "vol_5m": 0,
                        "synthetic": True,
                    }

            SEED_PROGRESS["done"] = i
            time.sleep(per_req_sleep)

        SEED_DONE = True

    threading.Thread(target=_run, daemon=True).start()


# ------------------- BACKGROUND TICKER (start once) -------------------
_started = False

def start_ticker_once():
    global _started
    if _started:
        return
    _started = True

    def _run():
        kws = KiteTicker(API_KEY, ACCESS_TOKEN)

        def on_connect(ws, _):
            ws.subscribe(TOKENS)
            ws.set_mode(ws.MODE_FULL, TOKENS)

        def on_ticks(ws, ticks):
            last_dt = None
            with LOCK:
                for t in ticks:
                    ts = t.get("exchange_timestamp")
                    if ts and (last_dt is None or ts > last_dt):
                        last_dt = ts
                    update_from_tick(t)

                _record_tick_batch(len(ticks), last_dt)

        kws.on_connect = on_connect
        kws.on_ticks = on_ticks
        kws.connect(threaded=True)

        while True:
            time.sleep(1)

    threading.Thread(target=_run, daemon=True).start()


# ------------------- DASH APP -------------------
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")

dash_app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.CYBORG],
    requests_pathname_prefix=BASE,   # browser URLs (/dash/...)
    routes_pathname_prefix="/",      # Flask after mount strips /dash
    assets_folder=ASSETS_DIR,
    suppress_callback_exceptions=True,
)
server = dash_app.server


def top_nav(pathname: str):
    pathname = pathname or f"{BASE}"
    if pathname in ("/dash", "/dash/"):
        pathname = f"{BASE}"

    is_intr = pathname == f"{BASE}intrabuzz"
    is_heat = pathname == f"{BASE}heatmap"
    is_sectors = (pathname == f"{BASE}") or pathname.startswith(f"{BASE}sector/")

    return dbc.Nav(
        [
            dbc.NavLink("Intrabuzz", href=f"{BASE}intrabuzz", active=is_intr, className="top-tab"),
            dbc.NavLink("Sectors", href=f"{BASE}", active=is_sectors, className="top-tab"),
            dbc.NavLink("Heatmap", href=f"{BASE}heatmap", active=is_heat, className="top-tab"),
        ],
        pills=True,
        className="top-tabs",
    )


def sectors_page():
    return html.Div(
        [
            html.H4("Sectors", className="page-title"),
            html.Div(id="sector-bars", className="sector-bars-wrap"),
            html.Div("Click a sector to open stocks (sorted by SpikeAbs).", className="hint"),
        ],
        className="page-wrap",
    )


def sector_page(sector):
    return html.Div(
        [
            dbc.Row(
                [
                    dbc.Col(html.H4(f"{sector} Stocks", className="page-title"), width=True),
                    dbc.Col(
                        dbc.Button("Back", href=f"{BASE}", color="secondary", outline=True, className="btn-back"),
                        width="auto",
                    ),
                ],
                className="align-items-center g-2",
            ),
            dag.AgGrid(
                id="grid",
                className="ag-theme-alpine-dark grid-wrap",
                columnDefs=[
                    {
                        "field": "Symbol",
                        "headerName": "Stock",
                        "pinned": "left",
                        "cellRenderer": "StockCell",
                        "minWidth": 240,
                    },
                    {"field": "Price", "type": "rightAligned",
                     "valueFormatter": {"function": "fmt2(params.value)"}},

                    {"field": "Change", "type": "rightAligned",
                     "valueFormatter": {"function": "fmtSigned2(params.value)"},
                     "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"}},

                    {"field": "Change%", "type": "rightAligned",
                     "valueFormatter": {"function": "fmtPct(params.value)"},
                     "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"}},

                    {"field": "Range/ATR", "type": "rightAligned",
                     "valueFormatter": {"function": "fmt2(params.value)"}},

                    {"field": "Spike", "type": "rightAligned",
                     "cellRenderer": "SpikeChip",
                     "valueFormatter": {"function": "fmtSigned2(params.value)"}},

                    {"field": "SpikeAbs", "headerName": "SpikeAbs", "type": "rightAligned",
                     "valueFormatter": {"function": "fmt2(params.value)"},
                     "sort": "desc"},
                ],
                rowData=[],
                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                dashGridOptions={
                    "animateRows": True,
                    "sortingOrder": ["desc", "asc"],
                    "getRowId": {"function": "params.data.Symbol"},
                },
                style={"height": "72vh", "width": "100%"},
            ),
        ],
        className="page-wrap",
    )


dash_app.layout = dbc.Container(
    fluid=True,
    children=[
        dcc.Location(id="url"),
        dcc.Interval(id="refresh", interval=2000, n_intervals=0),
        dcc.Interval(id="top_refresh", interval=1000, n_intervals=0),

        # Top bar skeleton (exists always -> safe for callbacks)
        html.Div(
            dbc.Row(
                [
                    dbc.Col(html.Div(id="top-nav"), width=True),
                    dbc.Col(html.Div(id="top-stats"), width="auto"),
                ],
                className="align-items-center g-2",
            ),
            className="topbar-wrap",
        ),

        html.Div(id="app-body"),
    ],
)


@dash_app.callback(
    Output("top-nav", "children"),
    Output("app-body", "children"),
    Input("url", "pathname"),
)
def route(pathname):
    pathname = pathname or f"{BASE}"
    if pathname in ("/dash", "/dash/"):
        pathname = f"{BASE}"

    nav = top_nav(pathname)

    if pathname == f"{BASE}intrabuzz":
        body = dbc.Alert("Intrabuzz placeholder", color="secondary", className="page-wrap")
        return nav, body

    if pathname.startswith(f"{BASE}sector/"):
        sector = unquote(pathname.split(f"{BASE}sector/")[1]).upper()
        body = sector_page(sector) if sector in SECTOR_DEFINITIONS else dbc.Alert("Sector not found", color="danger")
        return nav, body

    if pathname == f"{BASE}heatmap":
        body = dbc.Alert("Heatmap placeholder", color="secondary", className="page-wrap")
        return nav, body

    return nav, sectors_page()


@dash_app.callback(Output("top-stats", "children"), Input("top_refresh", "n_intervals"))
def update_top_stats(_):
    now_dt = datetime.now()
    now_str = now_dt.strftime("%H:%M:%S")

    with LOCK:
        if not SEED_DONE:
            done = SEED_PROGRESS.get("done", 0)
            total = SEED_PROGRESS.get("total", 0)
            return html.Div(
                [
                    dbc.Badge("Seeding", color="warning", className="stat-badge"),
                    html.Div(f"{done}/{total}", className="stat-chip"),
                    html.Div(f"Now {now_str}", className="stat-chip"),
                    html.Div(f"Errors {SEED_ERRORS}", className="stat-chip"),
                ],
                className="top-stats-wrap",
            )

        offline = (time.time() - LAST_TICK_TS) > 10 if LAST_TICK_TS else True
        tps = _get_tps()
        tot = TOTAL_TICKS
        last_dt = LAST_TICK_DT
        age = (time.time() - LAST_TICK_TS) if LAST_TICK_TS else None

    last_str = last_dt.strftime("%H:%M:%S") if last_dt else "--:--:--"
    age_str = f"{age:.0f}s" if age is not None else "--"

    return html.Div(
        [
            dbc.Badge("Offline" if offline else "Live",
                      color=("danger" if offline else "success"),
                      className="stat-badge"),
            html.Div(f"TPS {tps:.1f}", className="stat-chip"),
            html.Div(f"Ticks {tot:,}", className="stat-chip"),
            html.Div(f"Now {now_str}", className="stat-chip"),
            html.Div(f"Last {last_str}", className="stat-chip"),
            html.Div(f"Age {age_str}", className="stat-chip"),
        ],
        className="top-stats-wrap",
    )


@dash_app.callback(Output("sector-bars", "children"), Input("refresh", "n_intervals"), Input("url", "pathname"))
def render_sector_bars(_, pathname):
    pathname = pathname or f"{BASE}"
    if pathname not in (f"{BASE}", "/dash", "/dash/"):
        return dash.no_update

    with LOCK:
        scores = compute_sector_strength_signed()

    items = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    max_abs = max([abs(v) for _, v in items] + [1e-6])

    children = []
    for sector, val in items:
        h = int(30 + 260 * (abs(val) / max_abs))
        cls = "bar-green" if val >= 0 else "bar-red"
        label = f"{val:+.2f}"

        children.append(
            dcc.Link(
                href=f"{BASE}sector/{sector}",
                className="sector-link",
                children=html.Div(
                    [
                        html.Div(label, className="bar-value"),
                        html.Div(className=f"glow-bar {cls}", style={"height": f"{h}px"}),
                        html.Div(sector.title(), className="bar-name"),
                    ],
                    className="sector-bar-card",
                ),
            )
        )
    return children


@dash_app.callback(Output("grid", "rowData"), Input("refresh", "n_intervals"), Input("url", "pathname"))
def update_grid(_, pathname):
    if not pathname or not pathname.startswith(f"{BASE}sector/"):
        return dash.no_update
    sector = unquote(pathname.split(f"{BASE}sector/")[1]).upper()
    if sector not in SECTOR_DEFINITIONS:
        return []
    with LOCK:
        return sector_rows_sorted_both_sides(sector)


# ------------------- FASTAPI APP (export as `app`) -------------------
app = FastAPI(title="Stocker")

@app.on_event("startup")
def _startup():
    seed_history_once(days_back=7, interval="5minute", per_req_sleep=0.35)
    start_ticker_once()

@app.get("/health")
def health():
    with LOCK:
        return {
            "status": "ok",
            "seed_done": SEED_DONE,
            "seed_progress": SEED_PROGRESS,
            "seed_errors": SEED_ERRORS,
            "tps": round(_get_tps(), 3),
            "total_ticks": TOTAL_TICKS,
            "last_tick_time": (LAST_TICK_DT.isoformat() if LAST_TICK_DT else None),
        }

@app.get("/dash")
def _dash_redirect():
    return RedirectResponse(url="/dash/")

app.mount("/dash", WSGIMiddleware(server))

@app.get("/")
def root():
    return RedirectResponse(url="/dash/")