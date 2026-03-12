// assets/dashAgGridComponentFunctions.js
// Clean + safe global component/formatter registry for Dash AG Grid

(function () {
  const w = window;

  // Dash AG Grid registries
  const dagcomponentfuncs =
    (w.dashAgGridComponentFunctions = w.dashAgGridComponentFunctions || {});
  const dagfuncs =
    (w.dashAgGridFunctions = w.dashAgGridFunctions || {});

  // -----------------------------
  // Formatters
  // -----------------------------
  function toNum(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function fmt2(v) {
    const n = toNum(v);
    return n === null ? "" : n.toFixed(2);
  }

  function fmtSigned2(v) {
    const n = toNum(v);
    if (n === null) return "";
    return (n >= 0 ? "+" : "") + n.toFixed(2);
  }

  function fmtPct(v) {
    const n = toNum(v);
    if (n === null) return "";
    return (n >= 0 ? "+" : "") + n.toFixed(2) + "%";
  }

  // India compact volume: K / L / Cr
  function fmtVolCompactIN(v) {
    const n = toNum(v);
    if (n === null) return "";
    const a = Math.abs(n);
    if (a >= 1e7) return (n / 1e7).toFixed(2) + "Cr";
    if (a >= 1e5) return (n / 1e5).toFixed(2) + "L";
    if (a >= 1e3) return (n / 1e3).toFixed(2) + "K";
    return String(Math.round(n));
  }

  // Smart number (optional): trims trailing zeros
  // 23.00 -> "23", 1.20 -> "1.2", 10.30 -> "10.3"
  function fmtSmart(v) {
    const n = toNum(v);
    if (n === null) return "—";
    return n.toFixed(2).replace(/\.?0+$/, "");
  }

  function fmtPctSmart(v) {
    const n = toNum(v);
    if (n === null) return "—";
    const s = fmtSmart(n);
    return (n >= 0 ? "+" : "") + s + "%";
  }

  // Expose for Dash valueFormatter strings (recommended place)
  dagfuncs.toNum = toNum;
  dagfuncs.fmt2 = fmt2;
  dagfuncs.fmtSigned2 = fmtSigned2;
  dagfuncs.fmtPct = fmtPct;
  dagfuncs.fmtVolCompactIN = fmtVolCompactIN;
  dagfuncs.fmtSmart = fmtSmart;
  dagfuncs.fmtPctSmart = fmtPctSmart;

  // Also expose globally (harmless, helps debugging)
  w.toNum = toNum;
  w.fmt2 = fmt2;
  w.fmtSigned2 = fmtSigned2;
  w.fmtPct = fmtPct;
  w.fmtVolCompactIN = fmtVolCompactIN;
  w.fmtSmart = fmtSmart;
  w.fmtPctSmart = fmtPctSmart;

  // -----------------------------
  // Helpers
  // -----------------------------
  function getReact() {
    return w.React;
  }

  function tvUrlFor(sym) {
    const s = sym || "";

    // Special-case only what you asked for
    const tvSym = (s === "BAJAJ-AUTO") ? "BAJAJ_AUTO" : s;

    return (
      "https://www.tradingview.com/chart/?symbol=" +
      encodeURIComponent("NSE:" + tvSym) +
      "&interval=5"
    );
  }

  // -----------------------------
  // Cell renderers
  // -----------------------------

  // Simple symbol link
  dagcomponentfuncs.SymbolCell = function (params) {
    const React = getReact();
    if (!React) return params.value ?? "";

    const sym = params.value || "";
    const url = tvUrlFor(sym);

    return React.createElement(
      "a",
      {
        href: url,
        target: "_blank",
        rel: "noopener noreferrer",
        className: "stock-sym",
        onClick: function (e) {
          e.stopPropagation();
        },
      },
      sym
    );
  };

  // Stock cell: symbol + company (2 lines)
  dagcomponentfuncs.StockCell = function (params) {
    const React = getReact();
    if (!React) return params.value ?? "";

    const sym = params.value || "";
    const name = (params.data && params.data.Company) ? params.data.Company : "";
    const url = tvUrlFor(sym);

    return React.createElement("div", { className: "stock-cell" }, [
      React.createElement(
        "a",
        {
          key: "sym",
          href: url,
          target: "_blank",
          rel: "noopener noreferrer",
          className: "stock-sym",
          onClick: function (e) {
            e.stopPropagation();
          },
          title: sym,
        },
        sym
      ),
      React.createElement(
        "div",
        { key: "nm", className: "stock-name", title: name },
        name
      ),
    ]);
  };

  // %Change pill (with arrows) — used on home grids
  dagcomponentfuncs.PctPill = function (params) {
    const React = getReact();
    if (!React) return params.value ?? "";

    const v = toNum(params.value);
    if (v === null) {
      return React.createElement("span", { className: "val-pill neutral" }, "—");
    }

    const cls = v > 0 ? "val-pill up" : (v < 0 ? "val-pill down" : "val-pill neutral");
    const arrow = v > 0 ? "▲ " : (v < 0 ? "▼ " : "• ");
    return React.createElement("span", { className: cls }, arrow + fmtPct(v));
  };

  // RFactor pill
  dagcomponentfuncs.RfactorPill = function (params) {
    const React = getReact();
    if (!React) return params.value ?? "";

    const v = toNum(params.value);
    if (v === null) {
      return React.createElement("span", { className: "val-pill rf neutral" }, "—");
    }
    return React.createElement("span", { className: "val-pill rf" }, fmt2(v) + "×");
  };

  // Volume pill (compact IN)
  dagcomponentfuncs.VolPill = function (params) {
    const React = getReact();
    if (!React) return params.value ?? "";

    const v = toNum(params.value);
    if (v === null) {
      return React.createElement("span", { className: "val-pill vol neutral" }, "—");
    }
    return React.createElement("span", { className: "val-pill vol" }, fmtVolCompactIN(v));
  };

  // -----------------------------
  // NEW: Plain numeric cells for sector page (no pills)
  // Guaranteed fixed 2 decimals: 0.20, 1.20, 23.33
  // -----------------------------
  dagcomponentfuncs.Num2Cell = function (params) {
    const React = getReact();
    if (!React) return params.value ?? "";

    const v = toNum(params.value);
    if (v === null) return React.createElement("span", null, "—");
    return React.createElement("span", null, fmt2(v));
  };

  // Signed percent with 2 decimals: +0.20%
  dagcomponentfuncs.Pct2Cell = function (params) {
    const React = getReact();
    if (!React) return params.value ?? "";

    const v = toNum(params.value);
    if (v === null) return React.createElement("span", null, "—");
    return React.createElement("span", null, fmtPct(v));
  };
})();