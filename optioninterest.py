"""
optioninterest.py  (CLEAN: NIFTY FUT build-up only — movers removed)

Mounted in main app:
  http://127.0.0.1:8000/openinterest

Env:
  KITE_API_KEY
  KITE_ACCESS_TOKEN

What it does:
- Streams NIFTY near-month FUT ticks (LTP + OI) via KiteTicker
- Takes a baseline after 09:15 IST (first tick with LTP+OI)
- Computes ΔPrice and ΔOI from baseline
- Classifies build-up:
    LONG_BUILDUP / SHORT_BUILDUP / SHORT_COVERING / LONG_UNWINDING / NO_CLEAR
- Broadcasts state via WebSocket: /ws
- Minimal HTML UI at /
"""

import os
import json
import asyncio
import threading
import logging
from pathlib import Path
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
from collections import deque
from typing import Optional, Dict, Any

import pandas as pd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse

from kiteconnect import KiteConnect, KiteTicker


# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("openinterest")


# =============================================================================
# CONFIG
# =============================================================================
IST = ZoneInfo("Asia/Kolkata")

API_KEY = os.getenv("KITE_API_KEY", "").strip()
ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "").strip()
if not API_KEY or not ACCESS_TOKEN:
    raise RuntimeError("Missing KITE_API_KEY / KITE_ACCESS_TOKEN environment variables.")

TICK_WINDOW_SEC = 1.0


# =============================================================================
# FASTAPI APP
# =============================================================================
app = FastAPI(title="OpenInterest (NIFTY Fut Build-up)")


# Standalone CSS serving (also works when mounted)
HERE = Path(__file__).resolve().parent
THEME_PATH = HERE / "assets" / "theme.css"
OPTION_PATH = HERE / "assets" / "option.css"


@app.get("/theme.css")
def theme_css():
    if THEME_PATH.exists():
        return FileResponse(THEME_PATH, media_type="text/css")
    return JSONResponse({"error": "theme.css not found"}, status_code=404)


@app.get("/option.css")
def option_css():
    if OPTION_PATH.exists():
        return FileResponse(OPTION_PATH, media_type="text/css")
    return JSONResponse({"error": "option.css not found"}, status_code=404)


# =============================================================================
# LIVE STATE (NIFTY FUT)
# =============================================================================
state_lock = threading.Lock()
state: Dict[str, Any] = {
    "fut_token": None,
    "fut_symbol": None,

    "baseline_price": None,
    "baseline_oi": None,
    "baseline_time": None,

    "last_price": None,
    "last_oi": None,

    "dp": None,      # ΔPrice from baseline
    "doi": None,     # ΔOI from baseline

    "buildup_type": "NO_CLEAR",
    "bias": "NEUTRAL",
    "speed": "NA",
    "label": "Waiting for baseline…",

    "tick_count": 0,
    "last_tick_time": None,
}

tick_times = deque()
kws_client: Optional[KiteTicker] = None

_started = False
_baseline_task: Optional[asyncio.Task] = None
_stats_task: Optional[asyncio.Task] = None
_roll_task: Optional[asyncio.Task] = None


# =============================================================================
# WS MANAGER
# =============================================================================
class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self.lock:
            self.active.add(ws)

    async def disconnect(self, ws: WebSocket):
        async with self.lock:
            self.active.discard(ws)

    async def broadcast(self, message: dict):
        data = json.dumps(message, default=str)
        async with self.lock:
            dead = []
            for ws in self.active:
                try:
                    await ws.send_text(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.active.discard(ws)


manager = ConnectionManager()


# =============================================================================
# CORE LOGIC
# =============================================================================
def classify_buildup(dp: float, doi: int) -> dict:
    if dp > 0 and doi > 0:
        return {
            "buildup_type": "LONG_BUILDUP",
            "bias": "BULLISH",
            "speed": "NORMAL",
            "text": "Long build-up (often bullish)",
        }
    if dp < 0 and doi > 0:
        return {
            "buildup_type": "SHORT_BUILDUP",
            "bias": "BEARISH",
            "speed": "NORMAL",
            "text": "Short build-up (often bearish)",
        }
    if dp > 0 and doi < 0:
        return {
            "buildup_type": "SHORT_COVERING",
            "bias": "BULLISH",
            "speed": "FAST",
            "text": "Short covering (often bullish, fast)",
        }
    if dp < 0 and doi < 0:
        return {
            "buildup_type": "LONG_UNWINDING",
            "bias": "BEARISH",
            "speed": "WEAK",
            "text": "Long unwinding (often bearish, can be weak)",
        }
    return {
        "buildup_type": "NO_CLEAR",
        "bias": "NEUTRAL",
        "speed": "NA",
        "text": "No clear build-up",
    }


def pick_near_month_nifty_fut() -> tuple[int, str]:
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ACCESS_TOKEN)

    inst = pd.DataFrame(kite.instruments("NFO"))
    fut = inst[(inst["name"] == "NIFTY") & (inst["instrument_type"] == "FUT")].copy()
    fut["expiry"] = pd.to_datetime(fut["expiry"])

    today_ist = datetime.now(IST).date()
    fut = fut[fut["expiry"] >= pd.Timestamp(today_ist)].sort_values("expiry").iloc[0]
    return int(fut["instrument_token"]), str(fut["tradingsymbol"])


