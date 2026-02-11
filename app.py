# app.py  (NO AUTH • NO FIREBASE • NO SUBSCRIPTIONS)
#
# Open dashboard at: http://127.0.0.1:8000/dash/
#
# Required env:
#   KITE_API_KEY
#   KITE_ACCESS_TOKEN
#
# Run (single worker recommended):
#   uvicorn app:app --reload --workers 1

import os
import time
import threading
import logging
from collections import deque
from datetime import datetime, timedelta, time as dtime
from typing import Optional, Dict, Any, Tuple
from urllib.parse import unquote
from zoneinfo import ZoneInfo
from math import isfinite
from pathlib import Path

import pandas as pd
from fastapi import FastAPI
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse
from starlette.middleware.wsgi import WSGIMiddleware

import dash
from dash import dcc, html, Input, Output
import dash_bootstrap_components as dbc
import dash_ag_grid as dag

from kiteconnect import KiteConnect, KiteTicker


# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("turbotrades")


# =============================================================================
# CONFIG
# =============================================================================
BASE = "/dash/"
IST = ZoneInfo("Asia/Kolkata")

API_KEY = os.getenv("KITE_API_KEY", "").strip()
ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "").strip()
if not API_KEY or not ACCESS_TOKEN:
    raise RuntimeError("Missing KITE_API_KEY / KITE_ACCESS_TOKEN environment variables.")

SEED_SLEEP_SEC = float(os.getenv("SEED_SLEEP_SEC", "0.35"))

LOOKBACK_SESSIONS = 20

HOT_WINDOW_SEC = 15 * 60
HOT_SAMPLE_SEC = 5
HOT_HISTORY_MAX_SEC = HOT_WINDOW_SEC + 5 * 60


# =============================================================================
# KITE INIT
# =============================================================================
kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)


# =============================================================================
# SECTORS / SYMBOLS
# =============================================================================
SECTOR_DEFINITIONS = {
    "METAL": ["ADANIENT","HINDALCO","JSWSTEEL","HINDZINC","APLAPOLLO","TATASTEEL","JINDALSTEL","VEDL","SAIL","NATIONALUM","NMDC"],
    "PSUS": ["BANKINDIA","PNB","INDIANB","SBIN","UNIONBANK","BANKBARODA","CANBK"],
    "REALTY": ["PHOENIXLTD","GODREJPROP","LODHA","OBEROIRLTY","DLF","PRESTIGE","NBCC","NCC"],
    "ENERGY": ["CGPOWER","RELIANCE","GMRAIRPORT","JSWENERGY","ONGC","POWERGRID","BLUESTARCO","COALINDIA","SUZLON","IREDA",
               "IOC","IGL","TATAPOWER","INOXWIND","MAZDOCK","PETRONET","SOLARINDS","ADANIGREEN","NTPC","OIL","BDL","BPCL",
               "NHPC","POWERINDIA","ADANIENSOL","TORRENTPOWER"],
    "AUTO": ["BOSCHLTD","TIINDIA","HEROMOTOCO","M&M","EICHERMOT","EXIDEIND","BAJAJ-AUTO","ASHOKLEY","MARUTI","TITAGARH",
             "TVSMOTOR","MOTHERSON","SONACOMS","UNOMINDA","TATAMOTORS","BHARATFORG"],
    "IT": ["KAYNES","TATATECH","LTIM","CYIENT","MPHASIS","TCS","CAMS","OFSS","HFCL","TECHM","TATAELXSI","HCLTECH","WIPRO",
           "KPITTECH","COFORGE","PERSISTENT","INFY"],
    "PHARMA": ["CIPLA","ALKEM","BIOCON","DRREDDY","MANKIND","TORNTPHARM","ZYDUSLIFE","DIVISLAB","LUPIN","PPLPHARMA",
               "LAURUSLABS","FORTIS","AUROPHARMA","GLENMARK","SUNPHARMA"],
    "FMCG": ["ETERNAL","MARICO","NYKAA","NESTLEIND","VBL","COLPAL","HINDUNILVR","PATANJALI","DMART","DABUR","GODREJCP",
             "BRITANNIA","UNITDSPR","ITC","TATACONSUM","KALYANKJIL","SUPREMEIND"],
    "CEMENT": ["SHREECEM","DALBHARAT","AMBUJACEM","ULTRACEMCO"],
    "FINSERVICE": ["PNBHOUSING","BAJAJFINSV","ICICIPRULI","NUVAMA","HDFCLIFE","SAMMAANCAP","ANGELONE","RECLTD","BAJFINANCE",
                   "BSE","MAXHEALTH","ICICIGI","HUDCO","CHOLAFIN","PFC","HDFCAMC","MUTHOOTFIN","PAYTM","JIOFIN","SHRIRAMFIN",
                   "SBICARD","POLICYBZR","SBILIFE","LICHSGFIN","LICI","MANAPPURAM","IRFC","IIFL","CDSL"],
    "BANK": ["IDFCFIRSTB","FEDERALBNK","INDUSINDBK","HDFCBANK","SBIN","KOTAKBANK","AUBANK","CANBK","BANDHANBNK","RBLBANK",
             "ICICIBANK","AXISBANK"],
    "NIFTY_50": ["ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK","BAJAJ-AUTO","BAJFINANCE","BAJAJFINSV","BEL",
                "BHARTIARTL","CIPLA","COALINDIA","DRREDDY","EICHERMOT","GRASIM","HCLTECH","HDFCBANK","HDFCLIFE","HINDALCO",
                "HINDUNILVR","ICICIBANK","INFY","INDIGO","ITC","JIOFIN","JSWSTEEL","KOTAKBANK","LT","M&M","MARUTI","MAXHEALTH",
                "NESTLEIND","NTPC","ONGC","POWERGRID","RELIANCE","SBILIFE","SHRIRAMFIN","SBIN","SUNPHARMA","TCS","TATACONSUM",
                "TATASTEEL","TECHM","TITAN","TRENT","ULTRACEMCO","WIPRO","TATAMOTORS","ETERNAL"],
    "MIDCAP": ["RVNL","MPHASIS","HINDPETRO","PAGEIND","POLYCAB","LUPIN","IDFCFIRSTB","CONCOR","CUMMINSIND","VOLTAS",
               "BHARATFORG","FEDERALBNK","INDHOTEL","COFORGE","ASHOKLEY","PERSISTENT","UPL","GODREJPROP","AUROPHARMA","AUBANK",
               "ASTRAL","HDFCAMC","JUBLFOOD","PIIND"],
}
ALL_SYMBOLS = sorted(set(sum(SECTOR_DEFINITIONS.values(), [])))

