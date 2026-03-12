# app.py  (NO AUTH • NO FIREBASE • NO SUBSCRIPTIONS)
#
# Run:
#   uvicorn app:app --reload --workers 1

import os
import time
import math
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

# Dash page plugins
import web

# OpenInterest FastAPI app (mounted)
import optioninterest as openinterest

# FNO prev-OI seed module
import fnoseed


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
HOT_LAST_5M_KEY: Optional[str] = None

# Hot Now filters
HOT_MIN_RET_PCT = float(os.getenv("HOT_MIN_RET_PCT", "0.25"))       # min |spike%| to include
HOT_MIN_RANGE_PCT = float(os.getenv("HOT_MIN_RANGE_PCT", "0.40"))   # min window range%
HOT_CLOSE_POS_TH = float(os.getenv("HOT_CLOSE_POS_TH", "0.60"))

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

HOT_HISTORY: Dict[int, deque] = {}

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

def compute_rfactor_row_for_token(token: int):
    state_ = get_live_or_eod_state(token)
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

    rfactor_val = rvol * range_factor * move_factor
    dirr = (1.0 if pct_open >= 0 else -1.0) * rfactor_val

    return {
        "gap_pct": gap_pct,
        "pct_open": pct_open,
        "rfactor": float(rfactor_val),
        "dirr": float(dirr),
        "ltp": float(ltp),
        "day_open": float(day_open),
        "vol_today": float(vol_today),
    }


def _compute_hot_row_for_token(token: int):
    dq = HOT_HISTORY.get(token)
    if not dq or len(dq) < 2:
        return None

    now_epoch = float(dq[-1][0])
    cutoff = now_epoch - float(HOT_WINDOW_SEC)

    base = None
    for t, p, v in dq:
        if float(t) <= cutoff:
            base = (float(t), p, v)
        else:
            break
    if base is None:
        base = (float(dq[0][0]), dq[0][1], dq[0][2])

    base_t, base_p, base_v = base
    _, last_p, last_v = dq[-1]

    if base_p is None or float(base_p) <= 0 or last_p is None:
        return None

    win = [(float(t), p, v) for (t, p, v) in dq if float(t) >= base_t]
    prices = [float(p) for _, p, _ in win if p is not None]
    if len(prices) < 2:
        return None

    lo = float(min(prices))
    hi = float(max(prices))
    rng = float(hi - lo)

    base_pf = float(base_p)
    last_pf = float(last_p)

    range_pct = (rng / (base_pf + 1e-9)) * 100.0

    up_spike_pct = (hi - base_pf) / (base_pf + 1e-9) * 100.0
    down_spike_pct = (lo - base_pf) / (base_pf + 1e-9) * 100.0
    spike_pct = up_spike_pct if abs(up_spike_pct) >= abs(down_spike_pct) else down_spike_pct

    vol_win = None
    if base_v is not None and last_v is not None:
        vol_win = float(last_v) - float(base_v)
        if vol_win < 0:
            vol_win = None

    return {
        "range_pct": float(range_pct),
        "spike_pct": float(spike_pct),
        "vol_win": vol_win,
    }


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
    min_spike = float(HOT_MIN_RET_PCT)
    min_rng = float(HOT_MIN_RANGE_PCT)

    for sym in ALL_SYMBOLS:
        tok = symbol_to_token.get(sym)
        if not tok:
            continue

        hr = _compute_hot_row_for_token(tok)
        if not hr:
            continue

        spike = float(hr["spike_pct"])
        range_pct = float(hr["range_pct"])

        if abs(spike) < min_spike:
            continue
        if range_pct < min_rng:
            continue

        rows.append(
            {
                "Symbol": sym,
                "_spike": spike,
                "_abs_spike": abs(spike),
                "SPIKE%": round(spike, 2),
                "RNG5%": round(range_pct, 2),
                "DAY RNG%": None,
            }
        )

    if not rows:
        return [], []

    df = pd.DataFrame(rows).dropna(subset=["SPIKE%", "RNG5%"])
    if df.empty:
        return [], []

    gainers = (
        df[df["_spike"] > 0]
        .sort_values(["_abs_spike", "RNG5%"], ascending=[False, False])
        .head(n)[["Symbol", "SPIKE%", "RNG5%", "DAY RNG%"]]
        .to_dict("records")
    )
    losers = (
        df[df["_spike"] < 0]
        .sort_values(["_abs_spike", "RNG5%"], ascending=[False, False])
        .head(n)[["Symbol", "SPIKE%", "RNG5%", "DAY RNG%"]]
        .to_dict("records")
    )
    return gainers, losers


