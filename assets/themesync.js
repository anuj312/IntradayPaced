// assets/themeSync.js
(function () {
  function syncBodyClassFromRoot(root) {
    if (!root) return;

    document.body.classList.remove("theme-dark", "theme-light");

    if (root.classList.contains("theme-light")) {
      document.body.classList.add("theme-light");
    } else {
      document.body.classList.add("theme-dark");
    }
  }

  function attachWhenReady() {
    var root = document.getElementById("theme-root");
    if (!root) return false;

    // Initial sync
    syncBodyClassFromRoot(root);

    // Re-sync whenever Dash changes className
    new MutationObserver(function () {
      syncBodyClassFromRoot(root);
    }).observe(root, { attributes: true, attributeFilter: ["class"] });

    return true;
  }

  // Dash mounts #theme-root after load; keep retrying briefly
  var tries = 0;
  var t = setInterval(function () {
    tries += 1;
    if (attachWhenReady()) {
      clearInterval(t);
    } else if (tries > 100) {
      // ~20 seconds (100 * 200ms)
      clearInterval(t);
    }
  }, 200);
})();