# Instruments
ins = pd.DataFrame(kite.instruments("NSE"))
ins = ins[ins["tradingsymbol"].isin(ALL_SYMBOLS)].copy()
symbol_to_token: Dict[str, int] = dict(zip(ins["tradingsymbol"], ins["instrument_token"]))
symbol_to_name: Dict[str, str] = (
    dict(zip(ins["tradingsymbol"], ins["name"])) if "name" in ins.columns else {s: "" for s in ALL_SYMBOLS}
)
TOKENS = sorted(symbol_to_token.values())


# =============================================================================
# MARKET HOURS + EOD SNAPSHOT
# =============================================================================
def market_is_open_ist(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 15) <= t <= dtime(15, 30)


EOD_SNAPSHOT: Dict[int, Dict[str, Any]] = {}
# token -> {date, open, high, low, close, volume, prev_close}


# =============================================================================
# LIVE STATE
# =============================================================================
LOCK = threading.Lock()

LAST_PRICE: Dict[int, float] = {}
DAY_VOL: Dict[int, float] = {}
LAST_OHLC: Dict[int, dict] = {}

LAST_TICK_TS = 0.0
LAST_TICK_DT: Optional[datetime] = None
TOTAL_TICKS = 0

TPS_WINDOW_SEC = 1.0
TPS_BUCKETS = deque()

HOT_HISTORY: Dict[int, deque] = {}  # token -> deque[(epoch, ltp, cumvol)]


def _record_tick_batch(count: int, last_dt: Optional[datetime]):
    global LAST_TICK_TS, LAST_TICK_DT, TOTAL_TICKS
    now = time.time()
    TOTAL_TICKS += int(count)

    TPS_BUCKETS.append((now, int(count)))
    cutoff = now - TPS_WINDOW_SEC
    while TPS_BUCKETS and TPS_BUCKETS[0][0] < cutoff:
        TPS_BUCKETS.popleft()

    LAST_TICK_TS = now
    LAST_TICK_DT = last_dt or datetime.now()


def _get_tps() -> float:
    if not TPS_BUCKETS:
        return 0.0
    return sum(c for _, c in TPS_BUCKETS) / TPS_WINDOW_SEC


def _hot_history_push(token: int, epoch: float, ltp: float, cumvol: Optional[float]):
    dq = HOT_HISTORY.get(token)
    if dq is None:
        dq = deque()
        HOT_HISTORY[token] = dq

    if dq and (epoch - dq[-1][0]) < HOT_SAMPLE_SEC:
        last_epoch, _, last_vol = dq[-1]
        dq[-1] = (last_epoch, float(ltp), float(cumvol) if cumvol is not None else last_vol)
    else:
        dq.append((float(epoch), float(ltp), float(cumvol) if cumvol is not None else None))

    cutoff = epoch - HOT_HISTORY_MAX_SEC
    while dq and dq[0][0] < cutoff:
        dq.popleft()