def next_reset_915_ist(now_ist: datetime) -> datetime:
    target = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    if now_ist >= target:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target


def next_rollcheck_914_ist(now_ist: datetime) -> datetime:
    target = now_ist.replace(hour=9, minute=14, second=0, microsecond=0)
    if now_ist >= target:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target


async def baseline_reset_loop():
    while True:
        now = datetime.now(IST)
        nxt = next_reset_915_ist(now)
        await asyncio.sleep(max(0.0, (nxt - now).total_seconds()))

        with state_lock:
            state["baseline_price"] = None
            state["baseline_oi"] = None
            state["baseline_time"] = None
            state["dp"] = None
            state["doi"] = None
            state["buildup_type"] = "NO_CLEAR"
            state["bias"] = "NEUTRAL"
            state["speed"] = "NA"
            state["label"] = "Baseline reset at 09:15 IST. Waiting for first OI snapshot…"

        await manager.broadcast(
            {"type": "status", "msg": "Baseline auto-reset at 09:15 IST. Waiting for first tick with OI."}
        )


async def stats_broadcast_loop():
    while True:
        await asyncio.sleep(1)
        now = datetime.now(IST)
        now_ts = now.timestamp()
        cutoff = now_ts - TICK_WINDOW_SEC

        with state_lock:
            while tick_times and tick_times[0] < cutoff:
                tick_times.popleft()
            tps = float(len(tick_times)) / float(TICK_WINDOW_SEC)

            payload = {
                "type": "stats",
                "server_time": now.isoformat(),
                "tick_count": state["tick_count"],
                "tps": round(tps, 2),
                "last_tick_time": state["last_tick_time"],
                "symbol": state["fut_symbol"],
            }

        await manager.broadcast(payload)


async def roll_to_near_month_if_needed():
    global kws_client
    try:
        new_token, new_symbol = pick_near_month_nifty_fut()
    except Exception as e:
        await manager.broadcast({"type": "status", "msg": f"Roll-check failed: {repr(e)}"})
        return

    with state_lock:
        old_token = state["fut_token"]
        old_symbol = state["fut_symbol"]

    if old_token == new_token:
        return

    now_iso = datetime.now(IST).isoformat()
    with state_lock:
        state["fut_token"] = new_token
        state["fut_symbol"] = new_symbol

        state["last_price"] = None
        state["last_oi"] = None
        state["baseline_price"] = None
        state["baseline_oi"] = None
        state["baseline_time"] = None

        state["dp"] = None
        state["doi"] = None
        state["buildup_type"] = "NO_CLEAR"
        state["bias"] = "NEUTRAL"
        state["speed"] = "NA"
        state["label"] = f"Rolled to {new_symbol}. Waiting for baseline (after 09:15 IST)."

        state["tick_count"] = 0
        state["last_tick_time"] = now_iso
        tick_times.clear()

    try:
        if kws_client is not None and old_token is not None:
            try:
                kws_client.unsubscribe([int(old_token)])
            except Exception:
                pass
            kws_client.subscribe([int(new_token)])
            kws_client.set_mode(kws_client.MODE_FULL, [int(new_token)])
    except Exception as e:
        await manager.broadcast({"type": "status", "msg": f"Resubscribe failed: {repr(e)}"})

    await manager.broadcast({"type": "status", "msg": f"ROLLED: {old_symbol} -> {new_symbol}"})


async def roll_loop():
    while True:
        now = datetime.now(IST)
        nxt = next_rollcheck_914_ist(now)
        await asyncio.sleep(max(0.0, (nxt - now).total_seconds()))
        await roll_to_near_month_if_needed()


