# app.py  (NO AUTH • NO FIREBASE • NO SUBSCRIPTIONS)
#
# Run (local):
#   export KITE_API_KEY="..."
#   export KITE_ACCESS_TOKEN="..."
#   uvicorn app:app --reload --workers 1
#
# Render:
#   Start command: uvicorn app:app --host 0.0.0.0 --port $PORT --workers 1

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
from dash import dcc, html, Input, Output, State
import dash_bootstrap_components as dbc
import dash_ag_grid as dag

from kiteconnect import KiteConnect, KiteTicker

# OpenInterest FastAPI app (mounted)
import optioninterest as openinterest


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

# Hot Now window
HOT_WINDOW_SEC = 5 * 60
HOT_SAMPLE_SEC = 5
HOT_HISTORY_MAX_SEC = HOT_WINDOW_SEC + 10 * 60

# Hot Now filters
HOT_MIN_RET_PCT = float(os.getenv("HOT_MIN_RET_PCT", "0.25"))       # min |spike%| to include
HOT_MIN_RANGE_PCT = float(os.getenv("HOT_MIN_RANGE_PCT", "0.40"))   # min window range%

HVHR_N = int(os.getenv("HVHR_N", "20"))
HVHR_RFACTOR_Q = float(os.getenv("HVHR_RFACTOR_Q", "0.85"))

# PCR (NFO)
PCR_STRIKES_AROUND_ATM = int(os.getenv("PCR_STRIKES_AROUND_ATM", "12"))
PCR_CACHE_TTL_SEC = int(os.getenv("PCR_CACHE_TTL_SEC", "20"))
PCR_QUOTE_CHUNK = int(os.getenv("PCR_QUOTE_CHUNK", "180"))
NIFTY_SPOT_SYMBOL = os.getenv("NIFTY_SPOT_SYMBOL", "NSE:NIFTY 50")

# Background compute cadence (ultra-fast mode)
COMPUTE_CORE_EVERY_SEC = float(os.getenv("COMPUTE_CORE_EVERY_SEC", "2.0"))   # sector agg, leaderboards, sentiment
COMPUTE_HOT_EVERY_SEC = float(os.getenv("COMPUTE_HOT_EVERY_SEC", "5.0"))     # hot-now lists
COMPUTE_PCR_EVERY_SEC = float(os.getenv("COMPUTE_PCR_EVERY_SEC", "5.0"))     # will still respect internal PCR cache
COMPUTE_SLEEP_SEC = float(os.getenv("COMPUTE_SLEEP_SEC", "0.20"))            # loop sleep


# =============================================================================
# KITE INIT
# =============================================================================
kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)


# =============================================================================
# SECTORS / SYMBOLS
# =============================================================================
SECTOR_DEFINITIONS = {
    "METAL": [
        "ADANIENT", "APLAPOLLO", "BHARATFORG", "COALINDIA",
        "HINDALCO", "HINDZINC", "JSWSTEEL",
        "JINDALSTEL", "NMDC", "NATIONALUM",
        "SAIL", "TATASTEEL", "VEDL"
    ],
    "REALTY": [
        "PHOENIXLTD", "GODREJPROP", "LODHA",
        "OBEROIRLTY", "DLF", "PRESTIGE",
        "NBCC", "RVNL", "HUDCO"
    ],
    "ENERGY": [
        "RELIANCE", "ONGC", "IOC", "BPCL", "OIL",
        "NTPC", "POWERGRID", "POWERINDIA",
        "TATAPOWER", "TORNTPOWER", "JSWENERGY",
        "ADANIGREEN", "ADANIENSOL",
        "NHPC", "IREDA", "SUZLON", "INOXWIND",
        "WAAREEENER", "PREMIERENE",
        "PETRONET", "GAIL", "HINDPETRO"
    ],
    "AUTO": [
        "BOSCHLTD", "TIINDIA", "HEROMOTOCO",
        "M&M", "EICHERMOT", "EXIDEIND",
        "BAJAJ-AUTO", "ASHOKLEY",
        "MARUTI", "TVSMOTOR",
        "MOTHERSON", "SONACOMS",
        "UNOMINDA", "TMPV",
        "AMBER"
    ],
    "IT": [
        "INFY", "TCS", "HCLTECH", "WIPRO",
        "TECHM", "LTM", "MPHASIS",
        "KPITTECH", "COFORGE", "PERSISTENT",
        "TATAELXSI", "OFSS", "CAMS",
        "TATATECH", "NAUKRI", "KAYNES"
    ],
    "PHARMA": [
        "CIPLA", "ALKEM", "BIOCON", "DRREDDY",
        "MANKIND", "TORNTPHARM", "ZYDUSLIFE",
        "DIVISLAB", "LUPIN", "PPLPHARMA",
        "LAURUSLABS", "FORTIS",
        "AUROPHARMA", "GLENMARK",
        "SUNPHARMA", "SYNGENE",
        "MAXHEALTH", "APOLLOHOSP"
    ],
    "FMCG": [
        "HINDUNILVR", "ITC", "NESTLEIND",
        "BRITANNIA", "DABUR", "MARICO",
        "COLPAL", "GODREJCP",
        "TATACONSUM", "PATANJALI",
        "UNITDSPR",
        "VBL", "DMART", "NYKAA",
        "ETERNAL", "SWIGGY",
        "TITAN", "TRENT",
        "KALYANKJIL", "JUBLFOOD",
        "ASIANPAINT"
    ],
    "CEMENT": [
        "ULTRACEMCO", "SHREECEM",
        "AMBUJACEM", "DALBHARAT",
        "GRASIM", "ASTRAL",
        "PIDILITIND", "SUPREMEIND"
    ],
    "FINSERVICE": [
        "BAJFINANCE", "BAJAJFINSV", "BAJAJHLDNG",
        "ICICIPRULI", "ICICIGI", "SBILIFE",
        "HDFCLIFE", "LICI", "LICHSGFIN",
        "PNBHOUSING", "MUTHOOTFIN",
        "MANAPPURAM", "CHOLAFIN",
        "PFC", "RECLTD",
        "HDFCAMC", "360ONE",
        "KFINTECH", "NUVAMA",
        "PAYTM", "POLICYBZR",
        "IIFL", "SBICARD",
        "JIOFIN", "SHRIRAMFIN",
        "SAMMAANCAP", "ANGELONE",
        "BSE", "CDSL", "MCX", "IRFC"
    ],
    "BANK": [
        "HDFCBANK", "ICICIBANK", "AXISBANK",
        "KOTAKBANK", "IDFCFIRSTB",
        "FEDERALBNK", "INDUSINDBK",
        "AUBANK", "BANDHANBNK",
        "RBLBANK", "BANKINDIA", "PNB", "INDIANB",
        "SBIN", "UNIONBANK", "BANKBARODA", "CANBK"
    ],
    "TELECOM": [
        "BHARTIARTL", "INDUSTOWER",
        "HAVELLS", "KEI", "POLYCAB",
        "CROMPTON", "VOLTAS",
        "PGEL", "DIXON"
    ],
    "LOGISTICS": [
        "CONCOR", "DELHIVERY", "INDIGO",
        "INDHOTEL", "IRCTC",
        "BLUESTARCO", "GMRAIRPORT",
        "PAGEIND", "UPL"
    ],
    "DEFENCE": [
        "ABB", "BDL", "BEL", "BHEL",
        "CGPOWER", "CUMMINSIND",
        "HAL", "LT", "MAZDOCK",
        "SIEMENS", "SOLARINDS"
    ],
    "NIFTY_50": [
        "ADANIENT", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE",
        "BAJAJFINSV", "BEL", "BHARTIARTL", "BPCL", "CIPLA", "COALINDIA",
        "DRREDDY", "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
        "HINDALCO", "HINDUNILVR", "ICICIBANK", "INFY", "INDIGO", "ITC",
        "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI",
        "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE",
        "SBILIFE", "SHRIRAMFIN", "SBIN", "SUNPHARMA", "TCS", "TATACONSUM",
        "TATASTEEL", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
        "TMPV", "ETERNAL"
    ],
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
# LIVE / STATE (tick thread writes these)
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

EOD_SNAPSHOT: Dict[int, Dict[str, Any]] = {}
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

    # sample at HOT_SAMPLE_SEC cadence
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
            globals()["NFO_INS_DF"] = df
            log.info("Loaded NFO instruments (NIFTY only): %s rows", len(df))
        except Exception as e:
            globals()["NFO_LOAD_ERR"] = repr(e)
            log.exception("Failed to load NFO instruments")

    threading.Thread(target=_run, daemon=True).start()


def _chunk(lst: List[str], n: int):
    for i in range(0, len(lst), n):
        yield lst[i: i + n]


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
    if dfu is None or dfu.empty:
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
# SNAPSHOTS + ULTRA-FAST BACKGROUND COMPUTE CACHE
# =============================================================================
CACHE_LOCK = threading.Lock()
CACHE: Dict[str, Any] = {
    "sector_agg": {},
    "top15_gainers": [],
    "top15_losers": [],
    "hvhr_gainers": [],
    "hvhr_losers": [],
    "hot_gainers": [],
    "hot_losers": [],
    "sentiment": {"adv": 0, "dec": 0, "unch": 0, "total": 0, "score": 0.0, "label": "NEUTRAL"},
    "pcr": None,
    "updated": {
        "core": 0.0,
        "hot": 0.0,
        "pcr": 0.0,
    },
}


def _snapshot_state(include_hot: bool = False) -> Dict[str, Any]:
    """
    Copy required state under LOCK, then compute outside lock.
    """
    with LOCK:
        snap = {
            "price": dict(LAST_PRICE),
            "vol": dict(DAY_VOL),
            "ohlc": dict(LAST_OHLC),
            "eod": dict(EOD_SNAPSHOT),
            "daily": dict(DAILY_STATS),
            "tokens": list(TOKENS),
        }
        if include_hot:
            snap["hot"] = {tok: list(dq) for tok, dq in HOT_HISTORY.items()}
    return snap


def _get_live_or_eod_state_from_snap(token: int, snap: Dict[str, Any]) -> Optional[Tuple[float, float, dict]]:
    ltp = snap["price"].get(token)
    vol_today = snap["vol"].get(token)
    ohlc = snap["ohlc"].get(token) or {}

    if (
        ltp is not None
        and vol_today is not None
        and ohlc.get("open") is not None
        and ohlc.get("close") is not None
    ):
        return float(ltp), float(vol_today), ohlc

    e = (snap.get("eod") or {}).get(token)
    if not e or e.get("prev_close") is None:
        return None

    ohlc_eod = {"open": e["open"], "high": e["high"], "low": e["low"], "close": e["prev_close"]}
    return float(e["close"]), float(e["volume"]), ohlc_eod


def _time_factor_ist_for_rvol(now_ist: Optional[datetime] = None) -> float:
    now_ist = now_ist or datetime.now(IST)
    m_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    m_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)

    total_mins = 375.0
    if now_ist < m_open:
        mins_passed = 1.0
    elif now_ist > m_close:
        mins_passed = total_mins
    else:
        mins_passed = max(1.0, (now_ist - m_open).total_seconds() / 60.0)

    tf = mins_passed / total_mins
    return max(0.01, min(1.0, tf))


