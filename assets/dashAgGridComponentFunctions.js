// assets/dashAgGridComponentFunctions.js

var dagcomponentfuncs =
  window.dashAgGridComponentFunctions = window.dashAgGridComponentFunctions || {};

// --------------------
// Basic formatters (used by valueFormatter strings in Python)
// --------------------
window.fmt2 = function (v) {
  if (v === null || v === undefined || isNaN(v)) return "";
  return Number(v).toFixed(2);
};

window.fmtSigned2 = function (v) {
  if (v === null || v === undefined || isNaN(v)) return "";
  const n = Number(v);
  return (n >= 0 ? "+" : "") + n.toFixed(2);
};

window.fmtPct = function (v) {
  if (v === null || v === undefined || isNaN(v)) return "";
  const n = Number(v);
  return (n >= 0 ? "+" : "") + n.toFixed(2) + "%";
};

window.fmtInt = function (v) {
  if (v === null || v === undefined || isNaN(v)) return "";
  return Number(v).toLocaleString("en-IN");
};

// Compact Indian units: K / L / Cr (premium look for Volume)
window.fmtVolCompactIN = function (v) {
  if (v === null || v === undefined || isNaN(v)) return "";
  const n = Number(v);
  const a = Math.abs(n);

  if (a >= 1e7) return (n / 1e7).toFixed(2) + "Cr";
  if (a >= 1e5) return (n / 1e5).toFixed(2) + "L";
  if (a >= 1e3) return (n / 1e3).toFixed(2) + "K";
  return String(Math.round(n));
};

// --------------------
// Helpers
// --------------------
function _num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// --------------------
// Symbol-only cell renderer (TradingView link)
// --------------------
dagcomponentfuncs.SymbolCell = function (params) {
  const sym = params.value || "";
  const tvSymbol = "NSE:" + sym;
  const tvUrl =
    "https://www.tradingview.com/chart/?symbol=" +
    encodeURIComponent(tvSymbol) +
    "&interval=5";

  return React.createElement(
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

// --------------------
// Stock cell renderer (symbol + company, TradingView new tab)
// --------------------
dagcomponentfuncs.StockCell = function (params) {
  const sym = params.value || "";
  const name = (params.data && params.data.Company) ? params.data.Company : "";

  const tvSymbol = "NSE:" + sym;
  const tvUrl =
    "https://www.tradingview.com/chart/?symbol=" +
    encodeURIComponent(tvSymbol) +
    "&interval=5";

  return React.createElement(
    "div",
    { className: "stock-cell" },
    [
      React.createElement(
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
      React.createElement(
        "div",
        { key: "nm", className: "stock-name", title: name },
        name
      ),
    ]
  );
};

// ======================================================================
// ULTRA PREMIUM VALUE RENDERERS (for your 4-col tables)
// Use these via columnDefs: cellRenderer: "PctPill" / "RfactorPill" / "VolPill"
// ======================================================================

// %Change pill with arrow + color
dagcomponentfuncs.PctPill = function (params) {
  const v = _num(params.value);
  if (v === null) {
    return React.createElement("span", { className: "val-pill neutral" }, "—");
  }

  const cls = v > 0 ? "val-pill up" : (v < 0 ? "val-pill down" : "val-pill neutral");
  const arrow = v > 0 ? "▲ " : (v < 0 ? "▼ " : "• ");
  const txt = arrow + window.fmtPct(v);

  return React.createElement("span", { className: cls }, txt);
};

// RFactor pill
dagcomponentfuncs.RfactorPill = function (params) {
  const v = _num(params.value);
  if (v === null) {
    return React.createElement("span", { className: "val-pill rf neutral" }, "—");
  }
  return React.createElement("span", { className: "val-pill rf" }, window.fmt2(v) + "×");
};

// Volume pill (compact units)
dagcomponentfuncs.VolPill = function (params) {
  const v = _num(params.value);
  if (v === null) {
    return React.createElement("span", { className: "val-pill vol neutral" }, "—");
  }

  // If you prefer comma format instead of K/L/Cr, replace next line with:
  // const txt = window.fmtInt(v);
  const txt = window.fmtVolCompactIN(v);

  return React.createElement("span", { className: "val-pill vol" }, txt);
};