def start_kite_ticker(loop: asyncio.AbstractEventLoop):
    global kws_client
    kws = KiteTicker(API_KEY, ACCESS_TOKEN)
    kws_client = kws

    def schedule(payload: dict):
        asyncio.run_coroutine_threadsafe(manager.broadcast(payload), loop)

    def on_connect(ws, _response):
        with state_lock:
            token = state["fut_token"]
            sym = state["fut_symbol"]
        if token is None:
            schedule({"type": "status", "msg": "No FUT token to subscribe yet"})
            return
        ws.subscribe([int(token)])
        ws.set_mode(ws.MODE_FULL, [int(token)])
        schedule({"type": "status", "msg": f"Subscribed: {sym} (token={token})"})

    def on_ticks(ws, ticks):
        if not ticks:
            return

        now = datetime.now(IST)
        now_iso = now.isoformat()
        now_ts = now.timestamp()

        t = ticks[0]
        ltp = t.get("last_price")
        oi = t.get("oi")

        with state_lock:
            state["tick_count"] += len(ticks)
            state["last_tick_time"] = now_iso

            for _ in range(len(ticks)):
                tick_times.append(now_ts)

            cutoff = now_ts - TICK_WINDOW_SEC
            while tick_times and tick_times[0] < cutoff:
                tick_times.popleft()

            if ltp is not None:
                state["last_price"] = ltp
            if oi is not None:
                state["last_oi"] = oi

            # Baseline after 09:15 IST: first tick that has both LTP+OI
            if (
                state["baseline_price"] is None
                and state["last_price"] is not None
                and state["last_oi"] is not None
                and now.time() >= dtime(9, 15)
            ):
                state["baseline_price"] = state["last_price"]
                state["baseline_oi"] = state["last_oi"]
                state["baseline_time"] = now_iso

            if state["baseline_price"] is None or state["baseline_oi"] is None:
                state["dp"] = None
                state["doi"] = None
                state["buildup_type"] = "NO_CLEAR"
                state["bias"] = "NEUTRAL"
                state["speed"] = "NA"
                state["label"] = "Waiting for baseline (need LTP+OI after 09:15 IST)."

                payload = {
                    "type": "tick",
                    "time": now_iso,
                    "symbol": state["fut_symbol"],
                    "ltp": state["last_price"],
                    "oi": state["last_oi"],
                    "baseline_price": state["baseline_price"],
                    "baseline_oi": state["baseline_oi"],
                    "baseline_time": state["baseline_time"],
                    "dp": None,
                    "doi": None,
                    "buildup_type": state["buildup_type"],
                    "bias": state["bias"],
                    "speed": state["speed"],
                    "label": state["label"],
                }
            else:
                dp = float(state["last_price"] - state["baseline_price"])
                doi = int(state["last_oi"] - state["baseline_oi"])
                info = classify_buildup(dp, doi)

                state["dp"] = dp
                state["doi"] = doi
                state["buildup_type"] = info["buildup_type"]
                state["bias"] = info["bias"]
                state["speed"] = info["speed"]
                state["label"] = info["text"]

                payload = {
                    "type": "tick",
                    "time": now_iso,
                    "symbol": state["fut_symbol"],
                    "ltp": state["last_price"],
                    "oi": state["last_oi"],
                    "baseline_price": state["baseline_price"],
                    "baseline_oi": state["baseline_oi"],
                    "baseline_time": state["baseline_time"],
                    "dp": dp,
                    "doi": doi,
                    "buildup_type": state["buildup_type"],
                    "bias": state["bias"],
                    "speed": state["speed"],
                    "label": state["label"],
                }

        schedule(payload)

    def on_close(ws, code, reason):
        schedule({"type": "status", "msg": f"KiteTicker closed: {code} {reason}"})

    def on_error(ws, code, reason):
        schedule({"type": "status", "msg": f"KiteTicker error: {code} {reason}"})

    kws.on_connect = on_connect
    kws.on_ticks = on_ticks
    kws.on_close = on_close
    kws.on_error = on_error
    kws.connect(threaded=True)


# =============================================================================
# STARTUP / SHUTDOWN (called by main app explicitly too)
# =============================================================================
@app.on_event("startup")
async def on_startup():
    global _started, _baseline_task, _stats_task, _roll_task
    if _started:
        return
    _started = True

    token, symbol = pick_near_month_nifty_fut()
    with state_lock:
        state["fut_token"] = token
        state["fut_symbol"] = symbol

    loop = asyncio.get_running_loop()
    start_kite_ticker(loop)

    _baseline_task = asyncio.create_task(baseline_reset_loop())
    _stats_task = asyncio.create_task(stats_broadcast_loop())
    _roll_task = asyncio.create_task(roll_loop())

    await roll_to_near_month_if_needed()


