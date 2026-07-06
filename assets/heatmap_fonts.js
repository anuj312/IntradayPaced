// assets/heatmap_fonts.js
// sector big, stock small for Plotly treemap in Dash

(function () {
  function applyTreemapFonts(container) {
    try {
      const slices = container.querySelectorAll(".treemaplayer .trace .slice");
      slices.forEach((slice) => {
        const d = slice.__data__ || {};
        const id = (d.data && d.data.id) ? String(d.data.id) : String(d.id || "");
        const isSector = id.startsWith("sec:");

        const textEl = slice.querySelector("text");
        if (!textEl) return;

        textEl.style.fontSize = isSector ? "14px" : "10px";
        textEl.style.fontWeight = isSector ? "800" : "500";
      });
    } catch (e) {
      // ignore
    }
  }

  function install() {
    const wrap = document.getElementById("market-heatmap");
    if (!wrap) return false;

    // Plotly creates this inner div
    const gd = wrap.querySelector(".js-plotly-plot");
    if (!gd || typeof gd.on !== "function") return false;

    if (gd.__ttFontsInstalled) return true;
    gd.__ttFontsInstalled = true;

    const run = () => applyTreemapFonts(wrap);

    // Run on initial render + updates
    gd.on("plotly_afterplot", run);
    gd.on("plotly_redraw", run);
    gd.on("plotly_relayout", run);

    // run once now (in case it already rendered)
    setTimeout(run, 0);
    return true;
  }

  // Dash renders graphs async; poll until available
  const t = setInterval(() => {
    if (install()) clearInterval(t);
  }, 300);
})();