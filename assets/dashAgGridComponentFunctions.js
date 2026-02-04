// assets/dashAgGridComponentFunctions.js

var dagcomponentfuncs =
  window.dashAgGridComponentFunctions = window.dashAgGridComponentFunctions || {};

// --------------------
// Basic formatters
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