def _compute_rfactor_row_snap(token: int, snap: Dict[str, Any]) -> Optional[Dict[str, float]]:
    state_ = _get_live_or_eod_state_from_snap(token, snap)
    if not state_:
        return None

    ltp, vol_today, ohlc = state_
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

    st = (snap.get("daily") or {}).get(token) or {}
    avg_vol_20 = st.get("avg_vol_20")
    avg_range_20 = st.get("avg_range_20")
    avg_abs_oc_ret_20 = st.get("avg_abs_oc_ret_20")
    if not avg_vol_20 or not avg_range_20 or not avg_abs_oc_ret_20:
        return None

    eps = 1e-9
    rvol = float(vol_today) / (float(avg_vol_20) + eps)
    range_factor = max(0.0, float(range_today)) / (float(avg_range_20) + eps)
    move_factor = abs(float(pct_open)) / (float(avg_abs_oc_ret_20) + eps)

    rfactor_val = rvol * range_factor * move_factor
    dirr = (1.0 if pct_open >= 0 else -1.0) * rfactor_val

    return {
        "gap_pct": float(gap_pct),
        "pct_open": float(pct_open),
        "rfactor": float(rfactor_val),
        "dirr": float(dirr),
        "ltp": float(ltp),
        "day_open": float(day_open),
        "vol_today": float(vol_today),
    }


def _compute_market_sentiment_proxy_snap(snap: Dict[str, Any]) -> Dict[str, Any]:
    adv = dec = unch = 0
    for tok in snap.get("tokens") or []:
        st = _get_live_or_eod_state_from_snap(tok, snap)
        if not st:
            continue
        ltp, _, ohlc = st
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