@app.on_event("shutdown")
async def on_shutdown():
    global kws_client, _started, _baseline_task, _stats_task, _roll_task
    for t in (_baseline_task, _stats_task, _roll_task):
        if t is not None:
            t.cancel()

    try:
        if kws_client is not None:
            kws_client.close()
    except Exception:
        pass

    kws_client = None
    _started = False


# =============================================================================
# API
# =============================================================================
@app.get("/status")
def get_status():
    with state_lock:
        return dict(state)


# =============================================================================
# UI (HTML)
# =============================================================================
HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>OpenInterest</title>

  <!-- Theme from main app (also available standalone via /theme.css) -->
  <link rel="stylesheet" href="/theme.css">
  <!-- OpenInterest-only overrides -->
  <link rel="stylesheet" href="option.css">
</head>

<body>
  <div class="topbar-wrap">
    <div class="page-wrap oi-topbar">
      <div class="oi-topbar-row">
        <div class="oi-brand">
          <div class="oi-kicker"><span class="oi-kdot"></span> OPENINTEREST • FUTURES BUILD‑UP</div>
          <div class="oi-title">NIFTY Near‑Month Build‑Up</div>
          <div class="oi-sub">
            Baseline auto‑resets at <b>09:15 IST</b>. Signal uses <b>ΔPrice</b> + <b>ΔOI</b>.
            Contract auto‑rolls daily check at <b>09:14 IST</b>.
          </div>
        </div>

        <div class="oi-live">
          <div class="oi-live-left">
            <div id="dot" class="oi-dot"></div>
            <div class="oi-live-text">
              <div class="oi-status" id="status">Connecting…</div>
              <div class="oi-miniStats">
                <span><b>Server</b> <span id="server_time">-</span></span>
                <span><b>Last tick</b> <span id="last_tick_time">-</span></span>
                <span><b>Ticks</b> <span id="tick_count">0</span></span>
                <span><b>TPS</b> <span id="tps">0</span></span>
              </div>
            </div>
          </div>
          <div id="connChip" class="stat-chip">WS</div>
        </div>
      </div>
    </div>
  </div>

  <div class="page-wrap oi-shell">
    <div class="oi-grid">

      <!-- Instrument -->
      <div class="oi-card" id="instrumentCard">
        <div class="oi-card-hd">
          <div class="oi-instrument">
            <div class="k">Instrument</div>
            <div class="v" id="symbol">-</div>
          </div>

          <div class="oi-tags">
            <div class="oi-tag neutral" id="typeTag">NO CLEAR</div>
            <div class="oi-tag neutral" id="biasTag">NEUTRAL</div>
            <div class="oi-tag neutral" id="speedTag">NA</div>
          </div>
        </div>

        <div class="oi-card-bd">
          <div class="oi-metrics">
            <div class="oi-metric">
              <div class="k">LTP</div>
              <div class="v" id="ltp">-</div>
            </div>
            <div class="oi-metric">
              <div class="k">Open Interest</div>
              <div class="v" id="oi">-</div>
            </div>
            <div class="oi-metric">
              <div class="k">Δ Price</div>
              <div class="v oi-num" id="dp">-</div>
            </div>
            <div class="oi-metric">
              <div class="k">Δ OI</div>
              <div class="v oi-num" id="doi">-</div>
            </div>
          </div>

          <div class="oi-row">
            <div class="k">Baseline</div>
            <div class="v" id="baseline">-</div>
          </div>

          <div class="oi-row">
            <div class="k">Payload time</div>
            <div class="v" id="tick_time">-</div>
          </div>

          <div class="oi-infer">
            <div class="k">Inference</div>
            <div class="v" id="labelText">Waiting…</div>
          </div>
        </div>
      </div>

      <!-- Diagnostics -->
      <div class="oi-card" id="diagCard">
        <div class="oi-card-hd">
          <div class="oi-instrument">
            <div class="k">Diagnostics</div>
            <div class="v">Raw Stream</div>
          </div>
          <div class="oi-tag neutral">JSON</div>
        </div>

        <div class="oi-card-bd">
          <details open>
            <summary>Raw JSON</summary>
            <pre id="raw">{}</pre>
          </details>
        </div>
      </div>

    </div>
  </div>