def sector_rows_sorted(sector: str, sort_by: str = "RFactor"):
    """
    Must return keys used by sector_page():
      Symbol, Company, DirR, Price, %Change, Gap%, RVOLm, RFactor
    """
    rows = []
    tf = _time_factor_ist_for_rvol(datetime.now(IST))

    for s in SECTOR_DEFINITIONS.get(sector, []):
        tok = symbol_to_token.get(s)
        if not tok:
            continue

        rr = compute_rfactor_row_for_token(tok)
        if not rr:
            continue

        pct_open = float(rr["pct_open"])
        gap_pct = float(rr["gap_pct"])
        ltp = float(rr["ltp"])

        st = DAILY_STATS.get(tok) or {}
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


def compute_sector_aggregates() -> Dict[str, Dict[str, float]]:
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

            rr = compute_rfactor_row_for_token(tok)
            if not rr:
                continue

            dirr_vals.append(float(rr["dirr"]))

            st = DAILY_STATS.get(tok) or {}
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
        dirr_mean = float(pd.Series(dirr_vals).mean()) if dirr_vals else 0.0

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


def compute_market_sentiment_proxy():
    adv = dec = unch = 0
    for tok in TOKENS:
        state_ = get_live_or_eod_state(tok)
        if not state_:
            continue
        ltp, _, ohlc = state_
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