def _compute_sector_aggregates_from_rr(rr_by_tok: Dict[int, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
    tf = _time_factor_ist_for_rvol(datetime.now(IST))
    out: Dict[str, Dict[str, float]] = {}

    # For RVOLm metrics we need avg_vol_20; we will grab it from DAILY_STATS global snapshot
    # but rr_by_tok doesn't include avg_vol_20. We can compute RVOLm from rr + daily stats snapshot
    snap_daily = None
    # NOTE: we intentionally read DAILY_STATS without LOCK here because this function is called
    # from compute thread after snapshot; so pass in daily map when calling.
    # (we will inject via closure in compute loop)
    raise RuntimeError("internal: call _compute_sector_aggregates_from_rr_with_daily() instead")


def _compute_sector_aggregates_from_rr_with_daily(
    rr_by_tok: Dict[int, Dict[str, float]],
    daily_map: Dict[int, Dict[str, Optional[float]]],
) -> Dict[str, Dict[str, float]]:
    """
    Sector bars metrics:
      DirR = signed mean(DirR)
      RVOLmNetSum = Σbuy RVOLm - Σsell RVOLm
      RVOLmNetMean = RVOLmNetSum / N
    """
    tf = _time_factor_ist_for_rvol(datetime.now(IST))
    out: Dict[str, Dict[str, float]] = {}

    for sector, syms in SECTOR_DEFINITIONS.items():
        dirr_vals: List[float] = []

        buy_sum = 0.0
        sell_sum = 0.0
        buy_n = 0
        sell_n = 0

        for s in syms:
            tok = symbol_to_token.get(s)
            if not tok:
                continue

            rr = rr_by_tok.get(tok)
            if not rr:
                continue

            dirr_vals.append(float(rr["dirr"]))

            st = daily_map.get(tok) or {}
            avg_vol_20 = st.get("avg_vol_20")
            vol_today = rr.get("vol_today")
            pct_open = rr.get("pct_open")

            try:
                if pct_open is None or avg_vol_20 is None or vol_today is None:
                    continue
                if float(avg_vol_20) <= 0:
                    continue

                expected = float(avg_vol_20) * float(tf)
                rvolm = float(vol_today) / (expected + 1e-9)

                if float(pct_open) >= 0:
                    buy_sum += rvolm
                    buy_n += 1
                else:
                    sell_sum += rvolm
                    sell_n += 1
            except Exception:
                continue

        n_total = buy_n + sell_n
        dirr_mean = (sum(dirr_vals) / len(dirr_vals)) if dirr_vals else 0.0

        net_sum = float(buy_sum - sell_sum)
        gross_sum = float(buy_sum + sell_sum)

        net_mean = float(net_sum / n_total) if n_total > 0 else 0.0
        gross_mean = float(gross_sum / n_total) if n_total > 0 else 0.0

        out[sector] = {
            "DirR": float(dirr_mean),
            "RVOLmBuySum": float(buy_sum),
            "RVOLmSellSum": float(sell_sum),
            "RVOLmNetSum": float(net_sum),
            "RVOLmGrossSum": float(gross_sum),
            "RVOLmNetMean": float(net_mean),
            "RVOLmGrossMean": float(gross_mean),
            "N": float(n_total),
            "BuyN": float(buy_n),
            "SellN": float(sell_n),
        }

    return out


def _quantile_threshold(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    q = min(max(float(q), 0.0), 1.0)
    vs = sorted(values)
    if len(vs) == 1:
        return float(vs[0])
    idx = int(round(q * (len(vs) - 1)))
    idx = min(max(idx, 0), len(vs) - 1)
    return float(vs[idx])


def _compute_hot_row_from_series(series: List[Tuple[float, float, Optional[float]]]) -> Optional[dict]:
    """
    series: list[(epoch, ltp, cumvol)]
    """
    if not series or len(series) < 2:
        return None

    now_epoch = float(series[-1][0])
    cutoff = now_epoch - float(HOT_WINDOW_SEC)

    base = None
    for t, p, v in series:
        if float(t) <= cutoff:
            base = (float(t), p, v)
        else:
            break
    if base is None:
        base = (float(series[0][0]), series[0][1], series[0][2])

    base_t, base_p, base_v = base
    _, last_p, last_v = series[-1]

    if base_p is None or float(base_p) <= 0 or last_p is None:
        return None

    prices = [float(p) for (t, p, _v) in series if float(t) >= base_t and p is not None]
    if len(prices) < 2:
        return None

    lo = float(min(prices))
    hi = float(max(prices))
    rng = float(hi - lo)

    base_pf = float(base_p)
    range_pct = (rng / (base_pf + 1e-9)) * 100.0

    up_spike_pct = (hi - base_pf) / (base_pf + 1e-9) * 100.0
    down_spike_pct = (lo - base_pf) / (base_pf + 1e-9) * 100.0
    spike_pct = up_spike_pct if abs(up_spike_pct) >= abs(down_spike_pct) else down_spike_pct

    vol_win = None
    if base_v is not None and last_v is not None:
        vol_win = float(last_v) - float(base_v)
        if vol_win < 0:
            vol_win = None

    return {"range_pct": float(range_pct), "spike_pct": float(spike_pct), "vol_win": vol_win}


_compute_started = False


def start_compute_loop_once():
    """
    Ultra-fast mode: precompute sector aggregates + leaderboards in background thread.
    Dash callbacks become cheap dictionary reads.
    """
    global _compute_started
    if _compute_started:
        return
    _compute_started = True

    def _run():
        last_core = 0.0
        last_hot = 0.0
        last_pcr = 0.0

        while True:
            now = time.time()

            # ---- CORE (rfactor rows -> leaderboards + sector aggregates + sentiment) ----
            if (now - last_core) >= COMPUTE_CORE_EVERY_SEC:
                try:
                    snap = _snapshot_state(include_hot=False)

                    rr_by_tok: Dict[int, Dict[str, float]] = {}
                    rows_basic: List[dict] = []
                    rfactor_vals: List[float] = []

                    for sym in ALL_SYMBOLS:
                        tok = symbol_to_token.get(sym)
                        if not tok:
                            continue
                        rr = _compute_rfactor_row_snap(tok, snap)
                        if not rr:
                            continue

                        rr_by_tok[tok] = rr
                        rows_basic.append({
                            "Symbol": sym,
                            "%Change": round(float(rr["pct_open"]), 2),
                            "RFactor": round(float(rr["rfactor"]), 2),
                            "Vol": int(rr["vol_today"]),
                        })
                        rfactor_vals.append(float(rr["rfactor"]))

                    # Top gainers/losers by RFactor
                    gainers = [r for r in rows_basic if float(r["%Change"]) > 0]
                    losers = [r for r in rows_basic if float(r["%Change"]) < 0]
                    gainers.sort(key=lambda r: float(r["RFactor"]), reverse=True)
                    losers.sort(key=lambda r: float(r["RFactor"]), reverse=True)
                    top15_gainers = gainers[:15]
                    top15_losers = losers[:15]

                    # HVHR (top quantile by RFactor, then sort by Vol)
                    thr = _quantile_threshold(rfactor_vals, float(HVHR_RFACTOR_Q)) if rfactor_vals else None
                    if thr is None:
                        hvhr_gainers, hvhr_losers = [], []
                    else:
                        bucket = [r for r in rows_basic if float(r["RFactor"]) >= float(thr)]
                        bucket_g = [r for r in bucket if float(r["%Change"]) > 0]
                        bucket_l = [r for r in bucket if float(r["%Change"]) < 0]
                        bucket_g.sort(key=lambda r: (int(r["Vol"]), float(r["RFactor"])), reverse=True)
                        bucket_l.sort(key=lambda r: (int(r["Vol"]), float(r["RFactor"])), reverse=True)
                        hvhr_gainers = bucket_g[: int(HVHR_N)]
                        hvhr_losers = bucket_l[: int(HVHR_N)]

                    # Sector aggregates
                    sector_agg = _compute_sector_aggregates_from_rr_with_daily(
                        rr_by_tok=rr_by_tok,
                        daily_map=(snap.get("daily") or {}),
                    )

                    # Sentiment
                    sentiment = _compute_market_sentiment_proxy_snap(snap)

                    with CACHE_LOCK:
                        CACHE["sector_agg"] = sector_agg
                        CACHE["top15_gainers"] = top15_gainers
                        CACHE["top15_losers"] = top15_losers
                        CACHE["hvhr_gainers"] = hvhr_gainers
                        CACHE["hvhr_losers"] = hvhr_losers
                        CACHE["sentiment"] = sentiment
                        CACHE["updated"]["core"] = now

                except Exception:
                    log.exception("compute loop: CORE crashed")

                last_core = now

            # ---- HOT NOW ----
            if (now - last_hot) >= COMPUTE_HOT_EVERY_SEC:
                try:
                    snap = _snapshot_state(include_hot=True)
                    hot = snap.get("hot") or {}

                    rows = []
                    min_spike = float(HOT_MIN_RET_PCT)
                    min_rng = float(HOT_MIN_RANGE_PCT)

                    for sym in ALL_SYMBOLS:
                        tok = symbol_to_token.get(sym)
                        if not tok:
                            continue
                        series = hot.get(tok)
                        if not series:
                            continue

                        hr = _compute_hot_row_from_series(series)
                        if not hr:
                            continue

                        spike = float(hr["spike_pct"])
                        range_pct = float(hr["range_pct"])

                        if abs(spike) < min_spike:
                            continue
                        if range_pct < min_rng:
                            continue

                        rows.append({
                            "Symbol": sym,
                            "_spike": spike,
                            "_abs_spike": abs(spike),
                            "SPIKE%": round(spike, 2),
                            "RNG5%": round(range_pct, 2),
                            "DAY RNG%": None,
                        })

                    gain = [r for r in rows if float(r["_spike"]) > 0]
                    loss = [r for r in rows if float(r["_spike"]) < 0]
                    gain.sort(key=lambda r: (float(r["_abs_spike"]), float(r["RNG5%"])), reverse=True)
                    loss.sort(key=lambda r: (float(r["_abs_spike"]), float(r["RNG5%"])), reverse=True)

                    hot_gainers = [{k: v for k, v in r.items() if not k.startswith("_")} for r in gain[:15]]
                    hot_losers = [{k: v for k, v in r.items() if not k.startswith("_")} for r in loss[:15]]

                    with CACHE_LOCK:
                        CACHE["hot_gainers"] = hot_gainers
                        CACHE["hot_losers"] = hot_losers
                        CACHE["updated"]["hot"] = now

                except Exception:
                    log.exception("compute loop: HOT crashed")

                last_hot = now

            # ---- PCR ----
            if (now - last_pcr) >= COMPUTE_PCR_EVERY_SEC:
                try:
                    p = compute_real_nifty_oi_pcr(strikes_around_atm=PCR_STRIKES_AROUND_ATM)
                    with CACHE_LOCK:
                        CACHE["pcr"] = p
                        CACHE["updated"]["pcr"] = now
                except Exception:
                    log.exception("compute loop: PCR crashed")

                last_pcr = now

            time.sleep(COMPUTE_SLEEP_SEC)

    threading.Thread(target=_run, daemon=True).start()


# =============================================================================
# TICKER
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


# =============================================================================
# UI: Shared components
# =============================================================================
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


def _extract_sector_from_path(pn: str) -> Optional[str]:
    pn = (pn or "").strip()
    if "/sector/" not in pn:
        return None
    sector = unquote(pn.split("/sector/", 1)[1]).strip("/").upper()
    return sector or None


def _sector_modal_coldefs():
    return [
        {
            "colId": "stock",
            "field": "Symbol",
            "headerName": "STOCK",
            "cellRenderer": "SymbolCell",
            "minWidth": 130,
            "maxWidth": 170,
            "suppressSizeToFit": True,
            "headerClass": "h-left",
            "cellClass": "c-left",
        },
        {
            "colId": "company",
            "field": "Company",
            "headerName": "COMPANY",
            "cellRenderer": "CompanyLinkCell",
            "minWidth": 200,
            "flex": 1,
            "headerClass": "h-left",
            "cellClass": "c-left",
        },
        {
            "colId": "dirr",
            "field": "DirR",
            "headerName": "DIR R",
            "type": "rightAligned",
            "cellRenderer": "Num2Cell",
            "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"},
            "minWidth": 110,
            "maxWidth": 130,
            "suppressSizeToFit": True,
            "headerClass": "ag-right-aligned-header h-right",
            "cellClass": "ag-right-aligned-cell cell-num c-right",
        },
        {
            "colId": "price",
            "field": "Price",
            "headerName": "PRICE",
            "type": "rightAligned",
            "cellRenderer": "Num2Cell",
            "minWidth": 110,
            "maxWidth": 130,
            "suppressSizeToFit": True,
            "headerClass": "ag-right-aligned-header h-right",
            "cellClass": "ag-right-aligned-cell cell-num c-right",
        },
        {
            "colId": "pct",
            "field": "%Change",
            "headerName": "%CHG",
            "type": "rightAligned",
            "cellRenderer": "Pct2Cell",
            "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"},
            "minWidth": 105,
            "maxWidth": 125,
            "suppressSizeToFit": True,
            "headerClass": "ag-right-aligned-header h-right",
            "cellClass": "ag-right-aligned-cell cell-num c-right",
        },
        {
            "colId": "gap",
            "field": "Gap%",
            "headerName": "GAP %",
            "type": "rightAligned",
            "cellRenderer": "Pct2Cell",
            "minWidth": 105,
            "maxWidth": 125,
            "suppressSizeToFit": True,
            "headerClass": "ag-right-aligned-header h-right",
            "cellClass": "ag-right-aligned-cell cell-num c-right",
        },
        {
            "colId": "rvolm",
            "field": "RVOLm",
            "headerName": "RVOLm",
            "type": "rightAligned",
            "cellRenderer": "Num2Cell",
            "minWidth": 110,
            "maxWidth": 130,
            "suppressSizeToFit": True,
            "headerClass": "ag-right-aligned-header h-right",
            "cellClass": "ag-right-aligned-cell cell-num c-right",
        },
        {
            "colId": "rfactor",
            "field": "RFactor",
            "headerName": "RFACTOR",
            "type": "rightAligned",
            "cellRenderer": "Num2Cell",
            "minWidth": 120,
            "maxWidth": 140,
            "suppressSizeToFit": True,
            "headerClass": "ag-right-aligned-header h-right",
            "cellClass": "ag-right-aligned-cell cell-num c-right",
        },
    ]


def sector_modal_component():
    grid_opts = {
        "getRowId": {"function": "params.data.Symbol"},
        "alwaysShowVerticalScroll": True,
        "animateRows": True,
        # show ONLY funnel icon, hide hamburger menu
        "suppressHeaderMenuButton": True,
        "suppressHeaderFilterButton": False,
        "onGridReady": {"function": "params.api.sizeColumnsToFit();"},
        "onGridSizeChanged": {"function": "params.api.sizeColumnsToFit();"},
    }

    header = html.Div(
        [
            html.Div(id="sector-modal-title", children="SECTOR", className="tt-modal-title"),
            dcc.Link(
                dbc.Button(
                    "Close",
                    color="secondary",
                    outline=True,
                    className="tt-modal-close-btn",
                ),
                href=BASE,
                refresh=False,
                className="tt-modal-close-link",
            ),
        ],
        className="tt-modal-header tt-modal-header--flex",
    )

    return dbc.Modal(
        [
            dbc.ModalHeader(header, close_button=False, className="tt-modal-header-wrap"),
            dbc.ModalBody(
                html.Div(
                    dag.AgGrid(
                        id="sector-modal-grid",
                        className="ag-theme-alpine-dark grid-wrap compact-grid tt-modal-grid",
                        columnDefs=_sector_modal_coldefs(),
                        rowData=[],
                        defaultColDef={"sortable": True, "filter": True, "resizable": True},
                        dashGridOptions=grid_opts,
                        style={"height": "67vh", "width": "100%"},
                    ),
                    className="tt-modal-gridwrap",
                ),
                className="tt-modal-body",
            ),
        ],
        id="sector-modal",
        is_open=False,
        size="xl",
        centered=True,
        scrollable=True,
        backdrop=True,
        keyboard=True,

        # ✅ dbc.Modal uses className (NOT modalClassName)
        className="tt-modal",
        contentClassName="tt-modal-content",
        backdropClassName="tt-modal-backdrop",
    )

# =============================================================================
# PAGES
# =============================================================================
def sectors_page():
    four_cols = [
        {"colId": "stock", "field": "Symbol", "headerName": "STOCK", "cellRenderer": "SymbolCell",
         "minWidth": 10, "flex": 1, "headerClass": "h-left", "cellClass": "c-left"},
        {"colId": "pctChg", "field": "%Change", "headerName": "%CHG", "cellRenderer": "PctPill",
         "minWidth": 150, "maxWidth": 150, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right", "cellClass": "ag-right-aligned-cell cell-num c-right"},
        {"colId": "rfactor", "field": "RFactor", "headerName": "RFACTOR", "cellRenderer": "RfactorPill",
         "minWidth": 125, "maxWidth": 170, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right", "cellClass": "ag-right-aligned-cell cell-num c-right"},
        {"colId": "volume", "field": "Vol", "headerName": "VOLUME", "cellRenderer": "VolPill",
         "minWidth": 140, "maxWidth": 190, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right", "cellClass": "ag-right-aligned-cell cell-num c-right"},
    ]

    hot_cols = [
        four_cols[0],
        {"colId": "spike", "field": "SPIKE%", "headerName": "SPIKE%", "cellRenderer": "PctPill",
         "minWidth": 140, "maxWidth": 160, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right", "cellClass": "ag-right-aligned-cell cell-num c-right"},
        {"colId": "rng5", "field": "RNG5%", "headerName": "RNG5%", "type": "rightAligned",
         "valueFormatter": {"function": "fmtPct(params.value)"},
         "minWidth": 120, "maxWidth": 140, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right", "cellClass": "ag-right-aligned-cell cell-num c-right"},
        {"colId": "dayrng", "field": "DAY RNG%", "headerName": "DAY RNG%", "type": "rightAligned",
         "valueFormatter": {"function": "fmtPct(params.value)"},
         "minWidth": 130, "maxWidth": 160, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right", "cellClass": "ag-right-aligned-cell cell-num c-right"},
    ]

    grid_opts = {
        "getRowId": {"function": "params.data.Symbol"},
        "alwaysShowVerticalScroll": False,
        "animateRows": False,
        "suppressMenuHide": False,
        "onGridReady": {"function": "params.api.sizeColumnsToFit();"},
        "onGridSizeChanged": {"function": "params.api.sizeColumnsToFit();"},
    }

    top_bucket_pct = int((1.0 - float(HVHR_RFACTOR_Q)) * 100)

    return html.Div(
        [
            dcc.Interval(id="refresh_sectors", interval=2000, n_intervals=0),

            dbc.Row(
                [
                    dbc.Col(html.H4("Sectors", className="page-title sectors-title mb-0"), width="auto"),
                    dbc.Col(
                        dbc.RadioItems(
                            id="sectors-sort",
                            options=[
                                {"label": "Sort: RVOLm", "value": "RVOLm"},
                                {"label": "Sort: RVOLm Mean", "value": "RVOLmMean"},
                                {"label": "Sort: DirR (mean)", "value": "DirR"},
                            ],
                            value="RVOLmMean",
                            inline=True,
                            className="sectors-sort ms-2 mb-0",
                        ),
                        width=True,
                    ),
                ],
                className="sectors-header align-items-center g-2 mb-2",
            ),

            html.Div(id="sector-bars", className="sector-bars-wrap"),
            html.Div(
                "Click a sector bar to open popup. "
                "RVOLm = net paced rel vol (buy−sell). DirR = signed mean directional rfactor.",
                className="hint",
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
                            html.H6("Hot Now (last 5m) — Gainers", className="mt-1"),
                            dag.AgGrid(
                                id="hot15-gainers-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=hot_cols,
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
                            html.H6("Hot Now (last 5m) — Losers", className="mt-1"),
                            dag.AgGrid(
                                id="hot15-losers-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=hot_cols,
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

            dbc.Row(
                [
                    dbc.Col(dial_component("sentiment", "BIAS"), md=6),
                    dbc.Col(dial_component("pcr", "PCR"), md=6),
                ],
                className="g-2 dials-row",
            ),
        ],
        className="page-wrap",
    )


# --- Minimal VOLM page: Top15 BUY/SELL by RVOLm (computed on-demand; smaller page) ---
def top15_buy_sell_rvolm_rows(n: int = 15):
    snap = _snapshot_state(include_hot=False)
    tf = _time_factor_ist_for_rvol(datetime.now(IST))
    buy = []
    sell = []

    for sym in ALL_SYMBOLS:
        tok = symbol_to_token.get(sym)
        if not tok:
            continue

        st = _get_live_or_eod_state_from_snap(tok, snap)
        if not st:
            continue

        ltp, vol_today, ohlc = st
        op = (ohlc or {}).get("open")
        if op is None:
            continue

        try:
            ltp = float(ltp)
            vol_today = float(vol_today)
            op = float(op)
        except Exception:
            continue

        if op <= 0:
            continue

        st20 = (snap.get("daily") or {}).get(tok) or {}
        avg_vol_20 = st20.get("avg_vol_20")
        try:
            avg_vol_20 = float(avg_vol_20) if avg_vol_20 is not None else None
        except Exception:
            avg_vol_20 = None

        if not avg_vol_20 or avg_vol_20 <= 0:
            continue

        pct_open = (ltp - op) / op * 100.0
        expected = avg_vol_20 * tf
        rvolm = vol_today / (expected + 1e-9)

        row = {
            "Symbol": sym,
            "%Change": round(float(pct_open), 2),
            "RVOLm": round(float(rvolm), 2),
            "Vol": int(vol_today),
        }

        if pct_open >= 0:
            buy.append(row)
        else:
            sell.append(row)

    buy.sort(key=lambda x: float(x.get("RVOLm") or 0.0), reverse=True)
    sell.sort(key=lambda x: float(x.get("RVOLm") or 0.0), reverse=True)
    return buy[:n], sell[:n]


def volm_page():
    cols = [
        {"colId": "stock", "field": "Symbol", "headerName": "STOCK", "cellRenderer": "SymbolCell",
         "minWidth": 140, "maxWidth": 170, "suppressSizeToFit": True,
         "headerClass": "h-left", "cellClass": "c-left"},
        {"colId": "pct", "field": "%Change", "headerName": "%CHG", "cellRenderer": "PctPill",
         "minWidth": 140, "maxWidth": 150, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right",
         "cellClass": "ag-right-aligned-cell cell-num c-right"},
        {"colId": "rvolm", "field": "RVOLm", "headerName": "RVOLm", "cellRenderer": "Num2Cell",
         "minWidth": 120, "maxWidth": 140, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right",
         "cellClass": "ag-right-aligned-cell cell-num c-right"},
        {"colId": "vol", "field": "Vol", "headerName": "VOLUME", "cellRenderer": "VolPill",
         "minWidth": 150, "maxWidth": 190, "suppressSizeToFit": True,
         "headerClass": "ag-right-aligned-header h-right",
         "cellClass": "ag-right-aligned-cell cell-num c-right"},
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
            dcc.Interval(id="refresh_volm", interval=2000, n_intervals=0),

            dbc.Row(
                [
                    dbc.Col(
                        dcc.Link("← Back", href=BASE, className="stat-chip", style={"textDecoration": "none"}),
                        width="auto",
                    ),
                    dbc.Col(html.H4("Volm (RVOLm)", className="page-title mb-0"), width=True),
                ],
                className="align-items-center g-2 mb-2",
            ),

            dbc.Row(
                [
                    dbc.Col(
                        [
                            html.H6("Top 15 BUY (by RVOLm)", className="mt-1"),
                            dag.AgGrid(
                                id="volm-buy-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=cols,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(520px, 56vh)", "width": "100%"},
                            ),
                        ],
                        md=6,
                    ),
                    dbc.Col(
                        [
                            html.H6("Top 15 SELL (by RVOLm)", className="mt-1"),
                            dag.AgGrid(
                                id="volm-sell-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=cols,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(520px, 56vh)", "width": "100%"},
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


# =============================================================================
# DASH ROOT LAYOUT (modal lives here)
# =============================================================================
dash_app.layout = dbc.Container(
    fluid=True,
    children=[
        dcc.Location(id="url"),

        # IMPORTANT: prevents full sectors re-render when URL changes only for modal
        dcc.Store(id="page-store"),

        dcc.Interval(id="top_refresh", interval=1000, n_intervals=0),

        html.Div(
            dbc.Row(
                [
                    dbc.Col(
                        html.Div([html.Img(src=dash.get_asset_url("turbotrades.svg"), className="tt-logo")],
                                 className="tt-brand"),
                        width=True,
                    ),
                    dbc.Col(html.Div(id="top-stats"), width="auto"),
                ],
                className="align-items-center g-2",
            ),
            className="topbar-wrap",
        ),

        html.Div(id="app-body"),

        sector_modal_component(),
    ],
)


# =============================================================================
# ROUTER (no rebuild between /dash/ and /dash/sector/<x>)
# =============================================================================
def _classify_page(pathname: str) -> str:
    pn = (pathname or "").strip() or "/"

    # Support both "with prefix" and "internal" paths
    volm_paths = {"/volm", "/volm/", f"{BASE}volm", f"{BASE}volm/"}
    oi_paths = {"/openinterest", "/openinterest/", f"{BASE}openinterest", f"{BASE}openinterest/"}

    if pn in volm_paths:
        return "volm"
    if pn in oi_paths:
        return "openinterest"
    return "sectors"


@dash_app.callback(
    Output("app-body", "children"),
    Output("page-store", "data"),
    Input("url", "pathname"),
    State("page-store", "data"),
)
def route(pathname, current_page):
    page = _classify_page(pathname)
    if current_page == page:
        return dash.no_update, current_page

    if page == "volm":
        return volm_page(), "volm"

    if page == "openinterest":
        return html.Iframe(
            src="/openinterest",
            style={
                "width": "100%",
                "height": "calc(100vh - 140px)",
                "border": "0",
                "borderRadius": "16px",
            },
        ), "openinterest"

    return sectors_page(), "sectors"


# =============================================================================
# TOP CHIPS
# =============================================================================
def _oi_inference_chip():
    try:
        with openinterest.state_lock:
            s = dict(openinterest.state)
    except Exception:
        s = {}

    baseline_ok = (s.get("baseline_price") is not None) and (s.get("baseline_oi") is not None)

    bt_raw = (s.get("buildup_type") or "NO_CLEAR")
    bt = bt_raw.replace("_", " ")
    bias = (s.get("bias") or "NEUTRAL").upper()
    label = s.get("label") or ""

    if not baseline_ok:
        return html.Div("OI: WAITING BASELINE", className="stat-chip", title=label)

    text = f"OI: {bt} • {bias}"

    if bias == "BULLISH":
        style = {"color": "var(--good)", "borderColor": "rgba(46, 213, 115, 0.55)"}
    elif bias == "BEARISH":
        style = {"color": "var(--bad)", "borderColor": "rgba(255, 71, 87, 0.55)"}
    else:
        style = {}

    return html.Div(text, className="stat-chip", style=style, title=label)


@dash_app.callback(Output("top-stats", "children"), Input("top_refresh", "n_intervals"))
def update_top_stats(_):
    updated_str = datetime.now(IST).strftime("%H:%M:%S")

    with LOCK:
        offline = (time.time() - LAST_TICK_TS) > 10 if LAST_TICK_TS else True
        tot = TOTAL_TICKS
        d_done = DAILY_SEED_DONE
        d_done_n = int(DAILY_SEED_PROGRESS.get("done", 0) or 0)
        d_total = int(DAILY_SEED_PROGRESS.get("total", 0) or 0)
        d_err = int(DAILY_SEED_ERRORS or 0)

    with CACHE_LOCK:
        sm = dict(CACHE.get("sentiment") or {})
        pn = CACHE.get("pcr")

    # ---- BIAS chip ----
    sent_label = str(sm.get("label") or "NEUTRAL").upper()
    sent_score = float(sm.get("score") or 0.0)
    adv = int(sm.get("adv", 0) or 0)
    dec = int(sm.get("dec", 0) or 0)
    unch = int(sm.get("unch", 0) or 0)

    if sent_label == "BULLISH":
        sent_style = {"color": "var(--good)", "borderColor": "rgba(46, 213, 115, 0.55)"}
    elif sent_label == "BEARISH":
        sent_style = {"color": "var(--bad)", "borderColor": "rgba(255, 71, 87, 0.55)"}
    else:
        sent_style = {}

    sentiment_chip = html.Div(
        f"BIAS: {sent_label} ({sent_score:+.2f}) • {adv} ↑ • {dec} ↓",
        className="stat-chip",
        style=sent_style,
        title=f"Adv {adv} • Dec {dec} • Unch {unch}",
    )

    # ---- PCR chip ----
    if pn and pn.get("pcr") is not None:
        pcr = float(pn["pcr"])
        pcr_lbl = pcr_label_from_value(pcr)

        if pcr_lbl in ("BUY", "STRONG BUY"):
            pcr_style = {"color": "var(--good)", "borderColor": "rgba(46, 213, 115, 0.55)"}
        elif pcr_lbl in ("SELL", "STRONG SELL"):
            pcr_style = {"color": "var(--bad)", "borderColor": "rgba(255, 71, 87, 0.55)"}
        else:
            pcr_style = {}

        pcr_chip = html.Div(
            f"PCR: {pcr:.2f} ({pcr_lbl})",
            className="stat-chip",
            style=pcr_style,
            title=f"Expiry {pn.get('expiry')} • ATM {pn.get('atm')} • Time {pn.get('updated_at')}",
        )
    else:
        pcr_chip = html.Div("PCR: LOADING", className="stat-chip")

    chips = [
        dbc.Badge("Offline" if offline else "Live", color=("danger" if offline else "success"), className="stat-badge"),
        html.A(
            "Volm",
            href=f"{BASE}volm",
            target="_blank",
            className="stat-chip",
            style={"textDecoration": "none", "marginLeft": "8px", "cursor": "pointer"},
        ),
        _oi_inference_chip(),
        sentiment_chip,
        pcr_chip,
    ]

    if not d_done:
        chips.append(
            dbc.Badge(
                f"Seeding {d_done_n}/{d_total} (err {d_err})",
                color="warning",
                className="stat-badge",
                style={"marginLeft": "8px"},
            )
        )

    chips += [
        html.Div(f"Ticks {tot:,}", className="stat-chip"),
        html.Div(f"Time {updated_str}", className="stat-chip"),
    ]

    return html.Div(chips, className="top-stats-wrap")


# =============================================================================
# SECTOR BARS (render only; data from CACHE)
# =============================================================================
@dash_app.callback(
    Output("sector-bars", "children"),
    Input("refresh_sectors", "n_intervals"),
    Input("sectors-sort", "value"),
)
def render_sector_bars(_n, sort_by):
    sort_by = (sort_by or "RVOLmMean").strip()

    try:
        if sort_by == "DirR":
            metric = "DirR"
        elif sort_by == "RVOLmMean":
            metric = "RVOLmNetMean"
        else:
            metric = "RVOLmNetSum"

        with CACHE_LOCK:
            agg = dict(CACHE.get("sector_agg") or {})

        items = sorted(
            agg.items(),
            key=lambda kv: float(kv[1].get(metric, 0.0) or 0.0),
            reverse=True,
        )
        if not items:
            return html.Div("Loading sector bars…", className="hint")

        vals = [float(m.get(metric, 0.0) or 0.0) for _, m in items]
        raw_min = min(vals)
        raw_max = max(vals)

        span = raw_max - raw_min
        pad = (0.08 * span) if span > 1e-9 else 0.25

        vmin = raw_min - pad
        vmax = raw_max + pad
        vmin = min(vmin, 0.0)
        vmax = max(vmax, 0.0)

        if (vmax - vmin) <= 1e-9:
            vmin, vmax = -1.0, 1.0

        tick_min = float(vmin)
        tick_max = float(vmax)
        axis_span = float(tick_max - tick_min) or 1.0

        zero_pct = ((tick_max - 0.0) / axis_span) * 100.0
        zero_pct = max(0.0, min(100.0, zero_pct))

        plot_h = int(os.getenv("SECTOR_PLOT_H_PX", "350"))
        pos_px = plot_h * (zero_pct / 100.0)
        neg_px = plot_h - pos_px

        pos_dom = max(0.0, tick_max)
        neg_dom = max(0.0, -tick_min)
        eps = 1e-12

        def fmt(x: float) -> str:
            x = float(x)
            if abs(x) < 5e-7:
                x = 0.0
            return f"{x:.2f}"

        ticks = [tick_max, tick_max / 2.0, 0.0, tick_min / 2.0, tick_min]
        axis_ticks = []
        for tv in ticks:
            top_pct = ((tick_max - float(tv)) / axis_span) * 100.0
            axis_ticks.append(html.Div(fmt(tv), className="sector-axis-tick", style={"top": f"{top_pct:.2f}%"}))

        axis = html.Div(axis_ticks, className="sector-hist-axis", style={"height": f"{plot_h}px"})

        children = [axis, html.Div(className="sector-hist-zero-line")]
        bar_min_px = 4.0

        for sector, m in items:
            val = float(m.get(metric, 0.0) or 0.0)
            disp = sector.replace("_", " ").upper()
            val_str = f"{val:+.2f}"

            if val >= 0:
                bar_px = (val / (pos_dom + eps)) * pos_px if pos_dom > 0 and pos_px > 0 else 0.0
                bar_px = min(max(bar_px, 0.0), pos_px)
            else:
                bar_px = ((-val) / (neg_dom + eps)) * neg_px if neg_dom > 0 and neg_px > 0 else 0.0
                bar_px = min(max(bar_px, 0.0), neg_px)

            if 0 < bar_px < bar_min_px:
                bar_px = bar_min_px

            children.append(
                dcc.Link(
                    href=f"{BASE}sector/{sector}",
                    className="sector-hist-link",
                    refresh=False,
                    children=html.Div(
                        [
                            html.Div(
                                [html.Div(disp, className="sector-hist-tip-name"),
                                 html.Div(val_str, className="sector-hist-tip-val")],
                                className="sector-hist-tooltip",
                            ),
                            html.Div(
                                [html.Div(
                                    className=("sector-hist-bar pos" if val >= 0 else "sector-hist-bar neg"),
                                    style={"height": f"{bar_px:.0f}px"},
                                )],
                                className="sector-hist-track",
                                style={"height": f"{plot_h}px"},
                            ),
                            html.Div(disp, className="sector-hist-name"),
                        ],
                        className="sector-hist-col",
                        title=f"{metric} {val_str}",
                    ),
                )
            )

        return html.Div(
            children,
            className="sector-hist-plot",
            style={"--zero": f"{zero_pct:.2f}%", "--axisW": "68px"},
        )

    except Exception:
        log.exception("render_sector_bars crashed")
        return html.Div("Sector bars error (see logs).", className="hint")


# =============================================================================
# SECTOR MODAL (on-demand; small list -> ok)
# =============================================================================
def sector_rows_sorted(sector: str, sort_by: str = "RFactor"):
    rows = []
    tf = _time_factor_ist_for_rvol(datetime.now(IST))
    snap = _snapshot_state(include_hot=False)

    for s in SECTOR_DEFINITIONS.get(sector, []):
        tok = symbol_to_token.get(s)
        if not tok:
            continue

        rr = _compute_rfactor_row_snap(tok, snap)
        if not rr:
            continue

        pct_open = float(rr["pct_open"])
        gap_pct = float(rr["gap_pct"])
        ltp = float(rr["ltp"])

        st = (snap.get("daily") or {}).get(tok) or {}
        avg_vol_20 = st.get("avg_vol_20")
        vol_today = rr.get("vol_today")

        rvolm = None
        try:
            if avg_vol_20 and vol_today is not None and float(avg_vol_20) > 0:
                expected = float(avg_vol_20) * float(tf)
                rvolm = float(vol_today) / (expected + 1e-9)
        except Exception:
            rvolm = None

        rows.append({
            "Symbol": s,
            "Company": symbol_to_name.get(s, ""),
            "DirR": float(rr["dirr"]),
            "Price": ltp,
            "%Change": pct_open,
            "Gap%": gap_pct,
            "RVOLm": rvolm,
            "RFactor": float(rr["rfactor"]),
        })

    if not rows:
        return []

    sb = (sort_by or "").strip().upper()
    if sb in ("RVOL", "RVOLM"):
        key = "RVOLm"
    elif sb in ("DIRR", "DIR R"):
        key = "DirR"
    elif sb in ("%CHANGE", "%CHG", "CHG"):
        key = "%Change"
    else:
        key = "RFactor"

    def sort_val(x):
        v = x.get(key)
        return float(v) if v is not None else float("-inf")

    rows.sort(key=sort_val, reverse=True)
    return rows


@dash_app.callback(
    Output("sector-modal", "is_open"),
    Output("sector-modal-title", "children"),
    Output("sector-modal-grid", "rowData"),
    Input("url", "pathname"),
    Input("top_refresh", "n_intervals"),
)
def sync_sector_modal(pathname, _tick):
    sector = _extract_sector_from_path(pathname)
    if sector and sector in SECTOR_DEFINITIONS:
        rows = sector_rows_sorted(sector, sort_by="RFactor")
        title = sector.replace("_", " ").title()
        return True, title, rows
    return False, "Sector", []


# =============================================================================
# DIALS + LEADERBOARDS (read from CACHE only)
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
    with CACHE_LOCK:
        sm = dict(CACHE.get("sentiment") or {})
        pn = CACHE.get("pcr")

    score = float(sm.get("score") or 0.0)
    sent_angle = max(-90.0, min(90.0, score * 90.0))
    sent_style = {"--rot": f"{sent_angle:.2f}deg"}

    sent_label = str(sm.get("label") or "NEUTRAL")
    sent_sub = html.Span(
        [
            html.Span(sent_label, className=f"dial-state {_state_class(sent_label)}"),
            html.Span(f"{score:+.2f} • {sm.get('adv',0)} ↑ • {sm.get('dec',0)} ↓", className="dial-meta"),
        ],
        className="dial-sub-inner",
    )

    if pn and pn.get("pcr") is not None:
        pcr = float(pn["pcr"])
        label = pcr_label_from_value(pcr)

        pcr_clamped = max(0.0, min(2.0, pcr))
        pcr_angle = (pcr_clamped - 1.0) * 90.0
        pcr_style = {"--rot": f"{pcr_angle:.2f}deg"}

        pe_txt = _fmt_oi_compact(pn.get("pe_oi"))
        ce_txt = _fmt_oi_compact(pn.get("ce_oi"))

        pcr_sub = html.Span(
            [
                html.Span(label, className=f"dial-state {_state_class(label)}"),
                html.Span(f"PCR {pcr:.2f} • PE {pe_txt} • CE {ce_txt}", className="dial-meta"),
            ],
            className="dial-sub-inner",
        )
    else:
        pcr_style = {"--rot": "0deg"}
        pcr_sub = html.Span(
            [
                html.Span("LOADING", className="dial-state state-neutral"),
                html.Span("PCR", className="dial-meta"),
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
    with CACHE_LOCK:
        return list(CACHE.get("top15_gainers") or []), list(CACHE.get("top15_losers") or [])


@dash_app.callback(
    Output("hvhr-gainers-grid", "rowData"),
    Output("hvhr-losers-grid", "rowData"),
    Input("refresh_sectors", "n_intervals"),
)
def update_hvhr(_):
    with CACHE_LOCK:
        return list(CACHE.get("hvhr_gainers") or []), list(CACHE.get("hvhr_losers") or [])


@dash_app.callback(
    Output("hot15-gainers-grid", "rowData"),
    Output("hot15-losers-grid", "rowData"),
    Input("refresh_sectors", "n_intervals"),
)
def update_hot_now(_):
    with CACHE_LOCK:
        return list(CACHE.get("hot_gainers") or []), list(CACHE.get("hot_losers") or [])


@dash_app.callback(
    Output("volm-buy-grid", "rowData"),
    Output("volm-sell-grid", "rowData"),
    Input("refresh_volm", "n_intervals"),
)
def update_volm_grids(_):
    return top15_buy_sell_rvolm_rows(n=15)


# =============================================================================
# FASTAPI APP
# =============================================================================
app = FastAPI(title="TurboTrades (No Auth)")

HERE = Path(__file__).resolve().parent
THEME_PATH = HERE / "assets" / "theme.css"


@app.on_event("startup")
async def _startup():
    seed_daily_stats_once(per_req_sleep=SEED_SLEEP_SEC)
    start_ticker_once()
    load_nfo_instruments_once()
    start_compute_loop_once()  # <--- ULTRA-FAST precompute thread
    await openinterest.on_startup()


@app.on_event("shutdown")
async def _shutdown():
    await openinterest.on_shutdown()


@app.get("/dash")
def dash_no_slash():
    return RedirectResponse(url="/dash/", status_code=307)


@app.get("/health")
def health():
    with LOCK:
        offline = (time.time() - LAST_TICK_TS) > 10 if LAST_TICK_TS else True
        base = {
            "status": "ok",
            "offline": offline,
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
    with CACHE_LOCK:
        base["cache_updated"] = dict(CACHE.get("updated") or {})
        base["cache_sizes"] = {
            "sector_agg": len(CACHE.get("sector_agg") or {}),
            "top15_gainers": len(CACHE.get("top15_gainers") or []),
            "top15_losers": len(CACHE.get("top15_losers") or []),
            "hvhr_gainers": len(CACHE.get("hvhr_gainers") or []),
            "hvhr_losers": len(CACHE.get("hvhr_losers") or []),
            "hot_gainers": len(CACHE.get("hot_gainers") or []),
            "hot_losers": len(CACHE.get("hot_losers") or []),
            "pcr_ready": bool(CACHE.get("pcr")),
        }
    return base


@app.get("/theme.css")
def theme_css():
    if THEME_PATH.exists():
        return FileResponse(THEME_PATH, media_type="text/css")
    return JSONResponse({"error": "theme.css not found"}, status_code=404)


@app.get("/")
def root():
    return RedirectResponse(url="/dash/", status_code=307)


# Mount OpenInterest FastAPI app (websocket-capable)
app.mount("/openinterest", openinterest.app)

# Mount Dash (WSGI)
app.mount("/dash", WSGIMiddleware(server))