def update_from_tick(tick: dict):
    token = tick["instrument_token"]
    ltp = tick.get("last_price")
    cumvol = tick.get("volume_traded")
    ohlc = tick.get("ohlc") or {}
    ts = tick.get("exchange_timestamp") or datetime.now()

    if ltp is None:
        return None

    LAST_PRICE[token] = float(ltp)
    if cumvol is not None:
        DAY_VOL[token] = float(cumvol)
    if ohlc:
        LAST_OHLC[token] = ohlc

    _hot_history_push(token, time.time(), float(ltp), float(cumvol) if cumvol is not None else None)
    return ts


# =============================================================================
# DAILY STATS + EOD SEED
# =============================================================================
DAILY_STATS: Dict[int, Dict[str, Optional[float]]] = {}
DAILY_SEED_STARTED = False
DAILY_SEED_DONE = False
DAILY_SEED_PROGRESS = {"done": 0, "total": len(TOKENS)}
DAILY_SEED_ERRORS = 0


def compute_20d_daily_stats_and_eod(token: int, days_back: int = 220) -> Dict[str, Any]:
    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=days_back)

    candles = kite.historical_data(
        instrument_token=token,
        from_date=from_dt,
        to_date=to_dt,
        interval="day",
        continuous=False,
        oi=False,
    )

    df = pd.DataFrame(candles)
    if df.empty or len(df) < LOOKBACK_SESSIONS + 2:
        return {"avg_vol_20": None, "avg_range_20": None, "avg_abs_oc_ret_20": None, "eod": None}

    df["date"] = pd.to_datetime(df["date"])
    df["d"] = df["date"].dt.date
    today_ist = datetime.now(IST).date()

    # During market, today's daily candle can be partial -> use previous completed day
    if market_is_open_ist() and df.iloc[-1]["d"] == today_ist:
        df = df.iloc[:-1].copy()

    if len(df) < LOOKBACK_SESSIONS + 1:
        return {"avg_vol_20": None, "avg_range_20": None, "avg_abs_oc_ret_20": None, "eod": None}

    last = df.iloc[-1]
    prev = df.iloc[-2]

    eod = {
        "date": last["d"],
        "open": float(last["open"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "close": float(last["close"]),
        "volume": float(last["volume"]),
        "prev_close": float(prev["close"]),
    }

    df_stats = df.tail(LOOKBACK_SESSIONS).copy()
    df_stats["range"] = (df_stats["high"] - df_stats["low"]).astype(float)
    df_stats["oc_ret_pct"] = (df_stats["close"] - df_stats["open"]) / df_stats["open"] * 100.0
    df_stats = df_stats.dropna()

    return {
        "avg_vol_20": float(df_stats["volume"].mean()) if not df_stats.empty else None,
        "avg_range_20": float(df_stats["range"].mean()) if not df_stats.empty else None,
        "avg_abs_oc_ret_20": float(df_stats["oc_ret_pct"].abs().mean()) if not df_stats.empty else None,
        "eod": eod,
    }


def seed_daily_stats_once(per_req_sleep: float = SEED_SLEEP_SEC):
    global DAILY_SEED_STARTED, DAILY_SEED_DONE, DAILY_SEED_ERRORS
    if DAILY_SEED_STARTED:
        return
    DAILY_SEED_STARTED = True

    def _run():
        global DAILY_SEED_DONE, DAILY_SEED_ERRORS
        DAILY_SEED_PROGRESS["total"] = len(TOKENS)
        DAILY_SEED_PROGRESS["done"] = 0

        for i, tok in enumerate(TOKENS, start=1):
            try:
                st = compute_20d_daily_stats_and_eod(tok)
            except Exception:
                DAILY_SEED_ERRORS += 1
                st = {"avg_vol_20": None, "avg_range_20": None, "avg_abs_oc_ret_20": None, "eod": None}

            with LOCK:
                DAILY_STATS[tok] = {
                    "avg_vol_20": st.get("avg_vol_20"),
                    "avg_range_20": st.get("avg_range_20"),
                    "avg_abs_oc_ret_20": st.get("avg_abs_oc_ret_20"),
                }
                if st.get("eod"):
                    EOD_SNAPSHOT[tok] = st["eod"]

            DAILY_SEED_PROGRESS["done"] = i
            time.sleep(per_req_sleep)

        DAILY_SEED_DONE = True

    threading.Thread(target=_run, daemon=True).start()


def get_live_or_eod_state(token: int) -> Optional[Tuple[float, float, dict]]:
    """
    Returns (ltp, vol_today, ohlc_dict):
      - live ticks if present
      - else EOD snapshot (last completed day)
    For EOD:
      ltp = close
      vol_today = volume
      ohlc.close = prev_close (for gap calc)
    """
    ltp = LAST_PRICE.get(token)
    vol_today = DAY_VOL.get(token)
    ohlc = LAST_OHLC.get(token) or {}

    if (
        ltp is not None
        and vol_today is not None
        and ohlc.get("open") is not None
        and ohlc.get("close") is not None
    ):
        return float(ltp), float(vol_today), ohlc

    e = EOD_SNAPSHOT.get(token)
    if not e or e.get("prev_close") is None:
        return None

    ohlc_eod = {"open": e["open"], "high": e["high"], "low": e["low"], "close": e["prev_close"]}
    return float(e["close"]), float(e["volume"]), ohlc_eod


# =============================================================================
# METRICS
# =============================================================================
def compute_rfactor_row_for_token(token: int):
    state = get_live_or_eod_state(token)
    if not state:
        return None

    ltp, vol_today, ohlc = state
    prev_close = ohlc.get("close")
    day_open = ohlc.get("open")
    day_high = ohlc.get("high")
    day_low = ohlc.get("low")

    if prev_close is None or day_open is None:
        return None

    prev_close = float(prev_close)
    day_open = float(day_open)
    if prev_close <= 0 or day_open <= 0 or ltp <= 0:
        return None

    gap_pct = ((day_open - prev_close) / prev_close) * 100.0
    pct_open = ((ltp - day_open) / day_open) * 100.0
    range_today = (float(day_high) - float(day_low)) if (day_high is not None and day_low is not None) else 0.0

    st = DAILY_STATS.get(token) or {}
    avg_vol_20 = st.get("avg_vol_20")
    avg_range_20 = st.get("avg_range_20")
    avg_abs_oc_ret_20 = st.get("avg_abs_oc_ret_20")
    if not avg_vol_20 or not avg_range_20 or not avg_abs_oc_ret_20:
        return None

    eps = 1e-9
    rvol = float(vol_today) / (float(avg_vol_20) + eps)
    range_factor = max(0.0, float(range_today)) / (float(avg_range_20) + eps)
    move_factor = abs(float(pct_open)) / (float(avg_abs_oc_ret_20) + eps)

    rfactor = rvol * range_factor * move_factor
    dirr = (1.0 if pct_open >= 0 else -1.0) * rfactor

    return {
        "gap_pct": gap_pct,
        "pct_open": pct_open,
        "rfactor": rfactor,
        "dirr": dirr,
        "ltp": float(ltp),
        "day_open": float(day_open),
        "vol_today": float(vol_today),
    }


def _compute_hot_row_for_token(token: int):
    dq = HOT_HISTORY.get(token)
    if not dq or len(dq) < 2:
        return None

    now_epoch = dq[-1][0]
    cutoff = now_epoch - HOT_WINDOW_SEC

    base = None
    for t, p, v in dq:
        if t <= cutoff:
            base = (t, p, v)
        else:
            break
    if base is None:
        base = dq[0]

    _, base_p, base_v = base
    _, last_p, last_v = dq[-1]

    if base_p is None or float(base_p) <= 0:
        return None

    ret15 = (float(last_p) - float(base_p)) / float(base_p) * 100.0

    vol15 = None
    if base_v is not None and last_v is not None:
        vol15 = float(last_v) - float(base_v)
        if vol15 < 0:
            vol15 = None

    st = DAILY_STATS.get(token) or {}
    avg_vol_20 = st.get("avg_vol_20") or None
    rvol15 = None
    if vol15 is not None and avg_vol_20 and float(avg_vol_20) > 0:
        session_sec = 22500.0
        expected_15 = float(avg_vol_20) * (HOT_WINDOW_SEC / session_sec)
        rvol15 = float(vol15) / (expected_15 + 1e-9)

    score = abs(ret15) * (rvol15 if rvol15 is not None else 1.0)
    return {"ret15": ret15, "vol15": vol15, "score": score}


# =============================================================================
# TABLE BUILDERS
# =============================================================================
def top_gainers_losers_rfactor_rows(n: int = 15):
    rows = []
    for sym in ALL_SYMBOLS:
        tok = symbol_to_token.get(sym)
        if not tok:
            continue
        rr = compute_rfactor_row_for_token(tok)
        if not rr:
            continue
        rows.append({
            "Symbol": sym,
            "%Change": round(float(rr["pct_open"]), 2),
            "RFactor": round(float(rr["rfactor"]), 2),
            "Vol": int(rr["vol_today"]),
        })

    if not rows:
        return [], []

    df = pd.DataFrame(rows)
    gainers = df[df["%Change"] > 0].sort_values("RFactor", ascending=False).head(n).to_dict("records")
    losers = df[df["%Change"] < 0].sort_values("RFactor", ascending=False).head(n).to_dict("records")
    return gainers, losers


def top_hot_now_rows(n: int = 15):
    rows = []
    for sym in ALL_SYMBOLS:
        tok = symbol_to_token.get(sym)
        if not tok:
            continue
        hr = _compute_hot_row_for_token(tok)
        if not hr:
            continue

        rr = compute_rfactor_row_for_token(tok)
        rfac = rr["rfactor"] if rr else None

        rows.append({
            "Symbol": sym,
            "%Change": round(float(hr["ret15"]), 2),
            "RFactor": (round(float(rfac), 2) if rfac is not None else None),
            "Vol": (int(hr["vol15"]) if hr["vol15"] is not None else None),
            "_score": float(hr["score"]),
        })

    if not rows:
        return [], []

    df = pd.DataFrame(rows).dropna(subset=["%Change", "_score"])
    gainers = (
        df[df["%Change"] > 0]
        .sort_values("_score", ascending=False)
        .head(n)[["Symbol", "%Change", "RFactor", "Vol"]]
        .to_dict("records")
    )
    losers = (
        df[df["%Change"] < 0]
        .sort_values("_score", ascending=False)
        .head(n)[["Symbol", "%Change", "RFactor", "Vol"]]
        .to_dict("records")
    )
    return gainers, losers


def compute_sector_dirr_mean():
    out = {}
    for sector, syms in SECTOR_DEFINITIONS.items():
        vals = []
        for s in syms:
            tok = symbol_to_token.get(s)
            if not tok:
                continue
            rr = compute_rfactor_row_for_token(tok)
            if rr and rr.get("dirr") is not None:
                vals.append(float(rr["dirr"]))
        out[sector] = float(pd.Series(vals).mean()) if vals else 0.0
    return out


def sector_rows_sorted_by_rfactor(sector: str):
    rows = []
    for s in SECTOR_DEFINITIONS.get(sector, []):
        tok = symbol_to_token.get(s)
        if not tok:
            continue
        rr = compute_rfactor_row_for_token(tok)
        if not rr:
            continue

        ltp = rr["ltp"]
        day_open = rr["day_open"]
        chg_open = ltp - day_open

        rows.append({
            "Symbol": s,
            "Company": symbol_to_name.get(s, ""),
            "Price": round(float(ltp), 2),
            "Chg (O)": round(float(chg_open), 2),
            "Gap%": round(float(rr["gap_pct"]), 2),
            "Chg% (O)": round(float(rr["pct_open"]), 2),
            "RFactor": round(float(rr["rfactor"]), 2),
            "DirR": round(float(rr["dirr"]), 2),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return []
    return df.sort_values("RFactor", ascending=False, na_position="last").to_dict("records")


# =============================================================================
# DIALS — SENTIMENT + PCR (proxy)
# =============================================================================
def compute_market_sentiment_and_pcr_proxy():
    adv = dec = unch = 0
    up_vol = 0.0
    down_vol = 0.0

    for tok in TOKENS:
        state = get_live_or_eod_state(tok)
        if not state:
            continue
        ltp, vol, ohlc = state
        op = ohlc.get("open")
        if op is None:
            continue

        try:
            opf = float(op)
            ltp = float(ltp)
            volf = float(vol) if vol is not None else 0.0
        except Exception:
            continue

        if opf <= 0 or ltp <= 0:
            continue

        pct_open = (ltp - opf) / opf * 100.0

        if pct_open > 0:
            adv += 1
            up_vol += volf
        elif pct_open < 0:
            dec += 1
            down_vol += volf
        else:
            unch += 1

    total = adv + dec + unch
    score = (adv - dec) / total if total > 0 else 0.0

    if score >= 0.20:
        label = "BULLISH"
    elif score <= -0.20:
        label = "BEARISH"
    else:
        label = "NEUTRAL"

    eps = 1e-9
    pcr = (up_vol / (down_vol + eps)) if (up_vol > 0 or down_vol > 0) else 1.0
    if not isfinite(pcr) or pcr < 0:
        pcr = 1.0

    if pcr >= 1.40:
        pcr_label = "STRONG BUY"
    elif pcr >= 1.10:
        pcr_label = "BUY"
    elif pcr >= 0.90:
        pcr_label = "NEUTRAL"
    elif pcr >= 0.60:
        pcr_label = "SELL"
    else:
        pcr_label = "STRONG SELL"

    return {
        "adv": adv, "dec": dec, "unch": unch, "total": total,
        "score": float(score), "label": label,
        "up_vol": float(up_vol), "down_vol": float(down_vol),
        "pcr": float(pcr), "pcr_label": pcr_label,
    }


# =============================================================================
# TICKER (AUTO-RESTART)
# =============================================================================
_started = False


def start_ticker_once():
    global _started
    if _started:
        return
    _started = True

    def _run():
        while True:
            try:
                kws = KiteTicker(API_KEY, ACCESS_TOKEN)

                def on_connect(ws, _):
                    log.info("WS CONNECTED")
                    ws.subscribe(TOKENS)
                    ws.set_mode(ws.MODE_FULL, TOKENS)

                def on_ticks(ws, ticks):
                    try:
                        last_dt = None
                        with LOCK:
                            for t in ticks:
                                ts = update_from_tick(t)
                                if ts and (last_dt is None or ts > last_dt):
                                    last_dt = ts
                            _record_tick_batch(len(ticks), last_dt)
                    except Exception:
                        log.exception("on_ticks crashed")

                def on_close(ws, code, reason):
                    log.warning("WS CLOSED: %s %s", code, reason)

                def on_error(ws, code, reason):
                    log.error("WS ERROR: %s %s", code, reason)

                kws.on_connect = on_connect
                kws.on_ticks = on_ticks
                kws.on_close = on_close
                kws.on_error = on_error

                kws.connect(threaded=True)
                while True:
                    time.sleep(2)

            except Exception:
                log.exception("Ticker loop crashed; restarting in 5s")
                time.sleep(5)

    threading.Thread(target=_run, daemon=True).start()


# =============================================================================
# DASH APP (mounted at /dash)
# =============================================================================
dash_app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.CYBORG],
    requests_pathname_prefix=BASE,  # browser hits /dash/_dash-*
    routes_pathname_prefix="/",     # mount strips /dash
    assets_folder=os.path.join(os.path.dirname(__file__), "assets"),
    suppress_callback_exceptions=True,
)
server = dash_app.server


# ------------------- UI Components -------------------
def dial_component(prefix: str, title: str):
    return html.Div(
        html.Div(
            [
                html.Div(
                    [
                        html.Div([html.Div(className="dial-arc")], className="dial-arc-clip"),
                        html.Div(id=f"{prefix}-needle", className="dial-needle", style={"--rot": "0deg"}),
                        html.Div(className="dial-center"),
                        html.Div(["STRONG", html.Br(), "SELL"], className="dial-label dial-ss"),
                        html.Div("SELL", className="dial-label dial-s"),
                        html.Div("NEUTRAL", className="dial-label dial-n"),
                        html.Div("BUY", className="dial-label dial-b"),
                        html.Div(["STRONG", html.Br(), "BUY"], className="dial-label dial-sb"),
                    ],
                    className="dial-arc-wrap",
                ),
                html.Div(title, className="dial-title"),
                html.Div("—", id=f"{prefix}-sub", className="dial-sub"),
            ],
            className="dial-card",
        )
    )


def sectors_page():
    four_cols = [
        {"field": "Symbol", "headerName": "Stock", "cellRenderer": "SymbolCell", "minWidth": 130, "flex": 1},
        {"field": "%Change", "headerName": "%Chg", "type": "rightAligned", "minWidth": 95, "flex": 1, "cellClass": "cell-num", "cellRenderer": "PctPill"},
        {"field": "RFactor", "headerName": "RFactor", "type": "rightAligned", "minWidth": 95, "flex": 1, "cellClass": "cell-num", "cellRenderer": "RfactorPill"},
        {"field": "Vol", "headerName": "Vol", "type": "rightAligned", "minWidth": 110, "flex": 1, "cellClass": "cell-num", "cellRenderer": "VolPill"},
    ]

    grid_opts = {
        "getRowId": {"function": "params.data.Symbol"},
        "alwaysShowVerticalScroll": True,
        "animateRows": True,
        "onGridReady": {"function": "params.api.sizeColumnsToFit();"},
        "onGridSizeChanged": {"function": "params.api.sizeColumnsToFit();"},
    }

    return html.Div(
        [
            dcc.Interval(id="refresh_sectors", interval=2000, n_intervals=0),

            dbc.Row(
                [dbc.Col(dial_component("sentiment", "Sentimental Dial"), md=6),
                 dbc.Col(dial_component("pcr", "PCR"), md=6)],
                className="g-2 dials-row",
            ),

            html.Hr(),

            dbc.Row(
                [
                    dbc.Col(
                        [
                            html.H6("Top 15 Gainers (sorted by RFactor)", className="mt-1"),
                            dag.AgGrid(
                                id="top15-gainers-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=four_cols,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(520px, 48vh)", "width": "100%"},
                            ),
                        ],
                        md=6,
                    ),
                    dbc.Col(
                        [
                            html.H6("Top 15 Losers (sorted by RFactor)", className="mt-1"),
                            dag.AgGrid(
                                id="top15-losers-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=four_cols,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(520px, 48vh)", "width": "100%"},
                            ),
                        ],
                        md=6,
                    ),
                ],
                className="g-2",
            ),

            html.Hr(),

            html.H4("Sectors", className="page-title"),
            html.Div(id="sector-bars", className="sector-bars-wrap"),
            html.Div("Sectors sorted by AVG DirR (strength). Top tables sorted by RFactor.", className="hint"),

            html.Hr(),

            dbc.Row(
                [
                    dbc.Col(
                        [
                            html.H6("Hot Now (last 15m) — Gainers", className="mt-1"),
                            dag.AgGrid(
                                id="hot15-gainers-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=four_cols,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(520px, 48vh)", "width": "100%"},
                            ),
                        ],
                        md=6,
                    ),
                    dbc.Col(
                        [
                            html.H6("Hot Now (last 15m) — Losers", className="mt-1"),
                            dag.AgGrid(
                                id="hot15-losers-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=four_cols,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(520px, 48vh)", "width": "100%"},
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


def sector_page(sector: str):
    return html.Div(
        [
            dcc.Interval(id="refresh_sector", interval=2000, n_intervals=0),
            dbc.Row(
                [
                    dbc.Col(html.H4(f"{sector} Stocks", className="page-title"), width=True),
                    dbc.Col(dbc.Button("Back", href=f"{BASE}", color="secondary", outline=True, className="btn-back"), width="auto"),
                ],
                className="align-items-center g-2",
            ),
            dag.AgGrid(
                id="grid",
                className="ag-theme-alpine-dark grid-wrap",
                columnDefs=[
                    {"field": "Symbol", "headerName": "Stock", "pinned": "left", "cellRenderer": "StockCell", "minWidth": 200},
                    {"field": "Price", "type": "rightAligned", "valueFormatter": {"function": "fmt2(params.value)"}},
                    {"field": "Chg (O)", "type": "rightAligned", "valueFormatter": {"function": "fmtSigned2(params.value)"},
                     "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"}},
                    {"field": "Gap%", "type": "rightAligned", "valueFormatter": {"function": "fmtPct(params.value)"},
                     "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"}},
                    {"field": "Chg% (O)", "type": "rightAligned", "valueFormatter": {"function": "fmtPct(params.value)"},
                     "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"}},
                    {"field": "RFactor", "type": "rightAligned", "valueFormatter": {"function": "fmt2(params.value)"}, "sort": "desc"},
                    {"field": "DirR", "type": "rightAligned", "valueFormatter": {"function": "fmtSigned2(params.value)"},
                     "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"}},
                ],
                rowData=[],
                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                dashGridOptions={"alwaysShowVerticalScroll": True, "animateRows": True},
                style={"height": "72vh", "width": "100%"},
            ),
        ],
        className="page-wrap",
    )


# ------------------- Dash Layout -------------------
dash_app.layout = dbc.Container(
    fluid=True,
    children=[
        dcc.Location(id="url"),
        dcc.Interval(id="top_refresh", interval=1000, n_intervals=0),

        html.Div(
            dbc.Row(
                [
                    dbc.Col(html.Div("TurboTrades", className="top-tab"), width=True),
                    dbc.Col(html.Div(id="top-stats"), width="auto"),
                ],
                className="align-items-center g-2",
            ),
            className="topbar-wrap",
        ),

        html.Div(id="app-body"),
    ],
)


# ------------------- Dash Routing -------------------
@dash_app.callback(Output("app-body", "children"), Input("url", "pathname"))
def route(pathname):
    pn = (pathname or "").strip() or "/"

    if pn in ("/", "/dash", "/dash/", BASE):
        return sectors_page()

    if pn.startswith(f"{BASE}sector/"):
        sector = unquote(pn.split(f"{BASE}sector/")[1]).upper()
        return sector_page(sector) if sector in SECTOR_DEFINITIONS else dbc.Alert("Sector not found", color="danger")

    return sectors_page()


# ------------------- TOP HEADER STATS (ONLY PLACE SEEDING IS SHOWN) -------------------
@dash_app.callback(Output("top-stats", "children"), Input("top_refresh", "n_intervals"))
def update_top_stats(_):
    updated_str = datetime.now(IST).strftime("%H:%M:%S")

    with LOCK:
        offline = (time.time() - LAST_TICK_TS) > 10 if LAST_TICK_TS else True
        tps = _get_tps()
        tot = TOTAL_TICKS

        d_done = DAILY_SEED_DONE
        d_done_n = DAILY_SEED_PROGRESS.get("done", 0)
        d_total = DAILY_SEED_PROGRESS.get("total", 0)
        d_err = DAILY_SEED_ERRORS

        eod_date = None
        if EOD_SNAPSHOT:
            any_tok = next(iter(EOD_SNAPSHOT.keys()))
            eod_date = EOD_SNAPSHOT[any_tok].get("date")

    chips = [
        dbc.Badge("Offline" if offline else "Live", color=("danger" if offline else "success"), className="stat-badge"),
    ]

    # Show seeding ONLY here, beside Live/Offline
    if not d_done:
        chips.append(dbc.Badge(f"Seeding {d_done_n}/{d_total} (err {d_err})", color="warning", className="stat-badge"))

    chips += [
        html.Div(f"TPS {tps:.1f}", className="stat-chip"),
        html.Div(f"Ticks {tot:,}", className="stat-chip"),
    ]

    if eod_date:
        chips.append(html.Div(f"EOD {eod_date}", className="stat-chip"))

    chips.append(html.Div(f"Updated {updated_str}", className="stat-chip"))
    return html.Div(chips, className="top-stats-wrap")


# ------------------- Sector Bars -------------------
@dash_app.callback(Output("sector-bars", "children"), Input("refresh_sectors", "n_intervals"))
def render_sector_bars(_):
    with LOCK:
        scores = compute_sector_dirr_mean()

    items = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    max_abs = max([abs(v) for _, v in items] + [1e-6])

    children = []
    for sector, val in items:
        h = int(10 + 150 * (abs(val) / max_abs))
        h = min(h, 150)
        cls = "bar-green" if val >= 0 else "bar-red"
        label = f"{val:+.2f}×"

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


# ------------------- DIALS (Exact sentiment + PCR proxy mapping) -------------------
@dash_app.callback(
    Output("sentiment-needle", "style"),
    Output("sentiment-sub", "children"),
    Output("pcr-needle", "style"),
    Output("pcr-sub", "children"),
    Input("refresh_sectors", "n_intervals"),
)
def update_dials(_):
    with LOCK:
        m = compute_market_sentiment_and_pcr_proxy()

    # Sentiment: score [-1..+1] -> angle [-90..+90]
    score = float(m["score"])
    sent_angle = max(-90.0, min(90.0, score * 90.0))
    sent_style = {"--rot": f"{sent_angle:.2f}deg"}
    sent_sub = f'{m["label"]} • {score:+.2f} • {m["adv"]}/{m["dec"]}'

    # PCR proxy: clamp [0..2], map 0->-90, 1->0, 2->+90
    pcr = float(m["pcr"])
    pcr_clamped = max(0.0, min(2.0, pcr))
    pcr_angle = (pcr_clamped - 1.0) * 90.0
    pcr_style = {"--rot": f"{pcr_angle:.2f}deg"}
    pcr_sub = f'{m["pcr_label"]} • {pcr:.2f}'

    return sent_style, sent_sub, pcr_style, pcr_sub


# ------------------- Grids -------------------
@dash_app.callback(
    Output("top15-gainers-grid", "rowData"),
    Output("top15-losers-grid", "rowData"),
    Input("refresh_sectors", "n_intervals"),
)
def update_rfactor_leaderboards(_):
    with LOCK:
        return top_gainers_losers_rfactor_rows(n=15)


@dash_app.callback(
    Output("hot15-gainers-grid", "rowData"),
    Output("hot15-losers-grid", "rowData"),
    Input("refresh_sectors", "n_intervals"),
)
def update_hot_now(_):
    with LOCK:
        return top_hot_now_rows(n=15)


@dash_app.callback(
    Output("grid", "rowData"),
    Input("refresh_sector", "n_intervals"),
    Input("url", "pathname"),
)
def update_grid(_, pathname):
    pn = (pathname or "").strip()
    if not pn.startswith(f"{BASE}sector/"):
        return dash.no_update

    sector = unquote(pn.split(f"{BASE}sector/")[1]).upper()
    if sector not in SECTOR_DEFINITIONS:
        return []

    with LOCK:
        return sector_rows_sorted_by_rfactor(sector)


# =============================================================================
# FASTAPI APP
# =============================================================================
app = FastAPI(title="TurboTrades (No Auth)")

HERE = Path(__file__).resolve().parent
THEME_PATH = HERE / "assets" / "theme.css"


@app.on_event("startup")
def _startup():
    seed_daily_stats_once(per_req_sleep=SEED_SLEEP_SEC)
    start_ticker_once()


@app.get("/dash")
def dash_no_slash():
    return RedirectResponse(url="/dash/", status_code=307)


@app.get("/health")
def health():
    with LOCK:
        return {
            "status": "ok",
            "seed_20d_done": DAILY_SEED_DONE,
            "seed_20d_progress": DAILY_SEED_PROGRESS,
            "seed_20d_errors": DAILY_SEED_ERRORS,
            "tps": round(_get_tps(), 3),
            "total_ticks": TOTAL_TICKS,
            "last_tick_time": (LAST_TICK_DT.isoformat() if LAST_TICK_DT else None),
            "hot_history_tokens": len(HOT_HISTORY),
            "eod_tokens": len(EOD_SNAPSHOT),
        }


@app.get("/theme.css")
def theme_css():
    if THEME_PATH.exists():
        return FileResponse(THEME_PATH, media_type="text/css")
    return JSONResponse({"error": "theme.css not found"}, status_code=404)


@app.get("/")
def root():
    return RedirectResponse(url="/dash/", status_code=307)


# Mount Dash (no auth)
app.mount("/dash", WSGIMiddleware(server))