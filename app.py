import os
import time
import json
import base64
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, quote
from typing import Optional, Dict, Any
from http.cookies import SimpleCookie
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, FileResponse
from starlette.middleware.wsgi import WSGIMiddleware
from starlette.datastructures import Headers

import dash
from dash import dcc, html, Input, Output
import dash_bootstrap_components as dbc
import dash_ag_grid as dag

from kiteconnect import KiteConnect, KiteTicker

import firebase_admin
from firebase_admin import credentials, auth as fb_auth, firestore


# ------------------- MOUNT PATH -------------------
BASE = "/dash/"  # Browser URL: http://host:port/dash/


# ------------------- FIREBASE (Web config used on /login) -------------------
FIREBASE_WEB_CONFIG = {
    "apiKey": "AIzaSyCR0H-Rr3CVGzfxvNkOdnnLOQOJy73ctIU",
    "authDomain": "tradecorner-75138.firebaseapp.com",
    "projectId": "tradecorner-75138",
    "appId": "1:396741310115:web:98b3e5dba7a230857ab116",
}

SESSION_COOKIE_NAME = "session"
SESSION_EXPIRES_DAYS = int(os.getenv("SESSION_EXPIRES_DAYS", "7"))

# On Render (HTTPS) set COOKIE_SECURE=true. On localhost keep false.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")


# ------------------- FIREBASE ADMIN INIT -------------------
def init_firebase_admin():
    """
    Initializes Firebase Admin exactly once.

    Priority:
      1) FIREBASE_SERVICE_ACCOUNT_B64  (recommended on Render)
      2) FIREBASE_SERVICE_ACCOUNT_JSON (raw JSON string)
      3) FIREBASE_SERVICE_ACCOUNT_PATH (path to an existing json file)

    NOTE: We do NOT default to "service.json" because that file won't exist on Render
    unless you explicitly ship it (not recommended).
    """
    if firebase_admin._apps:
        return

    b64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64", "").strip()
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    path = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "").strip()

    cred = None

    if b64:
        try:
            info = json.loads(base64.b64decode(b64).decode("utf-8"))
            cred = credentials.Certificate(info)
        except Exception as e:
            raise RuntimeError(f"Invalid FIREBASE_SERVICE_ACCOUNT_B64: {e}") from e

    elif raw:
        try:
            info = json.loads(raw)
            cred = credentials.Certificate(info)
        except Exception as e:
            raise RuntimeError(f"Invalid FIREBASE_SERVICE_ACCOUNT_JSON: {e}") from e

    elif path:
        p = Path(path)
        if not p.exists():
            raise RuntimeError(
                f"FIREBASE_SERVICE_ACCOUNT_PATH was set to '{path}' but file does not exist."
            )
        cred = credentials.Certificate(str(p))

    else:
        raise RuntimeError(
            "Missing Firebase admin credentials. Set one of:\n"
            "  - FIREBASE_SERVICE_ACCOUNT_B64 (recommended)\n"
            "  - FIREBASE_SERVICE_ACCOUNT_JSON\n"
            "  - FIREBASE_SERVICE_ACCOUNT_PATH (must exist on server)\n"
        )

    firebase_admin.initialize_app(cred)


init_firebase_admin()
db = firestore.client()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def verify_session_cookie_from_headers(headers: Headers) -> Optional[dict]:
    cookie_hdr = headers.get("cookie", "") or ""
    c = SimpleCookie()
    c.load(cookie_hdr)
    morsel = c.get(SESSION_COOKIE_NAME)
    if not morsel:
        return None
    try:
        return fb_auth.verify_session_cookie(morsel.value, check_revoked=True)
    except Exception:
        return None


def user_display_name(decoded: Optional[dict]) -> str:
    if not decoded:
        return "User"
    nm = (decoded.get("name") or "").strip()
    if nm:
        return nm
    em = (decoded.get("email") or "").strip()
    if em and "@" in em:
        return em.split("@", 1)[0]
    return "User"


def get_uid(decoded: Optional[dict]) -> Optional[str]:
    return decoded.get("uid") if decoded else None


