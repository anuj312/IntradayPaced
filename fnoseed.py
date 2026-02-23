# fno_seed.py
#
# Single source of truth for FNO prev-day OI seeding state.
# Seeding is triggered from app.py only. web.py only reads.

import time
import threading
from datetime import datetime, timedelta, date
from typing import Optional, Dict, Any, List

import pandas as pd


state_lock = threading.RLock()

# Cached FUT universe (NFO-FUT only, filtered by allowed underlyings)
FNO_FUT_DF: Optional[pd.DataFrame] = None
FNO_FUT_UPDATED_AT: Optional[str] = None

# Prev-day OI maps by expiry (ISO date string)
PREV_OI_BY_EXPIRY: Dict[str, Dict[int, int]] = {}
PREV_OI_PROGRESS: Dict[str, Dict[str, Any]] = {}

LAST_ERROR: Optional[str] = None
_started = False


def load_fno_futures_universe_once(kite, allowed_underlyings: List[str], ist) -> pd.DataFrame:
    """
    Loads NFO futures instruments and filters to:
      segment == NFO-FUT, instrument_type == FUT, name in allowed_underlyings

    Caches result in-memory.
    """
    global FNO_FUT_DF, FNO_FUT_UPDATED_AT, LAST_ERROR

    with state_lock:
        if FNO_FUT_DF is not None and not FNO_FUT_DF.empty:
            return FNO_FUT_DF

    try:
        df = pd.DataFrame(kite.instruments("NFO"))
        df = df[(df["segment"] == "NFO-FUT") & (df["instrument_type"] == "FUT")].copy()
        df["expiry"] = pd.to_datetime(df["expiry"]).dt.date

        allowed = set(allowed_underlyings)
        if "name" in df.columns:
            df = df[df["name"].isin(allowed)].copy()
        else:
            df = df.iloc[0:0].copy()

        with state_lock:
            FNO_FUT_DF = df
            FNO_FUT_UPDATED_AT = datetime.now(ist).isoformat()

        return df

    except Exception as e:
        LAST_ERROR = repr(e)
        with state_lock:
            FNO_FUT_DF = pd.DataFrame()
            FNO_FUT_UPDATED_AT = datetime.now(ist).isoformat()
        return FNO_FUT_DF


def near_expiry_from_df(df: pd.DataFrame, ist) -> Optional[date]:
    """Nearest expiry >= today(IST)."""
    if df is None or df.empty:
        return None
    today = datetime.now(ist).date()
    exps = sorted({e for e in df["expiry"].dropna().tolist() if e >= today})
    return exps[0] if exps else None


def _fetch_prevday_oi(kite, token: int, ist) -> Optional[int]:
    """
    Prev-day OI for token, via daily candles with oi=True.
    """
    end_date = datetime.now(ist).date()
    frm = end_date - timedelta(days=12)
    to = end_date - timedelta(days=1)

    candles = kite.historical_data(
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


def seed_prev_oi_for_expiry(
    kite,
    ist,
    expiry_: date,
    fut_df: pd.DataFrame,
    pace_sec: float = 0.35,
) -> None:
    """
    Seeds PREV_OI_BY_EXPIRY[expiry] for all FUT tokens in that expiry.
    Runs synchronously (caller can run inside a thread).
    """
    global LAST_ERROR

    expiry_s = str(expiry_)
    dfe = fut_df[fut_df["expiry"] == expiry_].copy()
    tokens = [int(x) for x in dfe["instrument_token"].dropna().tolist()]

    with state_lock:
        PREV_OI_PROGRESS[expiry_s] = {
            "running": True,
            "done": 0,
            "total": len(tokens),
            "errors": 0,
            "updated_at": datetime.now(ist).isoformat(),
        }

    cache: Dict[int, int] = {}
    done = 0
    err = 0

    for tok in tokens:
        try:
            oi_prev = _fetch_prevday_oi(kite, tok, ist)
            if oi_prev is not None:
                cache[int(tok)] = int(oi_prev)
        except Exception as e:
            LAST_ERROR = repr(e)
            err += 1

        done += 1
        with state_lock:
            PREV_OI_PROGRESS[expiry_s]["done"] = done
            PREV_OI_PROGRESS[expiry_s]["errors"] = err
            PREV_OI_PROGRESS[expiry_s]["updated_at"] = datetime.now(ist).isoformat()

        time.sleep(float(pace_sec))

    with state_lock:
        PREV_OI_BY_EXPIRY[expiry_s] = cache
        PREV_OI_PROGRESS[expiry_s]["running"] = False
        PREV_OI_PROGRESS[expiry_s]["updated_at"] = datetime.now(ist).isoformat()


def start_seed_near_expiry_once(
    kite,
    ist,
    allowed_underlyings: List[str],
    pace_sec: float = 0.35,
) -> None:
    """
    Starts ONE background thread (only once per process) to:
      - load FUT universe
      - pick near expiry
      - seed prev OI for that expiry
    """
    global _started
    if _started:
        return
    _started = True

    def _run():
        global LAST_ERROR
        try:
            fut_df = load_fno_futures_universe_once(kite, allowed_underlyings, ist)
            near = near_expiry_from_df(fut_df, ist)
            if not near:
                with state_lock:
                    PREV_OI_PROGRESS["NONE"] = {
                        "running": False, "done": 0, "total": 0, "errors": 0,
                        "updated_at": datetime.now(ist).isoformat(),
                    }
                return

            seed_prev_oi_for_expiry(
                kite=kite,
                ist=ist,
                expiry_=near,
                fut_df=fut_df,
                pace_sec=pace_sec,
            )
        except Exception as e:
            LAST_ERROR = repr(e)

    threading.Thread(target=_run, daemon=True).start()