<script>
  // WS UI
  const statusEl  = document.getElementById("status");
  const dotEl     = document.getElementById("dot");
  const connChip  = document.getElementById("connChip");

  const typeTag  = document.getElementById("typeTag");
  const biasTag  = document.getElementById("biasTag");
  const speedTag = document.getElementById("speedTag");

  function fmtClockIST(iso){
    if(!iso) return "-";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleTimeString("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    });
  }

  function setTime(elId, iso){
    const el = document.getElementById(elId);
    if(!el) return;
    el.textContent = fmtClockIST(iso);
    el.title = iso || "";
  }

  function setConn(ok, text){
    if(ok){
      dotEl.className = "oi-dot ok";
      connChip.className = "stat-chip oi-chip-good";
      connChip.textContent = text || "LIVE";
    }else{
      dotEl.className = "oi-dot err";
      connChip.className = "stat-chip oi-chip-bad";
      connChip.textContent = text || "OFFLINE";
    }
  }

  function setTag(el, text, cls){
    el.textContent = text;
    el.className = "oi-tag " + (cls || "neutral");
  }

  function prettyType(t){
    if(!t) return "NO CLEAR";
    return String(t).replaceAll("_", " ");
  }

  function updateTags(msg){
    const t = msg.buildup_type || "NO_CLEAR";
    const b = msg.bias || "NEUTRAL";
    const s = msg.speed || "NA";

    let tc = "neutral";
    if(t === "LONG_BUILDUP") tc = "good";
    else if(t === "SHORT_BUILDUP") tc = "bad";

    let bc = (b === "BULLISH") ? "good" : (b === "BEARISH") ? "bad" : "neutral";
    let sc = "neutral";

    setTag(typeTag, prettyType(t), tc);
    setTag(biasTag, b, bc);
    setTag(speedTag, s, sc);
  }

  function setSigned(el, v, decimals){
    el.classList.remove("pos","neg","zero");
    if (v === undefined || v === null) { el.textContent = "-"; return; }
    const num = Number(v);
    if (!Number.isFinite(num)) { el.textContent = String(v); return; }

    if (decimals === null) el.textContent = String(num);
    else el.textContent = num.toFixed(decimals || 2);

    if (num > 0) el.classList.add("pos");
    else if (num < 0) el.classList.add("neg");
    else el.classList.add("zero");
  }

  // WebSocket path (works when mounted at /openinterest)
  const proto = (location.protocol === "https:") ? "wss" : "ws";
  const basePath = (location.pathname.endsWith("/")) ? location.pathname.slice(0, -1) : location.pathname;
  const ws = new WebSocket(proto + "://" + location.host + basePath + "/ws");

  ws.onopen  = () => { statusEl.textContent = "Connected"; setConn(true); };
  ws.onclose = () => { statusEl.textContent = "Disconnected"; setConn(false, "OFFLINE"); };
  ws.onerror = () => { statusEl.textContent = "Error"; setConn(false, "ERROR"); };

  function renderTickLike(msg){
    document.getElementById("raw").textContent = JSON.stringify(msg, null, 2);

    if (msg.symbol) document.getElementById("symbol").textContent = msg.symbol;
    setTime("tick_time", msg.time);

    if (msg.ltp !== undefined && msg.ltp !== null) document.getElementById("ltp").textContent = msg.ltp;
    if (msg.oi  !== undefined && msg.oi  !== null) document.getElementById("oi").textContent  = msg.oi;

    if (msg.baseline_price !== undefined && msg.baseline_price !== null) {
      document.getElementById("baseline").textContent =
        "P0=" + msg.baseline_price + ", OI0=" + msg.baseline_oi + ", T0=" + fmtClockIST(msg.baseline_time);
      document.getElementById("baseline").title = msg.baseline_time || "";
    } else {
      document.getElementById("baseline").textContent = "-";
      document.getElementById("baseline").title = "";
    }

    setSigned(document.getElementById("dp"), msg.dp, 2);
    setSigned(document.getElementById("doi"), msg.doi, null);

    document.getElementById("labelText").textContent = (msg.label || "—");
    updateTags(msg);
  }

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);

    if (msg.type === "status") { statusEl.textContent = msg.msg; return; }

    if (msg.type === "stats") {
      setTime("server_time", msg.server_time);
      setTime("last_tick_time", msg.last_tick_time);
      document.getElementById("tick_count").textContent = msg.tick_count || "0";
      document.getElementById("tps").textContent = msg.tps || "0";
      if (msg.symbol) document.getElementById("symbol").textContent = msg.symbol;
      return;
    }

    if (msg.type === "snapshot") { renderTickLike(msg); return; }
    renderTickLike(msg);
  };
</script>
</body>
</html>
"""


@app.get("/")
def home():
    return HTMLResponse(HTML)


# =============================================================================
# WEBSOCKET
# =============================================================================
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        with state_lock:
            snap = {"type": "snapshot", **dict(state)}
        await ws.send_text(json.dumps(snap, default=str))

        # keep the connection open; updates are pushed by broadcaster
        while True:
            await asyncio.sleep(60)

    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception:
        await manager.disconnect(ws)