# ------------------- SUBSCRIPTIONS (Firestore) -------------------
PLAN_6M_PRICE = 4999
PLAN_LIFE_PRICE = 9999
PLAN_6M_DAYS = int(os.getenv("PLAN_6M_DAYS", "183"))  # ~6 months


def get_subscription(uid: str) -> Dict[str, Any]:
    """
    Firestore doc: subscriptions/{uid}
    """
    doc = db.collection("subscriptions").document(uid).get()
    if not doc.exists:
        return {"subscribed": False, "plan": None, "expires_at": None}

    data = doc.to_dict() or {}
    if data.get("status") != "active":
        return {"subscribed": False, "plan": data.get("plan"), "expires_at": data.get("expires_at")}

    plan = data.get("plan")
    expires_at = data.get("expires_at")  # Firestore timestamp -> datetime

    if not expires_at:
        # lifetime
        return {"subscribed": True, "plan": plan, "expires_at": None}

    try:
        exp = expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return {"subscribed": exp > utcnow(), "plan": plan, "expires_at": exp}
    except Exception:
        return {"subscribed": False, "plan": plan, "expires_at": None}


def activate_subscription(uid: str, plan: str, decoded_user: Optional[dict] = None):
    """
    Writes/updates:
      subscriptions/{uid}

    Stores user_name and user_email so Firebase console shows who purchased.
    """
    if not uid:
        raise ValueError("uid is required")

    plan = (plan or "").strip().lower()
    if plan not in ("6m", "lifetime"):
        raise ValueError("plan must be '6m' or 'lifetime'")

    if plan == "6m":
        expires_at = utcnow() + timedelta(days=PLAN_6M_DAYS)
        price = PLAN_6M_PRICE
    else:
        expires_at = None
        price = PLAN_LIFE_PRICE

    user_name = None
    user_email = None
    if decoded_user:
        user_name = (decoded_user.get("name") or "").strip() or None
        user_email = (decoded_user.get("email") or "").strip() or None

    ref = db.collection("subscriptions").document(uid)
    exists = ref.get().exists

    payload = {
        "status": "active",
        "plan": plan,
        "price_inr": price,
        "expires_at": expires_at,  # None for lifetime
        "user_name": user_name,
        "user_email": user_email,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    if not exists:
        payload["created_at"] = firestore.SERVER_TIMESTAMP

    ref.set(payload, merge=True)


# ------------------- AUTH GATE (login required) -------------------
class AuthGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = Headers(scope=scope)
        path = scope.get("path", "/")
        user = verify_session_cookie_from_headers(headers)

        if not user:
            nxt = quote(path)
            resp = RedirectResponse(url=f"/login?next={nxt}", status_code=307)
            resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
            return await resp(scope, receive, send)

        return await self.app(scope, receive, send)


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


# ------------------- KITE INIT -------------------
kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)

ins = pd.DataFrame(kite.instruments("NSE"))
ins = ins[ins["tradingsymbol"].isin(ALL_SYMBOLS)].copy()

symbol_to_token = dict(zip(ins["tradingsymbol"], ins["instrument_token"]))
symbol_to_name = dict(zip(ins["tradingsymbol"], ins.get("name", "")))
TOKENS = sorted(symbol_to_token.values())


# ------------------- LIVE STATE (ticks) -------------------
LOCK = threading.Lock()
LAST_PRICE: Dict[int, float] = {}
DAY_VOL: Dict[int, float] = {}
LAST_OHLC: Dict[int, dict] = {}

LAST_TICK_TS = 0.0
LAST_TICK_DT = None
TOTAL_TICKS = 0
TPS_WINDOW_SEC = 1.0
TPS_BUCKETS = deque()

# ------------------- DAILY STATS (RFactor baselines) -------------------
LOOKBACK_SESSIONS = 20
DAILY_STATS: Dict[int, Dict[str, Optional[float]]] = {}
DAILY_SEED_STARTED = False
DAILY_SEED_DONE = False
DAILY_SEED_PROGRESS = {"done": 0, "total": len(TOKENS)}
DAILY_SEED_ERRORS = 0


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


