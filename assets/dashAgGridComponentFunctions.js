// assets/dashAgGridComponentFunctions.js

var dagcomponentfuncs =
  (window.dashAgGridComponentFunctions = window.dashAgGridComponentFunctions || {});

// --- global formatter functions used by valueFormatter strings ---
function fmt2(v){ if (v === null || v === undefined || isNaN(v)) return ""; return Number(v).toFixed(2); }
function fmtSigned2(v){ if (v === null || v === undefined || isNaN(v)) return ""; const n = Number(v); return (n >= 0 ? "+" : "") + n.toFixed(2); }
function fmtPct(v){ if (v === null || v === undefined || isNaN(v)) return ""; const n = Number(v); return (n >= 0 ? "+" : "") + n.toFixed(2) + "%"; }
function fmtVolCompactIN(v){
  if (v === null || v === undefined || isNaN(v)) return "";
  const n = Number(v), a = Math.abs(n);
  if (a >= 1e7) return (n / 1e7).toFixed(2) + "Cr";
  if (a >= 1e5) return (n / 1e5).toFixed(2) + "L";
  if (a >= 1e3) return (n / 1e3).toFixed(2) + "K";
  return String(Math.round(n));
}

// export to window so Dash/AgGrid can see them
window.fmt2 = fmt2;
window.fmtSigned2 = fmtSigned2;
window.fmtPct = fmtPct;
window.fmtVolCompactIN = fmtVolCompactIN;

function _num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// --- Symbol cell renderer (TradingView link) ---
dagcomponentfuncs.SymbolCell = function (params) {
  const sym = params.value || "";
  const tvUrl =
    "https://www.tradingview.com/chart/?symbol=" +
    encodeURIComponent("NSE:" + sym) +
    "&interval=5";

  return window.React.createElement(
    "a",
    {
      href: tvUrl,
      target: "_blank",
      rel: "noopener noreferrer",
      className: "stock-sym",
      onClick: function (e) { e.stopPropagation(); },
    },
    sym
  );
};

// --- Stock cell renderer (symbol + company) ---
dagcomponentfuncs.StockCell = function (params) {
  const sym = params.value || "";
  const name = (params.data && params.data.Company) ? params.data.Company : "";

  const tvUrl =
    "https://www.tradingview.com/chart/?symbol=" +
    encodeURIComponent("NSE:" + sym) +
    "&interval=5";

  return window.React.createElement("div", { className: "stock-cell" }, [
    window.React.createElement(
      "a",
      {
        key: "sym",
        href: tvUrl,
        target: "_blank",
        rel: "noopener noreferrer",
        className: "stock-sym",
        onClick: function (e) { e.stopPropagation(); },
      },
      sym
    ),
    window.React.createElement(
      "div",
      { key: "nm", className: "stock-name", title: name },
      name
    ),
  ]);
};

// --- %Change pill ---
dagcomponentfuncs.PctPill = function (params) {
  const v = _num(params.value);
  if (v === null) {
    return window.React.createElement("span", { className: "val-pill neutral" }, "—");
  }
  const cls = v > 0 ? "val-pill up" : (v < 0 ? "val-pill down" : "val-pill neutral");
  const arrow = v > 0 ? "▲ " : (v < 0 ? "▼ " : "• ");
  return window.React.createElement("span", { className: cls }, arrow + fmtPct(v));
};

// --- RFactor pill ---
dagcomponentfuncs.RfactorPill = function (params) {
  const v = _num(params.value);
  if (v === null) {
    return window.React.createElement("span", { className: "val-pill rf neutral" }, "—");
  }
  return window.React.createElement("span", { className: "val-pill rf" }, fmt2(v) + "×");
};

// --- Volume pill ---
dagcomponentfuncs.VolPill = function (params) {
  const v = _num(params.value);
  if (v === null) {
    return window.React.createElement("span", { className: "val-pill vol neutral" }, "—");
  }
  return window.React.createElement("span", { className: "val-pill vol" }, fmtVolCompactIN(v));
};