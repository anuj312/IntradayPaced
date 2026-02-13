# app.py  (NO AUTH • NO FIREBASE • NO SUBSCRIPTIONS)
#
# Dashboard: http://127.0.0.1:8000/dash/
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
from typing import Optional, Dict, Any, Tuple, List
from urllib.parse import unquote
from zoneinfo import ZoneInfo
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
from rotation import app as rotation_app

# NEW: Volm page lives in web.py (Dash "plugin" page)
import web


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

HVHR_N = int(os.getenv("HVHR_N", "20"))
HVHR_RFACTOR_Q = float(os.getenv("HVHR_RFACTOR_Q", "0.85"))

PCR_STRIKES_AROUND_ATM = int(os.getenv("PCR_STRIKES_AROUND_ATM", "12"))
PCR_CACHE_TTL_SEC = int(os.getenv("PCR_CACHE_TTL_SEC", "20"))
PCR_QUOTE_CHUNK = int(os.getenv("PCR_QUOTE_CHUNK", "180"))

NIFTY_SPOT_SYMBOL = os.getenv("NIFTY_SPOT_SYMBOL", "NSE:NIFTY 50")


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
               "IOC","IGL","TATAPOWER","INOXWIND","MAZDOCK","PETRONET","SOLARINDS", "PREMIERENE","ADANIGREEN","NTPC","OIL","BDL","BPCL",
               "NHPC","POWERINDIA","ADANIENSOL","TORNTPOWER"],
    "AUTO": ["BOSCHLTD","TIINDIA","HEROMOTOCO","M&M","EICHERMOT","EXIDEIND","BAJAJ-AUTO","ASHOKLEY","MARUTI","TITAGARH",
             "TVSMOTOR","MOTHERSON","SONACOMS","UNOMINDA","TMPV","BHARATFORG"],
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

ins = pd.DataFrame(kite.instruments("NSE"))
ins = ins[ins["tradingsymbol"].isin(ALL_SYMBOLS)].copy()
symbol_to_token: Dict[str, int] = dict(zip(ins["tradingsymbol"], ins["instrument_token"]))
symbol_to_name: Dict[str, str] = (
    dict(zip(ins["tradingsymbol"], ins["name"])) if "name" in ins.columns else {s: "" for s in ALL_SYMBOLS}
)
TOKENS = sorted(symbol_to_token.values())


# =============================================================================
# LIVE / STATE
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

EOD_SNAPSHOT: Dict[int, Dict[str, Any]] = {}  # token -> eod dict
DAILY_STATS: Dict[int, Dict[str, Optional[float]]] = {}

DAILY_SEED_STARTED = False
DAILY_SEED_DONE = False
DAILY_SEED_PROGRESS = {"done": 0, "total": len(TOKENS)}
DAILY_SEED_ERRORS = 0


def market_is_open_ist(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 15) <= t <= dtime(15, 30)


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
# PCR (NFO)
# =============================================================================
NFO_INS_DF: Optional[pd.DataFrame] = None
NFO_LOAD_STARTED = False
NFO_LOAD_ERR: Optional[str] = None
PCR_CACHE: Dict[str, Tuple[dict, float]] = {}


def load_nfo_instruments_once():
    global NFO_LOAD_STARTED
    if NFO_LOAD_STARTED:
        return
    NFO_LOAD_STARTED = True

    def _run():
        global NFO_INS_DF, NFO_LOAD_ERR
        try:
            df = pd.DataFrame(kite.instruments("NFO"))
            df = df[df["instrument_type"].isin(["CE", "PE"])].copy()
            df = df[df["name"] == "NIFTY"].copy()
            df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
            NFO_INS_DF = df
            log.info("Loaded NFO instruments (NIFTY only): %s rows", len(df))
        except Exception as e:
            NFO_LOAD_ERR = repr(e)
            log.exception("Failed to load NFO instruments")

    threading.Thread(target=_run, daemon=True).start()