def _get_tps():
    if not TPS_BUCKETS:
        return 0.0
    return sum(c for _, c in TPS_BUCKETS) / TPS_WINDOW_SEC


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

    return ts


def compute_20d_daily_stats_for_token(token: int, days_back: int = 140):
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
    if df.empty or len(df) < LOOKBACK_SESSIONS + 1:
        return {"avg_vol_20": None, "avg_range_20": None, "avg_abs_ret_20": None}

    df = df.tail(LOOKBACK_SESSIONS + 1).copy()
    df["range"] = (df["high"] - df["low"]).astype(float)
    df["prev_close"] = df["close"].shift(1)
    df["ret_pct"] = (df["close"] - df["prev_close"]) / df["prev_close"] * 100.0
    df = df.dropna().tail(LOOKBACK_SESSIONS)

    return {
        "avg_vol_20": float(df["volume"].mean()),
        "avg_range_20": float(df["range"].mean()),
        "avg_abs_ret_20": float(df["ret_pct"].abs().mean()),
    }


def seed_daily_stats_once(per_req_sleep: float = 0.35):
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
                st = compute_20d_daily_stats_for_token(tok)
            except Exception:
                DAILY_SEED_ERRORS += 1
                st = {"avg_vol_20": None, "avg_range_20": None, "avg_abs_ret_20": None}

            with LOCK:
                DAILY_STATS[tok] = st

            DAILY_SEED_PROGRESS["done"] = i
            time.sleep(per_req_sleep)

        DAILY_SEED_DONE = True

    threading.Thread(target=_run, daemon=True).start()


def compute_rfactor_row_for_token(token: int):
    ltp = LAST_PRICE.get(token)
    vol_today = DAY_VOL.get(token)
    ohlc = LAST_OHLC.get(token) or {}

    prev_close = ohlc.get("close")
    day_high = ohlc.get("high")
    day_low = ohlc.get("low")

    if ltp is None or vol_today is None or prev_close is None:
        return None

    prev_close = float(prev_close)
    ltp = float(ltp)
    vol_today = float(vol_today)

    if prev_close <= 0 or ltp <= 0:
        return None

    pct_today = ((ltp - prev_close) / prev_close) * 100.0
    range_today = (float(day_high) - float(day_low)) if (day_high is not None and day_low is not None) else 0.0

    st = DAILY_STATS.get(token) or {}
    avg_vol_20 = st.get("avg_vol_20")
    avg_range_20 = st.get("avg_range_20")
    avg_abs_ret_20 = st.get("avg_abs_ret_20")
    if not avg_vol_20 or not avg_range_20 or not avg_abs_ret_20:
        return None

    eps = 1e-9
    rvol = vol_today / (float(avg_vol_20) + eps)
    range_factor = max(0.0, range_today) / (float(avg_range_20) + eps)
    move_factor = abs(pct_today) / (float(avg_abs_ret_20) + eps)

    rfactor = rvol * range_factor * move_factor
    dirr = (1.0 if pct_today >= 0 else -1.0) * rfactor

    return {"pct": pct_today, "rfactor": rfactor, "dirr": dirr, "ltp": ltp, "prev_close": prev_close}


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
            "Change%": round(float(rr["pct"]), 2),
            "RFactor": round(float(rr["rfactor"]), 2),
            "DirR": round(float(rr["dirr"]), 2),
        })

    if not rows:
        return [], []

    df = pd.DataFrame(rows).dropna(subset=["Change%", "RFactor"])
    gainers = df[df["Change%"] > 0].sort_values("RFactor", ascending=False).head(n).to_dict("records")
    losers = df[df["Change%"] < 0].sort_values("RFactor", ascending=False).head(n).to_dict("records")
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
        prev_close = rr["prev_close"]
        chg = ltp - prev_close

        rows.append({
            "Symbol": s,
            "Company": symbol_to_name.get(s, ""),
            "Price": round(float(ltp), 2),
            "Change": round(float(chg), 2),
            "Change%": round(float(rr["pct"]), 2),
            "RFactor": round(float(rr["rfactor"]), 2),
            "DirR": round(float(rr["dirr"]), 2),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return []
    df = df.sort_values("RFactor", ascending=False, na_position="last")
    return df.to_dict("records")


# ------------------- MARKET STATUS (LIVE/CLOSED) -------------------
IST = ZoneInfo("Asia/Kolkata")

def market_status() -> str:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return "CLOSED"

    preopen_dt = now.replace(hour=9, minute=0, second=0, microsecond=0)
    open_dt    = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_dt   = now.replace(hour=15, minute=30, second=0, microsecond=0)

    if preopen_dt <= now < open_dt:
        return "PREOPEN"
    if open_dt <= now < close_dt:
        return "OPEN"
    return "CLOSED"


# ------------------- BACKGROUND TICKER -------------------
_started = False

def start_ticker_once():
    global _started
    if _started:
        return
    _started = True

    def _run():
        kws = KiteTicker(API_KEY, ACCESS_TOKEN)

        def on_connect(ws, _):
            print("WS CONNECTED")
            ws.subscribe(TOKENS)
            ws.set_mode(ws.MODE_FULL, TOKENS)

        def on_ticks(ws, ticks):
            last_dt = None
            with LOCK:
                for t in ticks:
                    ts = update_from_tick(t)
                    if ts and (last_dt is None or ts > last_dt):
                        last_dt = ts
                _record_tick_batch(len(ticks), last_dt)

        def on_close(ws, code, reason):
            print("WS CLOSED:", code, reason)

        def on_error(ws, code, reason):
            print("WS ERROR:", code, reason)

        # (optional but useful)
        def on_reconnect(ws, attempts):
            print("WS RECONNECT attempt:", attempts)

        def on_noreconnect(ws):
            print("WS NORECONNECT (gave up)")

        kws.on_connect = on_connect
        kws.on_ticks = on_ticks
        kws.on_close = on_close
        kws.on_error = on_error
        kws.on_reconnect = on_reconnect
        kws.on_noreconnect = on_noreconnect

        kws.connect(threaded=True)

        while True:
            time.sleep(1)

    threading.Thread(target=_run, daemon=True).start()


# ------------------- DASH APP -------------------
dash_app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.CYBORG],
    requests_pathname_prefix=BASE,
    routes_pathname_prefix="/",
    assets_folder=os.path.join(os.path.dirname(__file__), "assets"),
    suppress_callback_exceptions=True,
)
server = dash_app.server


