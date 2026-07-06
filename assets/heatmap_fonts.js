// assets/heatmap_fonts.js
// sector big, stock small + keep treemap ROOT strip black even on hover

(function () {
  function applyTreemapTweaks(container) {
    try {
      const slices = container.querySelectorAll(".treemaplayer .trace .slice");

      slices.forEach((slice) => {
        const d = slice.__data__ || {};
        const id = (d.data && d.data.id) ? String(d.data.id) : String(d.id || "");
        const depth =
          (d.data && d.data.depth !== undefined) ? Number(d.data.depth) :
          (d.depth !== undefined) ? Number(d.depth) : NaN;

        const isRoot = (depth === 0) || (id === "root") || (id === "") || (!id);

        const path = slice.querySelector("path, rect");
        const textEl = slice.querySelector("text");

        if (isRoot) {
          // Force root strip black and prevent hover/click from affecting it
          slice.style.pointerEvents = "none";
          if (path) {
            path.style.fill = "#000000";
            path.style.stroke = "#000000";
            path.style.pointerEvents = "none";
          }
          if (textEl) textEl.style.display = "none";
          return;
        }

        // Sector nodes are "sec:XXXX"
        const isSector = id.startsWith("sec:");

        if (textEl) {
          textEl.style.fontSize = isSector ? "15px" : "10px";
          textEl.style.fontWeight = isSector ? "800" : "500";
        }
      });
    } catch (e) {
      // ignore
    }
  }

  function install() {
    const wrap = document.getElementById("market-heatmap");
    if (!wrap) return false;

    const gd = wrap.querySelector(".js-plotly-plot");
    if (!gd || typeof gd.on !== "function") return false;

    if (gd.__ttHeatmapInstalled) return true;
    gd.__ttHeatmapInstalled = true;

    const run = () => applyTreemapTweaks(wrap);

    // After initial render / updates
    gd.on("plotly_afterplot", run);
    gd.on("plotly_redraw", run);
    gd.on("plotly_relayout", run);

    // After hover re-paints
    gd.on("plotly_hover", () => setTimeout(run, 0));
    gd.on("plotly_unhover", () => setTimeout(run, 0));

    setTimeout(run, 0);
    return true;
  }

  const t = setInterval(() => {
    if (install()) clearInterval(t);
  }, 300);
})();