def _chunk(lst: List[str], n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def _quote_many(keys: List[str], chunk_size: int = PCR_QUOTE_CHUNK) -> dict:
    out = {}
    for ch in _chunk(keys, chunk_size):
        out.update(kite.quote(ch))
    return out


def _infer_strike_step(strikes: pd.Series) -> float:
    s = sorted(set(float(x) for x in strikes.dropna().tolist()))
    if len(s) < 3:
        return 50.0
    diffs = [b - a for a, b in zip(s, s[1:]) if (b - a) > 0]
    if not diffs:
        return 50.0
    diffs.sort()
    return float(diffs[len(diffs) // 2])


def compute_real_nifty_oi_pcr(strikes_around_atm: int = PCR_STRIKES_AROUND_ATM) -> Optional[dict]:
    cache_key = f"NIFTY:oi:{strikes_around_atm}"
    cached = PCR_CACHE.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0]

    if NFO_LOAD_ERR or NFO_INS_DF is None:
        return None

    try:
        spot = float(kite.ltp([NIFTY_SPOT_SYMBOL])[NIFTY_SPOT_SYMBOL]["last_price"])
    except Exception:
        return None

    dfu = NFO_INS_DF
    if dfu.empty:
        return None

    expiry = min(dfu["expiry"].tolist()) if len(dfu) else None
    if not expiry:
        return None

    dfe = dfu[dfu["expiry"] == expiry].copy()
    if dfe.empty:
        return None

    step = _infer_strike_step(dfe["strike"])
    atm = round(spot / step) * step

    lo = atm - strikes_around_atm * step
    hi = atm + strikes_around_atm * step
    dfe = dfe[(dfe["strike"] >= lo) & (dfe["strike"] <= hi)].copy()
    if dfe.empty:
        return None

    ce = dfe[dfe["instrument_type"] == "CE"]
    pe = dfe[dfe["instrument_type"] == "PE"]

    ce_keys = ["NFO:" + s for s in ce["tradingsymbol"].tolist()]
    pe_keys = ["NFO:" + s for s in pe["tradingsymbol"].tolist()]
    keys = ce_keys + pe_keys
    if not keys:
        return None

    try:
        q = _quote_many(keys, chunk_size=PCR_QUOTE_CHUNK)
    except Exception:
        return None

    ce_oi = sum(float(q.get(k, {}).get("oi") or 0.0) for k in ce_keys)
    pe_oi = sum(float(q.get(k, {}).get("oi") or 0.0) for k in pe_keys)
    pcr = pe_oi / (ce_oi + 1e-9)

    data = {
        "underlying": "NIFTY",
        "expiry": str(expiry),
        "spot": spot,
        "atm": atm,
        "step": step,
        "range": [float(lo), float(hi)],
        "ce_oi": float(ce_oi),
        "pe_oi": float(pe_oi),
        "pcr": float(pcr),
        "strikes": int(len(dfe)),
        "updated_at": datetime.now(IST).strftime("%H:%M:%S"),
    }

    PCR_CACHE[cache_key] = (data, time.time() + PCR_CACHE_TTL_SEC)
    return data


def pcr_label_from_value(pcr: float) -> str:
    if pcr >= 1.40:
        return "STRONG BUY"
    if pcr >= 1.10:
        return "BUY"
    if pcr >= 0.90:
        return "NEUTRAL"
    if pcr >= 0.60:
        return "SELL"
    return "STRONG SELL"


# =============================================================================
# METRICS / TABLE BUILDERS
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


def high_vol_high_rfactor_gainers_losers(n: int = HVHR_N, rfactor_quantile: float = HVHR_RFACTOR_Q):
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

    df = pd.DataFrame(rows).dropna(subset=["RFactor", "Vol", "%Change"])
    if df.empty:
        return [], []

    q = min(max(float(rfactor_quantile), 0.0), 1.0)
    thr = float(df["RFactor"].quantile(q)) if len(df) >= 3 else float(df["RFactor"].min())
    df = df[df["RFactor"] >= thr]

    gainers = (
        df[df["%Change"] > 0]
        .sort_values(["Vol", "RFactor"], ascending=[False, False])
        .head(int(n))
        .to_dict("records")
    )
    losers = (
        df[df["%Change"] < 0]
        .sort_values(["Vol", "RFactor"], ascending=[False, False])
        .head(int(n))
        .to_dict("records")
    )
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
# SENTIMENT
# =============================================================================
def compute_market_sentiment_proxy():
    adv = dec = unch = 0

    for tok in TOKENS:
        state = get_live_or_eod_state(tok)
        if not state:
            continue
        ltp, _, ohlc = state
        op = ohlc.get("open")
        if op is None:
            continue

        try:
            opf = float(op)
            ltp = float(ltp)
        except Exception:
            continue

        if opf <= 0 or ltp <= 0:
            continue

        pct_open = (ltp - opf) / opf * 100.0
        if pct_open > 0:
            adv += 1
        elif pct_open < 0:
            dec += 1
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

    return {"adv": adv, "dec": dec, "unch": unch, "total": total, "score": float(score), "label": label}


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

                kws.on_connect = on_connect
                kws.on_ticks = on_ticks
                kws.connect(threaded=True)

                while True:
                    time.sleep(2)

            except Exception:
                log.exception("Ticker loop crashed; restarting in 5s")
                time.sleep(5)

    threading.Thread(target=_run, daemon=True).start()


# =============================================================================
# DASH APP
# =============================================================================
dash_app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.CYBORG],
    requests_pathname_prefix=BASE,
    routes_pathname_prefix="/",
    assets_folder=os.path.join(os.path.dirname(__file__), "assets"),
    suppress_callback_exceptions=True,
)
server = dash_app.server