def get_user_from_dash_request() -> Optional[dict]:
    """
    Dash runs under Flask (WSGI). Read cookie from Flask request headers.
    """
    try:
        from flask import request as flask_request  # type: ignore
        cookie_hdr = flask_request.headers.get("Cookie", "") or ""
        return verify_session_cookie_from_headers(Headers({"cookie": cookie_hdr}))
    except Exception:
        return None


def subscription_overlay(uname: str):
    overlay_style = {
        "position": "fixed",
        "inset": "0",
        "background": "rgba(0,0,0,0.55)",
        "backdropFilter": "blur(10px)",
        "WebkitBackdropFilter": "blur(10px)",
        "display": "flex",
        "alignItems": "center",
        "justifyContent": "center",
        "zIndex": "9999",
        "padding": "18px",
    }
    panel_style = {
        "width": "min(720px, 96vw)",
        "borderRadius": "22px",
        "border": "1px solid rgba(255,255,255,0.14)",
        "background": "linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.035))",
        "boxShadow": "0 28px 80px rgba(0,0,0,0.65)",
        "padding": "18px 18px 16px",
    }
    plan_card = {
        "borderRadius": "18px",
        "border": "1px solid rgba(255,255,255,0.12)",
        "background": "rgba(0,0,0,0.18)",
        "padding": "14px",
        "boxShadow": "inset 0 1px 0 rgba(255,255,255,0.08)",
        "height": "100%",
    }
    btn = {
        "display": "inline-flex",
        "alignItems": "center",
        "justifyContent": "center",
        "width": "100%",
        "padding": "12px 14px",
        "borderRadius": "14px",
        "border": "1px solid rgba(255,255,255,0.14)",
        "background": "linear-gradient(135deg, rgba(34,211,238,0.95), rgba(59,130,246,0.85))",
        "color": "#06101A",
        "fontWeight": "900",
        "textDecoration": "none",
        "marginTop": "12px",
    }
    btn2 = dict(btn)
    btn2["background"] = "linear-gradient(135deg, rgba(168,85,247,0.92), rgba(236,72,153,0.72))"

    return html.Div(
        html.Div(
            html.Div(
                [
                    html.Div("Subscription Required", style={"fontWeight": "950", "fontSize": "20px"}),
                    html.Div(
                        f"Hi {uname}. Choose a plan to unlock the dashboard.",
                        style={"opacity": 0.72, "marginTop": "6px", "fontSize": "13px"},
                    ),
                    html.Hr(style={"borderColor": "rgba(255,255,255,0.10)"}),

                    dbc.Row(
                        [
                            dbc.Col(
                                html.Div(
                                    [
                                        html.Div("6 Months", style={"fontWeight": "950", "fontSize": "16px"}),
                                        html.Div("₹ 4999", style={"fontWeight": "950", "fontSize": "28px", "marginTop": "8px"}),
                                        html.Div("Access for 6 months • Full dashboard", style={"opacity": 0.7, "marginTop": "6px", "fontSize": "13px"}),
                                        html.A("Buy 6 Months", href="/subscribe/activate?plan=6m", style=btn),
                                    ],
                                    style=plan_card,
                                ),
                                md=6,
                            ),
                            dbc.Col(
                                html.Div(
                                    [
                                        html.Div("Lifetime", style={"fontWeight": "950", "fontSize": "16px"}),
                                        html.Div("₹ 9999", style={"fontWeight": "950", "fontSize": "28px", "marginTop": "8px"}),
                                        html.Div("Lifetime access • Full dashboard", style={"opacity": 0.7, "marginTop": "6px", "fontSize": "13px"}),
                                        html.A("Buy Lifetime", href="/subscribe/activate?plan=lifetime", style=btn2),
                                    ],
                                    style=plan_card,
                                ),
                                md=6,
                            ),
                        ],
                        className="g-2",
                    ),

                    html.Div(
                        "Note: Payment gateway is not wired yet. These buttons activate subscription for now.",
                        style={"opacity": 0.55, "marginTop": "12px", "fontSize": "12px"},
                    ),
                    html.Div([html.A("Logout", href="/logout")], style={"marginTop": "10px", "fontSize": "12px"}),
                ],
                style=panel_style,
            ),
            style=overlay_style,
        )
    )