web.register_fno_movers(
    dash_app,
    BASE=BASE,
    ctx={
        "ALL_SYMBOLS": ALL_SYMBOLS,
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


def _sector_grid_opts(sort_by: str) -> dict:
    sb = (sort_by or "RFactor").strip().upper()

    if sb in ("RVOL", "RVOLM"):
        sort_model = [{"colId": "rvolm", "sort": "desc"}]
    elif sb in ("DIRR", "DIR R"):
        sort_model = [{"colId": "dirr", "sort": "desc"}]
    elif sb in ("%CHANGE", "%CHG", "CHG"):
        sort_model = [{"colId": "pct", "sort": "desc"}]
    else:
        sort_model = [{"colId": "rfactor", "sort": "desc"}]

    return {
        "domLayout": "autoHeight",
        "animateRows": True,
        "suppressMenuHide": True,
        "suppressHeaderMenuButton": False,
        "suppressHeaderFilterButton": False,
        "alwaysShowVerticalScroll": False,
        "sortModel": sort_model,
    }


def sector_page(sector: str):
    coldefs = [
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
            "minWidth": 180,
            "flex": 1,
            "headerClass": "h-left",
            "cellClass": "c-left",
        },
        {
            "colId": "dirr",
            "field": "DirR",
            "headerName": "DIR R",
            "type": "rightAligned",
            "cellRenderer": "Num2Cell",  # <-- fixed 2 decimals
            "cellClassRules": {
                "cell-pos": "params.value > 0",
                "cell-neg": "params.value < 0",
            },
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
            "cellRenderer": "Num2Cell",  # <-- fixed 2 decimals
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
            "cellRenderer": "Pct2Cell",  # <-- +0.20%
            "cellClassRules": {
                "cell-pos": "params.value > 0",
                "cell-neg": "params.value < 0",
            },
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
            "cellRenderer": "Pct2Cell",  # <-- +0.20%
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
            "cellRenderer": "Num2Cell",  # <-- fixed 2 decimals
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
            "cellRenderer": "Num2Cell",  # <-- fixed 2 decimals
            "minWidth": 120,
            "maxWidth": 140,
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
        "onGridReady": {"function": "params.api.sizeColumnsToFit();"},
        "onGridSizeChanged": {"function": "params.api.sizeColumnsToFit();"},
    }

    title = sector.replace("_", " ").title()

    return html.Div(
        [
            dcc.Interval(id="refresh_sector", interval=2000, n_intervals=0),
            dbc.Row(
                [
                    dbc.Col(
                        dcc.Link("← Back", href=BASE, className="stat-chip", style={"textDecoration": "none"}),
                        width="auto",
                    ),
                    dbc.Col(html.H4(title, className="page-title mb-0"), width=True),
                    dbc.Col(
                        dbc.RadioItems(
                            id="sector-sort",
                            options=[
                                {"label": "Sort: RFactor", "value": "RFactor"},
                                {"label": "Sort: RVOLm", "value": "RVOLm"},
                                {"label": "Sort: DirR", "value": "DirR"},
                                {"label": "Sort: %Chg", "value": "%Change"},
                            ],
                            value="RFactor",
                            inline=True,
                        ),
                        width="auto",
                    ),
                ],
                className="align-items-center g-2 mb-2",
            ),
            dag.AgGrid(
                id="grid",
                className="ag-theme-alpine-dark grid-wrap compact-grid",
                columnDefs=coldefs,
                rowData=[],
                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                dashGridOptions=grid_opts,
                style={"width": "100%"},
            ),
        ],
        className="page-wrap",
    )


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
                                {"label": "Sort: DirR", "value": "DirR"},
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
            html.Div("RVOLm = (Σbuy−Σsell). RVOLm Mean = (Σbuy−Σsell)/N. DirR = mean directional rfactor.", className="hint"),

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
                    dbc.Col(dial_component("sentiment", "Sentiment Dial"), md=6),
                    dbc.Col(dial_component("pcr", "NIFTY OI PCR"), md=6),
                ],
                className="g-2 dials-row",
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
    ],
)


def _extract_sector_from_path(pn: str) -> Optional[str]:
    pn = (pn or "").strip()
    if "/sector/" not in pn:
        return None
    sector = unquote(pn.split("/sector/", 1)[1]).strip("/").upper()
    return sector or None


@dash_app.callback(Output("app-body", "children"), Input("url", "pathname"))
def route(pathname):
    pn = (pathname or "").strip() or "/"

    if pn in ("/", "/dash", "/dash/", BASE):
        return sectors_page()

    if pn in (f"{BASE}volm", f"{BASE}volm/"):
        return web.volm_page(BASE)

    if pn in (f"{BASE}fnomovers", f"{BASE}fnomovers/"):
        return web.fno_movers_page(BASE)

    if pn in (f"{BASE}openinterest", f"{BASE}openinterest/"):
        return html.Iframe(
            src="/openinterest",
            style={
                "width": "100%",
                "height": "calc(100vh - 140px)",
                "border": "0",
                "borderRadius": "16px",
            },
        )

    sector = _extract_sector_from_path(pn)
    if sector:
        return sector_page(sector) if sector in SECTOR_DEFINITIONS else dbc.Alert("Sector not found", color="danger")

    return sectors_page()


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
        sm = compute_market_sentiment_proxy()
        d_done = DAILY_SEED_DONE
        d_done_n = int(DAILY_SEED_PROGRESS.get("done", 0) or 0)
        d_total = int(DAILY_SEED_PROGRESS.get("total", 0) or 0)
        d_err = int(DAILY_SEED_ERRORS or 0)

    pn = compute_real_nifty_oi_pcr(strikes_around_atm=PCR_STRIKES_AROUND_ATM)

    sent_label = str(sm.get("label") or "NEUTRAL").upper()
    sent_score = float(sm.get("score") or 0.0)
    if sent_label == "BULLISH":
        sent_style = {"color": "var(--good)", "borderColor": "rgba(46, 213, 115, 0.55)"}
    elif sent_label == "BEARISH":
        sent_style = {"color": "var(--bad)", "borderColor": "rgba(255, 71, 87, 0.55)"}
    else:
        sent_style = {}

    sentiment_chip = html.Div(
        f"Sentiment: {sent_label} ({sent_score:+.2f})",
        className="stat-chip",
        style=sent_style,
        title=f"Adv {sm.get('adv',0)} • Dec {sm.get('dec',0)} • Unch {sm.get('unch',0)}",
    )

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
            f"NIFTY PCR: {pcr:.2f} ({pcr_lbl})",
            className="stat-chip",
            style=pcr_style,
            title=f"Expiry {pn.get('expiry')} • ATM {pn.get('atm')} • Updated {pn.get('updated_at')}",
        )
    else:
        pcr_chip = html.Div("NIFTY PCR: LOADING", className="stat-chip")

    chips = [
        dbc.Badge("Offline" if offline else "Live", color=("danger" if offline else "success"), className="stat-badge"),
        html.A("Volm", href=f"{BASE}volm", target="_blank", className="stat-chip",
               style={"textDecoration": "none", "marginLeft": "8px", "cursor": "pointer"}),
        html.A("FNO Movers", href=f"{BASE}fnomovers", target="_blank", className="stat-chip",
               style={"textDecoration": "none", "marginLeft": "8px", "cursor": "pointer"}),

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
        html.Div(f"Updated {updated_str}", className="stat-chip"),
    ]
    return html.Div(chips, className="top-stats-wrap")


