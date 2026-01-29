// assets/dashAgGridComponentFunctions.js

var dagcomponentfuncs =
  window.dashAgGridComponentFunctions = window.dashAgGridComponentFunctions || {};

// --------------------
// Basic formatters (used by app.py valueFormatter calls)
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

// --------------------
// Spike chip renderer
// --------------------
dagcomponentfuncs.SpikeChip = function (params) {
  const v = params.value;
  if (v === null || v === undefined || isNaN(v)) return "";

  const n = Number(v);
  const cls = n >= 0 ? "spike-chip spike-pos" : "spike-chip spike-neg";
  const txt = (n >= 0 ? "+" : "") + n.toFixed(2);

  return React.createElement("span", { className: cls }, txt);
};

// --------------------
// Stock cell renderer (opens TradingView chart in NEW TAB)
// --------------------
dagcomponentfuncs.StockCell = function (params) {
  const sym = params.value || "";
  const name = (params.data && params.data.Company) ? params.data.Company : "";

  // TradingView symbol mapping
  const tvSymbol = "NSE:" + sym; // change to "BSE:" if needed
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
          onClick: function (e) {
            // prevent ag-grid row click/selection from also firing
            e.stopPropagation();
          },
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