# NEW: register Volm page callbacks (uses the same WS/state from this app.py)
web.register_volm(
    dash_app,
    BASE=BASE,
    ctx={
        "LOCK": LOCK,
        "ALL_SYMBOLS": ALL_SYMBOLS,
        "symbol_to_token": symbol_to_token,
        "DAILY_STATS": DAILY_STATS,
        "get_live_or_eod_state": get_live_or_eod_state,
        "IST": IST,
    },
)


def dial_component(prefix: str, title: str):
    return html.Div(
        html.Div(
            [
                html.Div(
                    [
                        html.Div([html.Div(className=f"dial-arc dial-arc-{prefix}")], className="dial-arc-clip"),
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
            className=f"dial-card dial-{prefix}",
        )
    )


def sectors_page():
    four_cols = [
        {
            "colId": "stock",
            "field": "Symbol",
            "headerName": "STOCK",
            "cellRenderer": "SymbolCell",
            "minWidth": 10,
            "flex": 1,
            "headerClass": "h-left",
            "cellClass": "c-left",
        },
        {
            "colId": "pctChg",
            "field": "%Change",
            "headerName": "%CHG",
            "cellRenderer": "PctPill",
            "width": 10,
            "minWidth": 150,
            "maxWidth": 150,
            "suppressSizeToFit": True,
            "headerClass": "ag-right-aligned-header h-right",
            "cellClass": "ag-right-aligned-cell cell-num c-right",
        },
        {
            "colId": "rfactor",
            "field": "RFactor",
            "headerName": "RFACTOR",
            "cellRenderer": "RfactorPill",
            "width": 10,
            "minWidth": 125,
            "maxWidth": 170,
            "suppressSizeToFit": True,
            "headerClass": "ag-right-aligned-header h-right",
            "cellClass": "ag-right-aligned-cell cell-num c-right",
        },
        {
            "colId": "volume",
            "field": "Vol",
            "headerName": "VOLUME",
            "cellRenderer": "VolPill",
            "width": 5,
            "minWidth": 140,
            "maxWidth": 190,
            "suppressSizeToFit": True,
            "headerClass": "ag-right-aligned-header h-right",
            "cellClass": "ag-right-aligned-cell cell-num c-right",
        },
    ]

    grid_opts = {
        "getRowId": {"function": "params.data.Symbol"},
        "alwaysShowVerticalScroll": False,
        "animateRows": False,
        "suppressMenuHide": False,
        "suppressHeaderMenuButton": False,
        "suppressHeaderFilterButton": False,
        "onGridReady": {"function": "params.api.sizeColumnsToFit();"},
        "onGridSizeChanged": {"function": "params.api.sizeColumnsToFit();"},
    }

    top_bucket_pct = int((1.0 - float(HVHR_RFACTOR_Q)) * 100)

    return html.Div(
        [
            dcc.Interval(id="refresh_sectors", interval=2000, n_intervals=0),

            dbc.Row(
                [
                    dbc.Col(dial_component("sentiment", "Sentiment Dial"), md=6),
                    dbc.Col(dial_component("pcr", "NIFTY OI PCR"), md=6),
                ],
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
                            html.H6(
                                f"High Vol + High RFactor — Gainers (Top {top_bucket_pct}% RFactor bucket, sorted by Vol)",
                                className="mt-1",
                            ),
                            dag.AgGrid(
                                id="hvhr-gainers-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=four_cols,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(520px, 44vh)", "width": "100%"},
                            ),
                        ],
                        md=6,
                    ),
                    dbc.Col(
                        [
                            html.H6(
                                f"High Vol + High RFactor — Losers (Top {top_bucket_pct}% RFactor bucket, sorted by Vol)",
                                className="mt-1",
                            ),
                            dag.AgGrid(
                                id="hvhr-losers-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=four_cols,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(520px, 44vh)", "width": "100%"},
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
                    {"colId": "stock", "field": "Symbol", "headerName": "STOCK", "pinned": "left",
                     "cellRenderer": "StockCell", "minWidth": 260},

                    {"colId": "price", "field": "Price", "headerName": "PRICE", "type": "rightAligned",
                     "valueFormatter": {"function": "fmt2(params.value)"}, "minWidth": 120, "flex": 1},

                    {"colId": "chgO", "field": "Chg (O)", "headerName": "CHG (O)", "type": "rightAligned",
                     "valueFormatter": {"function": "fmtSigned2(params.value)"},
                     "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0", "cell-zero": "params.value === 0"},
                     "minWidth": 130, "flex": 1},

                    {"colId": "gapPct", "field": "Gap%", "headerName": "GAP%", "type": "rightAligned",
                     "valueFormatter": {"function": "fmtPct(params.value)"},
                     "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0", "cell-zero": "params.value === 0"},
                     "minWidth": 115, "flex": 1},

                    {"colId": "chgPctO", "field": "Chg% (O)", "headerName": "CHG% (O)", "type": "rightAligned",
                     "valueFormatter": {"function": "fmtPct(params.value)"},
                     "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0", "cell-zero": "params.value === 0"},
                     "minWidth": 140, "flex": 1},

                    {"colId": "rfactor", "field": "RFactor", "headerName": "RFACTOR", "type": "rightAligned",
                     "valueFormatter": {"function": "fmt2(params.value)"}, "sort": "desc",
                     "minWidth": 130, "flex": 1},

                    {"colId": "dirr", "field": "DirR", "headerName": "DIR R", "type": "rightAligned",
                     "valueFormatter": {"function": "fmtSigned2(params.value)"},
                     "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0", "cell-zero": "params.value === 0"},
                     "minWidth": 120, "flex": 1},
                ],
                rowData=[],
                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                dashGridOptions={
                    "domLayout": "autoHeight",
                    "animateRows": True,
                    "suppressMenuHide": True,
                    "suppressHeaderMenuButton": False,
                    "suppressHeaderFilterButton": False,
                    "alwaysShowVerticalScroll": False,
                },
                style={"height": "auto", "width": "100%"},
            ),
        ],
        className="page-wrap",
    )


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


