var dagcomponentfuncs =
  window.dashAgGridComponentFunctions = window.dashAgGridComponentFunctions || {};

// basic formatters used in app.py valueFormatter calls
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

dagcomponentfuncs.SpikeChip = function (params) {
  const v = params.value;
  if (v === null || v === undefined || isNaN(v)) return "";
  const n = Number(v);
  const cls = n >= 0 ? "spike-chip spike-pos" : "spike-chip spike-neg";
  const txt = (n >= 0 ? "+" : "") + n.toFixed(2);
  return React.createElement("span", { className: cls }, txt);
};

dagcomponentfuncs.StockCell = function (params) {
  const sym = params.value || "";
  const name = (params.data && params.data.Company) ? params.data.Company : "";

  return React.createElement(
    "div",
    { className: "stock-cell" },
    [
      React.createElement("div", { className: "stock-sym" }, sym),
      React.createElement("div", { className: "stock-name" }, name),
    ]
  );
};