def top_nav(uname: str, plan_text: str):
    state = market_live_closed()

    def pill(text: str, kind: str):
        cls = "nav-link top-tab"
        style = {"cursor": "default"}

        if kind == "live":
            style |= {
                "background": "linear-gradient(90deg, rgba(51,255,139,0.22), rgba(51,255,139,0.10))",
                "borderColor": "rgba(51,255,139,0.30)",
                "color": "rgba(51,255,139,0.95)",
                "boxShadow": "0 18px 44px rgba(51,255,139,0.10)",
            }
        elif kind == "closed":
            style |= {
                "background": "linear-gradient(90deg, rgba(255,81,102,0.22), rgba(255,81,102,0.10))",
                "borderColor": "rgba(255,81,102,0.30)",
                "color": "rgba(255,81,102,0.95)",
                "boxShadow": "0 18px 44px rgba(255,81,102,0.10)",
            }
        elif kind == "plan":
            style |= {
                "background": "linear-gradient(90deg, rgba(59,130,246,0.22), rgba(168,85,247,0.18))",
                "borderColor": "rgba(255,255,255,0.18)",
                "color": "rgba(255,255,255,0.92)",
                "boxShadow": "0 18px 44px rgba(59,130,246,0.10)",
            }

        return dbc.NavItem(html.Div(text, className=cls, style=style))

    return dbc.Nav(
        [
            pill(state, "live" if state == "LIVE" else "closed"),
            pill(uname, "user"),
            pill(plan_text, "plan"),
        ],
        pills=True,
        className="top-tabs",
    )