@dash_app.callback(Output("app-body", "children"), Input("url", "pathname"))
def route(pathname):
    pn = (pathname or "").strip() or "/"

    # Home
    if pn in ("/", "/dash", "/dash/", BASE):
        return sectors_page()

    # NEW: Volm page (from web.py)
    if pn in (f"{BASE}volm", f"{BASE}volm/"):
        return web.volm_page(BASE)

    # Sector
    if pn.startswith(f"{BASE}sector/"):
        sector = unquote(pn.split(f"{BASE}sector/")[1]).upper()
        return sector_page(sector) if sector in SECTOR_DEFINITIONS else dbc.Alert("Sector not found", color="danger")

    return sectors_page()


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

    chips = [
        dbc.Badge(
            "Offline" if offline else "Live",
            color=("danger" if offline else "success"),
            className="stat-badge",
        ),
    ]

    chips.append(
    html.A(
        "Volm",
        href=f"{BASE}volm",
        target="_blank",
        className="stat-chip",
        style={"textDecoration": "none", "marginLeft": "8px", "cursor": "pointer"},
    )
)

    # Rotation link
    rotation_link = html.A(
        "Rotation ⬈",
        href="/rotation/",
        target="_blank",
        className="stat-chip",
        style={
            "textDecoration": "none",
            "color": "var(--text-muted)",
            "cursor": "pointer",
            "marginLeft": "8px",
        },
    )
    chips.append(rotation_link)

    # Seeding Progress
    if not d_done:
        chips.append(
            dbc.Badge(
                f"Seeding {d_done_n}/{d_total}",
                color="warning",
                className="stat-badge",
                style={"marginLeft": "8px"},
            )
        )

    chips += [
        html.Div(f"TPS {tps:.1f}", className="stat-chip"),
        html.Div(f"Ticks {tot:,}", className="stat-chip"),
        html.Div(f"Updated {updated_str}", className="stat-chip"),
    ]

    return html.Div(chips, className="top-stats-wrap")


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


# =============================================================================
# Dial helpers
# =============================================================================
def _state_class(label: str) -> str:
    L = (label or "").upper().strip()
    L = " ".join(L.split())
    if L == "STRONG SELL":
        return "state-ss"
    if L == "SELL":
        return "state-sell"
    if L == "NEUTRAL":
        return "state-neutral"
    if L == "BUY":
        return "state-buy"
    if L == "STRONG BUY":
        return "state-sb"
    if L == "BEARISH":
        return "state-sell"
    if L == "BULLISH":
        return "state-buy"
    return "state-neutral"