@dash_app.callback(
    Output("sector-bars", "children"),
    Input("refresh_sectors", "n_intervals"),
    Input("sectors-sort", "value"),
)
def render_sector_bars(_, sort_by):
    sort_by = (sort_by or "RVOLmMean").strip()

    try:
        with LOCK:
            agg = compute_sector_aggregates()

        if sort_by == "DirR":
            metric = "DirR"
        elif sort_by == "RVOLmMean":
            metric = "RVOLmNetMean"
        else:
            metric = "RVOLmNetSum"

        items = sorted(
            agg.items(),
            key=lambda kv: float(kv[1].get(metric, 0.0) or 0.0),
            reverse=True,
        )
        if not items:
            return [html.Div("Loading sector bars…", className="hint")]

        vals = [abs(float(v.get(metric, 0.0) or 0.0)) for _, v in items]
        raw_max = max(vals) if vals else 0.0

        def nice_ceil(x: float) -> float:
            x = float(x)
            if x <= 0:
                return 1.0
            exp = math.floor(math.log10(x))
            f = x / (10 ** exp)
            if f <= 1:
                nf = 1
            elif f <= 2:
                nf = 2
            elif f <= 5:
                nf = 5
            else:
                nf = 10
            return float(nf * (10 ** exp))

        tick_max = nice_ceil(raw_max)
        tick_half = tick_max / 2.0

        def fmt(x: float) -> str:
            return f"{x:.2f}"

        axis = html.Div(
            html.Div(
                [
                    html.Div(fmt(tick_max), className="sector-axis-tick"),
                    html.Div(fmt(tick_half), className="sector-axis-tick"),
                    html.Div(fmt(0.0), className="sector-axis-tick"),
                    html.Div(fmt(-tick_half), className="sector-axis-tick"),
                    html.Div(fmt(-tick_max), className="sector-axis-tick"),
                ],
                className="sector-axis-ticks",
            ),
            className="sector-hist-axis",
        )

        children = [axis]

        max_bar = int(os.getenv("SECTOR_MAX_BAR_PX", "160"))
        denom = tick_max if tick_max > 1e-9 else 1.0

        for sector, m in items:
            val = float(m.get(metric, 0.0) or 0.0)
            disp = sector.replace("_", " ").upper()
            val_str = f"{val:+.2f}"

            bar_h = int(6 + max_bar * (abs(val) / denom))
            bar_h = min(bar_h, max_bar)

            children.append(
                dcc.Link(
                    href=f"{BASE}sector/{sector}",
                    className="sector-hist-link",
                    children=html.Div(
                        [
                            html.Div(
                                [
                                    html.Div(disp, className="sector-hist-tip-name"),
                                    html.Div(val_str, className="sector-hist-tip-val"),
                                ],
                                className="sector-hist-tooltip",
                            ),
                            html.Div(
                                [
                                    html.Div(
                                        className=("sector-hist-bar pos" if val >= 0 else "sector-hist-bar neg"),
                                        style={"height": f"{bar_h}px"},
                                    )
                                ],
                                className="sector-hist-track",
                            ),
                            html.Div(disp, className="sector-hist-name"),
                        ],
                        className="sector-hist-col",
                        title=f"{metric} {val_str}",
                    ),
                )
            )

        return children

    except Exception:
        log.exception("render_sector_bars crashed")
        return [html.Div("Sector bars error (see logs).", className="hint")]


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

    pn = compute_real_nifty_oi_pcr(strikes_around_atm=PCR_STRIKES_AROUND_ATM)

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
                html.Span(
                    f"NIFTY OI PCR {pcr:.2f} • PE {pe_txt} • CE {ce_txt}",
                    className="dial-meta",
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
    global HOT_LAST_5M_KEY

    now = datetime.now(IST)
    bucket_min = (now.minute // 5) * 5
    key = f"{now.date().isoformat()} {now.hour:02d}:{bucket_min:02d}"

    if HOT_LAST_5M_KEY is None:
        HOT_LAST_5M_KEY = key
        with LOCK:
            return top_hot_now_rows(n=15)

    if (now.minute % 5) != 0 or now.second > 2:
        return dash.no_update, dash.no_update

    if key == HOT_LAST_5M_KEY:
        return dash.no_update, dash.no_update

    HOT_LAST_5M_KEY = key
    with LOCK:
        return top_hot_now_rows(n=15)


@dash_app.callback(
    Output("grid", "rowData"),
    Output("grid", "dashGridOptions"),
    Input("refresh_sector", "n_intervals"),
    Input("url", "pathname"),
    Input("sector-sort", "value"),
)
def update_grid(_n, pathname, sort_by):
    pn = (pathname or "").strip()
    sector = _extract_sector_from_path(pn)
    if not sector:
        return dash.no_update, dash.no_update

    if sector not in SECTOR_DEFINITIONS:
        return [], _sector_grid_opts(sort_by)

    with LOCK:
        rows = sector_rows_sorted(sector, sort_by=sort_by)

    return rows, _sector_grid_opts(sort_by)


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

    def _start_fno_seed_when_ready():
        while True:
            with LOCK:
                done = DAILY_SEED_DONE
            if done:
                break
            time.sleep(2)

        fnoseed.start_seed_near_expiry_once(
            kite=kite,
            ist=IST,
            allowed_underlyings=ALL_SYMBOLS,
            pace_sec=float(os.getenv("FNO_PREV_OI_PACE_SEC", "0.35")),
        )

    threading.Thread(target=_start_fno_seed_when_ready, daemon=True).start()
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
        base = {
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

    try:
        with fnoseed.state_lock:
            futdf = fnoseed.FNO_FUT_DF
            fut_loaded = bool(futdf is not None and not futdf.empty)
            near = fnoseed.near_expiry_from_df(futdf, IST) if fut_loaded else None
            near_s = str(near) if near else None
            prog = dict(fnoseed.PREV_OI_PROGRESS.get(near_s) or {}) if near_s else {}
            base.update({
                "fno_fut_loaded": fut_loaded,
                "fno_near_expiry": near_s,
                "fno_prev_oi_progress": prog,
                "fno_last_error": getattr(fnoseed, "LAST_ERROR", None),
            })
    except Exception as e:
        base.update({"fno_prev_oi_error": repr(e)})

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