def locked_page():
    return dbc.Alert(
        "Dashboard is locked until you activate a subscription.",
        color="secondary",
        className="page-wrap",
    )


def sectors_page():
    common_cols = [
        {"field": "Symbol", "headerName": "Stock", "pinned": "left", "cellRenderer": "SymbolCell", "minWidth": 120},
        {"field": "Change%", "type": "rightAligned",
         "valueFormatter": {"function": "fmtPct(params.value)"},
         "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"}},
        {"field": "RFactor", "type": "rightAligned", "valueFormatter": {"function": "fmt2(params.value)"}},
        {"field": "DirR", "type": "rightAligned",
         "valueFormatter": {"function": "fmtSigned2(params.value)"},
         "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"}},
    ]

    grid_opts = {
        "getRowId": {"function": "params.data.Symbol"},
        "alwaysShowVerticalScroll": True,
        "animateRows": True,
    }

    return html.Div(
        [
            dcc.Interval(id="refresh_sectors", interval=2000, n_intervals=0),

            html.H4("Sectors", className="page-title"),
            html.Div(id="sector-bars", className="sector-bars-wrap"),
            html.Div("Sorted by AVG DirRFactor (sector strength).", className="hint"),

            html.Hr(),

            dbc.Row(
                [
                    dbc.Col(
                        [
                            html.H6("Top 15 Gainers (by RFactor)", className="mt-1"),
                            dag.AgGrid(
                                id="top15-gainers-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=common_cols,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(760px, 72vh)", "width": "100%"},
                            ),
                        ],
                        md=6,
                    ),
                    dbc.Col(
                        [
                            html.H6("Top 15 Losers (by RFactor)", className="mt-1"),
                            dag.AgGrid(
                                id="top15-losers-grid",
                                className="ag-theme-alpine-dark grid-wrap compact-grid",
                                columnDefs=common_cols,
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                                dashGridOptions=grid_opts,
                                style={"height": "min(760px, 72vh)", "width": "100%"},
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
                    {"field": "Symbol", "headerName": "Stock", "pinned": "left", "cellRenderer": "StockCell", "minWidth": 200},
                    {"field": "Price", "type": "rightAligned", "valueFormatter": {"function": "fmt2(params.value)"}},
                    {"field": "Change", "type": "rightAligned",
                     "valueFormatter": {"function": "fmtSigned2(params.value)"},
                     "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"}},
                    {"field": "Change%", "type": "rightAligned",
                     "valueFormatter": {"function": "fmtPct(params.value)"},
                     "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"}},
                    {"field": "RFactor", "type": "rightAligned", "valueFormatter": {"function": "fmt2(params.value)"}, "sort": "desc"},
                    {"field": "DirR", "type": "rightAligned",
                     "valueFormatter": {"function": "fmtSigned2(params.value)"},
                     "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"}},
                ],
                rowData=[],
                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                dashGridOptions={
                    "alwaysShowVerticalScroll": True,
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
        dcc.Interval(id="top_refresh", interval=1000, n_intervals=0),

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
    decoded = get_user_from_dash_request()
    uname = user_display_name(decoded)
    uid = get_uid(decoded)

    if not uid:
        nav = top_nav(uname, "PLAN —")
        return nav, dbc.Alert("Not authenticated.", color="danger", className="page-wrap")

    sub = get_subscription(uid)
    subscribed = bool(sub.get("subscribed"))
    plan_text = f"PLAN {(sub.get('plan') or 'ACTIVE').upper()}" if subscribed else "PLAN NO"

    nav = top_nav(uname, plan_text)

    if not subscribed:
        return nav, html.Div([locked_page(), subscription_overlay(uname)])

    pathname = pathname or f"{BASE}"
    if pathname in ("/dash", "/dash/"):
        pathname = f"{BASE}"

    if pathname.startswith(f"{BASE}sector/"):
        sector = unquote(pathname.split(f"{BASE}sector/")[1]).upper()
        body = sector_page(sector) if sector in SECTOR_DEFINITIONS else dbc.Alert("Sector not found", color="danger")
        return nav, body

    return nav, sectors_page()


@dash_app.callback(Output("top-stats", "children"), Input("top_refresh", "n_intervals"))
def update_top_stats(_):
    updated_str = datetime.now().strftime("%H:%M:%S")

    with LOCK:
        offline = (time.time() - LAST_TICK_TS) > 10 if LAST_TICK_TS else True
        tps = _get_tps()
        tot = TOTAL_TICKS
        d_done = DAILY_SEED_DONE
        d_done_n = DAILY_SEED_PROGRESS.get("done", 0)
        d_total = DAILY_SEED_PROGRESS.get("total", 0)

    # No username/plan here (no duplication)
    chips = [
        dbc.Badge("Offline" if offline else "Live",
                  color=("danger" if offline else "success"),
                  className="stat-badge"),
        html.Div(f"TPS {tps:.1f}", className="stat-chip"),
        html.Div(f"Ticks {tot:,}", className="stat-chip"),
    ]

    if not d_done:
        chips.append(dbc.Badge("Seeding", color="warning", className="stat-badge"))
        chips.append(html.Div(f"20D {d_done_n}/{d_total} (err {DAILY_SEED_ERRORS})", className="stat-chip"))

    chips.append(html.A("Logout", href="/logout", className="stat-chip", style={"cursor": "pointer"}))
    chips.append(html.Div(f"Updated {updated_str}", className="stat-chip"))
    return html.Div(chips, className="top-stats-wrap")


@dash_app.callback(
    Output("sector-bars", "children"),
    Input("refresh_sectors", "n_intervals"),
)
def render_sector_bars(_):
    decoded = get_user_from_dash_request()
    uid = get_uid(decoded)
    if not uid or not get_subscription(uid).get("subscribed"):
        return []

    with LOCK:
        scores = compute_sector_dirr_mean()

    items = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    max_abs = max([abs(v) for _, v in items] + [1e-6])

    base_h = 10
    scale_h = 150
    cap_h = 150

    children = []
    for sector, val in items:
        h = int(base_h + scale_h * (abs(val) / max_abs))
        h = min(h, cap_h)
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


@dash_app.callback(
    Output("top15-gainers-grid", "rowData"),
    Output("top15-losers-grid", "rowData"),
    Input("refresh_sectors", "n_intervals"),
)
def update_rfactor_leaderboards(_):
    decoded = get_user_from_dash_request()
    uid = get_uid(decoded)
    if not uid or not get_subscription(uid).get("subscribed"):
        return [], []

    with LOCK:
        gainers, losers = top_gainers_losers_rfactor_rows(n=15)
    return gainers, losers


@dash_app.callback(
    Output("grid", "rowData"),
    Input("refresh_sector", "n_intervals"),
    Input("url", "pathname"),
)
def update_grid(_, pathname):
    decoded = get_user_from_dash_request()
    uid = get_uid(decoded)
    if not uid or not get_subscription(uid).get("subscribed"):
        return []

    if not pathname or not pathname.startswith(f"{BASE}sector/"):
        return dash.no_update

    sector = unquote(pathname.split(f"{BASE}sector/")[1]).upper()
    if sector not in SECTOR_DEFINITIONS:
        return []

    with LOCK:
        return sector_rows_sorted_by_rfactor(sector)


# ------------------- FASTAPI APP -------------------
app = FastAPI(title="Stocker")

HERE = Path(__file__).resolve().parent
THEME_PATH = HERE / "assets" / "theme.css"


@app.on_event("startup")
def _startup():
    seed_daily_stats_once(per_req_sleep=0.35)
    start_ticker_once()


@app.get("/theme.css")
def theme_css():
    if THEME_PATH.exists():
        return FileResponse(THEME_PATH, media_type="text/css")
    return JSONResponse({"error": "theme.css not found"}, status_code=404)


LOGIN_HTML = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Login • TradeCorner</title>
  <link rel="stylesheet" href="/theme.css">
</head>
<body class="login-body">
  <div class="login-stage">
    <div class="login-blur"></div>
    <div class="login-cardWrap">
      <div class="login-panel">
        <div class="login-title">TradeCorner • Login</div>
        <div class="login-sub">Sign in with Google to access the dashboard.</div>
        <button class="login-btn" id="googleBtn">Login with Google</button>
        <div class="login-err" id="err"></div>
        <div class="login-foot">If login fails, check pop-up blockers.</div>
      </div>
    </div>
  </div>

  <script type="module">
    import {{ initializeApp }} from "https://www.gstatic.com/firebasejs/10.7.1/firebase-app.js";
    import {{ getAuth, GoogleAuthProvider, signInWithPopup }}
      from "https://www.gstatic.com/firebasejs/10.7.1/firebase-auth.js";

    const firebaseConfig = {json.dumps(FIREBASE_WEB_CONFIG)};
    const app = initializeApp(firebaseConfig);
    const auth = getAuth(app);
    const provider = new GoogleAuthProvider();

    function qs(name) {{
      const u = new URL(window.location.href);
      return u.searchParams.get(name) || "";
    }}

    async function sessionLogin(idToken) {{
      const res = await fetch("/auth/sessionLogin", {{
        method: "POST",
        headers: {{ "content-type": "application/json" }},
        credentials: "include",
        body: JSON.stringify({{ idToken }})
      }});
      if (!res.ok) {{
        const txt = await res.text();
        throw new Error(txt || ("HTTP " + res.status));
      }}
    }}

    document.getElementById("googleBtn").addEventListener("click", async () => {{
      const errEl = document.getElementById("err");
      errEl.textContent = "";
      try {{
        const result = await signInWithPopup(auth, provider);
        const idToken = await result.user.getIdToken(true);
        await sessionLogin(idToken);
        const next = qs("next") || "{BASE}";
        window.location.href = next;
      }} catch (e) {{
        errEl.textContent = String(e && e.message ? e.message : e);
      }}
    }});
  </script>
</body>
</html>
"""


@app.get("/login")
def login():
    return HTMLResponse(LOGIN_HTML)


@app.post("/auth/sessionLogin")
async def session_login(request: Request):
    data = await request.json()
    id_token = (data or {}).get("idToken", "")
    if not id_token:
        return JSONResponse({"error": "missing idToken"}, status_code=400)

    try:
        decoded = fb_auth.verify_id_token(id_token)
        expires_in = SESSION_EXPIRES_DAYS * 24 * 60 * 60
        session_cookie = fb_auth.create_session_cookie(id_token, expires_in=expires_in)
    except Exception as e:
        return JSONResponse({"error": f"auth failed: {repr(e)}"}, status_code=401)

    resp = JSONResponse({"ok": True, "uid": decoded.get("uid")})
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_cookie,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=expires_in,
        path="/",
    )
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=307)
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp


@app.get("/subscribe/activate")
def subscribe_activate(request: Request, plan: str = "6m"):
    """
    DEMO activation endpoint.
    Replace with real payment verification before calling activate_subscription().
    """
    decoded = verify_session_cookie_from_headers(Headers(request.headers))
    if not decoded:
        return RedirectResponse(url="/login?next=/dash/", status_code=307)

    uid = decoded.get("uid")
    try:
        activate_subscription(uid, plan, decoded_user=decoded)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    return RedirectResponse(url="/dash/", status_code=307)


@app.get("/api/me")
def api_me(request: Request):
    decoded = verify_session_cookie_from_headers(Headers(request.headers))
    if not decoded:
        return JSONResponse({"ok": False, "error": "not authenticated"}, status_code=401)

    uid = decoded.get("uid")
    nm = user_display_name(decoded)
    sub = get_subscription(uid)

    return JSONResponse({
        "ok": True,
        "uid": uid,
        "name": nm,
        "subscribed": bool(sub.get("subscribed")),
        "plan": sub.get("plan"),
        "expires_at": (sub.get("expires_at").isoformat() if sub.get("expires_at") else None),
    })


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
        }


@app.get("/")
def root():
    return RedirectResponse(url=f"{BASE}", status_code=307)


# Protect dashboard behind login session cookie
app.mount("/dash", AuthGate(WSGIMiddleware(server)))