def _fmt_oi_compact(v: Optional[float]) -> str:
    if v is None:
        return "—"
    n = float(v)
    a = abs(n)
    if a >= 1e7:
        return f"{n/1e7:.2f}Cr"
    if a >= 1e5:
        return f"{n/1e5:.2f}L"
    if a >= 1e3:
        return f"{n/1e3:.2f}K"
    return str(int(round(n)))


@dash_app.callback(
    Output("sentiment-needle", "style"),
    Output("sentiment-sub", "children"),
    Output("pcr-needle", "style"),
    Output("pcr-sub", "children"),
    Input("refresh_sectors", "n_intervals"),
)
def update_dials(_):
    # Sentiment
    with LOCK:
        sm = compute_market_sentiment_proxy()

    score = float(sm["score"])
    sent_angle = max(-90.0, min(90.0, score * 90.0))
    sent_style = {"--rot": f"{sent_angle:.2f}deg"}

    sent_label = str(sm["label"])
    sent_sub = html.Span(
        [
            html.Span(sent_label, className=f"dial-state {_state_class(sent_label)}"),
            html.Span(f"{score:+.2f} • {sm['adv']} ↑ • {sm['dec']} ↓", className="dial-meta"),
        ],
        className="dial-sub-inner",
    )

    # PCR
    pn = compute_real_nifty_oi_pcr(strikes_around_atm=PCR_STRIKES_AROUND_ATM)

    if pn and pn.get("pcr") is not None:
        pcr = float(pn["pcr"])
        label = pcr_label_from_value(pcr)

        pcr_clamped = max(0.0, min(2.0, pcr))
        pcr_angle = (pcr_clamped - 1.0) * 90.0
        pcr_style = {"--rot": f"{pcr_angle:.2f}deg"}

        pe_oi = pn.get("pe_oi")
        ce_oi = pn.get("ce_oi")
        pe_txt = _fmt_oi_compact(pe_oi)
        ce_txt = _fmt_oi_compact(ce_oi)

        pcr_sub = html.Span(
            [
                html.Span(label, className=f"dial-state {_state_class(label)}"),
                html.Span(
                    f"NIFTY OI PCR {pcr:.2f} • PE {pe_txt} • CE {ce_txt}",
                    className="dial-meta",
                    title=(
                        f"PE OI {float(pe_oi):,.0f} / CE OI {float(ce_oi):,.0f}"
                        if (pe_oi is not None and ce_oi is not None)
                        else None
                    ),
                ),
            ],
            className="dial-sub-inner",
        )
    else:
        pcr_style = {"--rot": "0deg"}
        pcr_sub = html.Span(
            [
                html.Span("LOADING", className="dial-state state-neutral"),
                html.Span("NIFTY OI PCR", className="dial-meta"),
            ],
            className="dial-sub-inner",
        )

    return sent_style, sent_sub, pcr_style, pcr_sub


@dash_app.callback(
    Output("top15-gainers-grid", "rowData"),
    Output("top15-losers-grid", "rowData"),
    Input("refresh_sectors", "n_intervals"),
)
def update_rfactor_leaderboards(_):
    with LOCK:
        return top_gainers_losers_rfactor_rows(n=15)


@dash_app.callback(
    Output("hvhr-gainers-grid", "rowData"),
    Output("hvhr-losers-grid", "rowData"),
    Input("refresh_sectors", "n_intervals"),
)
def update_hvhr(_):
    with LOCK:
        return high_vol_high_rfactor_gainers_losers(n=HVHR_N, rfactor_quantile=HVHR_RFACTOR_Q)


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

app.mount("/rotation", rotation_app)
HERE = Path(__file__).resolve().parent
THEME_PATH = HERE / "assets" / "theme.css"


@app.on_event("startup")
def _startup():
    seed_daily_stats_once(per_req_sleep=SEED_SLEEP_SEC)
    start_ticker_once()
    load_nfo_instruments_once()


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
            "nfo_loaded": bool(NFO_INS_DF is not None),
            "nfo_error": NFO_LOAD_ERR,
        }


@app.get("/theme.css")
def theme_css():
    if THEME_PATH.exists():
        return FileResponse(THEME_PATH, media_type="text/css")
    return JSONResponse({"error": "theme.css not found"}, status_code=404)


@app.get("/")
def root():
    return RedirectResponse(url="/dash/", status_code=307)


app.mount("/dash", WSGIMiddleware(server))