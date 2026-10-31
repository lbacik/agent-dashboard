/* Fleet Matrix filters: the KPI strip above the unified table toggles which
   rows are shown. Every `fleet` SSE re-render replaces the buttons and rows,
   so the active filter set lives in page memory only (a full reload resets
   to Online units) and is reapplied after every swap. No browser storage is
   used here. The Fleet units rail carries a scaled-down copy of the same
   buttons and the same `data-vc-*` rows, so one filter set drives both;
   the selected rail unit always stays listed (`drawer.js` re-applies the
   filters after every selection change). */
(function () {
  var vcFilters = { online: true, alerts: false, working: false, offline: false };

  var LABELS = { online: "ONLINE", alerts: "ALERTS", working: "WORKING", offline: "OFFLINE" };
  var ORDER = ["online", "alerts", "working", "offline"];

  function toggleVcFilter(key) {
    var nextVal = !vcFilters[key];
    vcFilters[key] = nextVal;
    if (nextVal && (key === "alerts" || key === "working")) {
      vcFilters.online = false;
    } else if (nextVal && key === "online") {
      vcFilters.alerts = false;
      vcFilters.working = false;
    }
  }

  function resetVcFilters() {
    vcFilters.online = true;
    vcFilters.alerts = false;
    vcFilters.working = false;
    vcFilters.offline = false;
  }

  function rowMatches(row) {
    if (vcFilters.online && row.hasAttribute("data-vc-online")) { return true; }
    if (vcFilters.alerts && row.hasAttribute("data-vc-alerts")) { return true; }
    if (vcFilters.working && row.hasAttribute("data-vc-working")) { return true; }
    if (vcFilters.offline && row.hasAttribute("data-vc-offline")) { return true; }
    return false;
  }

  function activeNames() {
    var out = [];
    for (var i = 0; i < ORDER.length; i++) {
      if (vcFilters[ORDER[i]]) { out.push(LABELS[ORDER[i]]); }
    }
    return out;
  }

  function scopeOf(el) {
    if (!el) { return document; }
    if (el.querySelectorAll) { return el; }
    return document;
  }

  function applyRailFilters() {
    var items = document.querySelectorAll("li.vc-rail-row");
    var shown = 0;
    for (var i = 0; i < items.length; i++) {
      var keep = rowMatches(items[i]) || !!items[i].querySelector(".vc-rail-item.is-selected");
      items[i].hidden = !keep;
      if (keep) { shown++; }
    }
    var groups = document.querySelectorAll(".vc-rail-stopped");
    for (var g = 0; g < groups.length; g++) {
      groups[g].hidden = !groups[g].querySelector("li.vc-rail-row:not([hidden])");
    }
    var empties = document.querySelectorAll("[data-vc-rail-empty]");
    for (var e = 0; e < empties.length; e++) {
      empties[e].hidden = items.length === 0 || shown > 0;
    }
  }

  function applyVcFilters(scope) {
    applyRailFilters();
    var root = scopeOf(scope);
    var rows = root.querySelectorAll
      ? root.querySelectorAll("tr.fleet-row")
      : document.querySelectorAll("tr.fleet-row");
    /* When the swap target is the fleet fragment itself, rows inside it are
       covered; fall back to the whole document when the scope holds none
       (e.g. an SSE swap whose target is the outer live region). */
    if (rows.length === 0 && root !== document) {
      rows = document.querySelectorAll("tr.fleet-row");
      root = document;
    }
    var shown = 0;
    for (var i = 0; i < rows.length; i++) {
      if (rowMatches(rows[i])) {
        rows[i].hidden = false;
        shown++;
      } else {
        rows[i].hidden = true;
      }
    }
    var buttons = (root === document ? document : root).querySelectorAll
      ? (root === document ? document : root).querySelectorAll("[data-vc-filter]")
      : document.querySelectorAll("[data-vc-filter]");
    if (buttons.length === 0) {
      buttons = document.querySelectorAll("[data-vc-filter]");
    }
    for (var b = 0; b < buttons.length; b++) {
      var key = buttons[b].getAttribute("data-vc-filter");
      var on = !!vcFilters[key];
      buttons[b].setAttribute("aria-pressed", on ? "true" : "false");
      if (on) {
        buttons[b].classList.add("is-on");
      } else {
        buttons[b].classList.remove("is-on");
      }
      var badge = buttons[b].querySelector(".vc-state");
      if (badge) { badge.textContent = on ? "ON" : "OFF"; }
    }
    var names = activeNames();
    var summaries = root.querySelectorAll
      ? root.querySelectorAll("[data-vc-summary]")
      : [];
    if (summaries.length === 0) {
      summaries = document.querySelectorAll("[data-vc-summary]");
    }
    var label = names.length ? names.join(" + ") + " (" + shown + " SHOWN)"
      : "NO FILTERS (" + shown + " SHOWN)";
    for (var s = 0; s < summaries.length; s++) {
      summaries[s].textContent = label;
    }
    var allOff = names.length === 0;
    var empties = root.querySelectorAll ? root.querySelectorAll("[data-vc-empty]") : [];
    if (empties.length === 0) { empties = document.querySelectorAll("[data-vc-empty]"); }
    for (var e = 0; e < empties.length; e++) {
      empties[e].hidden = !allOff;
    }
    var norunning = root.querySelectorAll
      ? root.querySelectorAll("[data-vc-no-running]")
      : [];
    if (norunning.length === 0) { norunning = document.querySelectorAll("[data-vc-no-running]"); }
    for (var n = 0; n < norunning.length; n++) {
      norunning[n].hidden = shown > 0 || allOff;
    }
  }

  window.applyVcFilters = applyVcFilters;

  document.addEventListener("click", function (e) {
    var tile = e.target && e.target.closest
      ? e.target.closest("[data-vc-filter]")
      : null;
    if (tile) {
      toggleVcFilter(tile.getAttribute("data-vc-filter"));
      applyVcFilters(document);
      return;
    }
    var reset = e.target && e.target.closest
      ? e.target.closest("[data-vc-reset]")
      : null;
    if (reset) {
      resetVcFilters();
      applyVcFilters(document);
    }
  });

  document.addEventListener("htmx:afterSwap", function (e) {
    var target = e.detail && e.detail.target;
    applyVcFilters(target || document);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      applyVcFilters(document);
    });
  } else {
    applyVcFilters(document);
  }
})();
