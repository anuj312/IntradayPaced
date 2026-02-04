(function () {
  function sync() {
    var root = document.getElementById("theme-root");
    if (!root) return;

    document.body.classList.remove("theme-dark", "theme-light");

    if (root.classList.contains("theme-light")) {
      document.body.classList.add("theme-light");
    } else {
      document.body.classList.add("theme-dark");
    }
  }

  function init() {
    sync();
    var root = document.getElementById("theme-root");
    if (!root) return;

    new MutationObserver(sync).observe(root, { attributes: true, attributeFilter: ["class"] });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();