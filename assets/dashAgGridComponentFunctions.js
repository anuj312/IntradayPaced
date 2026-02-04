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
// OPTIONAL: RFactor chip renderer
// --------------------
dagcomponentfuncs.RFactorChip = function (params) {
const v = params.value;
if (v === null || v === undefined || isNaN(v)) return "";

const n = Number(v);
let bg = "rgba(124,92,255,.18)";
let bd = "rgba(124,92,255,.35)";

if (n >= 10) { bg = "rgba(34,197,94,.18)"; bd = "rgba(34,197,94,.40)"; }
else if (n >= 5) { bg = "rgba(34,211,238,.16)"; bd = "rgba(34,211,238,.35)"; }
else if (n >= 2) { bg = "rgba(124,92,255,.16)"; bd = "rgba(124,92,255,.32)"; }

const style = {
display: "inline-block",
padding: "4px 10px",
borderRadius: "999px",
border: "1px solid " + bd,
background: bg,
fontWeight: 900,
fontFamily:
"ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono','Courier New', monospace",
};

return React.createElement("span", { style }, n.toFixed(2));
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