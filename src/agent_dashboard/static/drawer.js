/* Instance drawer helpers: closing drops the ?instance= parameter so a
   reload does not reopen the drawer; the recovery next action copies. */
function closeDrawer() {
  var holder = document.getElementById("drawer-holder");
  if (holder) { holder.innerHTML = ""; }
  var url = new URL(window.location.href);
  url.searchParams.delete("instance");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}

/* The drawer refresh swaps only the aside's inner content (hx-target="this"),
   so the `<aside id="drawer">` scroll container stays in place. Changing its
   content can still clamp scrollTop, so keep the position across the swap.
   Load-once parts of the drawer (hx-preserve) survive the same swap. */
(function () {
  var drawerScrollTop = null;
  document.addEventListener("htmx:beforeSwap", function (e) {
    var target = e.detail && e.detail.target;
    if (target && target.id === "drawer") { drawerScrollTop = target.scrollTop; }
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    var target = e.detail && e.detail.target;
    if (target && target.id === "drawer" && drawerScrollTop !== null) {
      target.scrollTop = drawerScrollTop;
      drawerScrollTop = null;
    }
  });
})();

/* Attempt statistics strip: click a day or drag across days to select a range.
   The strip carries data-base (the stats URL prefix), data-target (the card
   slot selector) and data-days (comma-separated ISO days); the range change
   swaps only the card, and the server remembers it in a cookie. */
(function () {
  var drag = null;
  function stripIndex(strip, e) {
    var rect = strip.querySelector("svg").getBoundingClientRect();
    var n = strip.dataset.days.split(",").length;
    var i = Math.floor((e.clientX - rect.left) / rect.width * n);
    return Math.max(0, Math.min(n - 1, i));
  }
  function markDrag() {
    var lo = Math.min(drag.a, drag.b), hi = Math.max(drag.a, drag.b);
    drag.strip.classList.add("is-dragging");
    drag.strip.querySelectorAll(".strip-day").forEach(function (r) {
      var i = parseInt(r.getAttribute("data-i"), 10);
      r.classList.toggle("is-drag", i >= lo && i <= hi);
    });
  }
  document.addEventListener("pointerdown", function (e) {
    var strip = e.target.closest && e.target.closest(".stats-strip");
    if (!strip || e.button !== 0) { return; }
    e.preventDefault();
    drag = { strip: strip, a: stripIndex(strip, e), b: stripIndex(strip, e) };
    markDrag();
  });
  document.addEventListener("pointermove", function (e) {
    if (!drag) { return; }
    var i = stripIndex(drag.strip, e);
    if (i !== drag.b) { drag.b = i; markDrag(); }
  });
  document.addEventListener("pointerup", function () {
    if (!drag) { return; }
    var days = drag.strip.dataset.days.split(",");
    var lo = Math.min(drag.a, drag.b), hi = Math.max(drag.a, drag.b);
    var url = drag.strip.dataset.base + "from=" + days[lo] + "&to=" + days[hi];
    var target = drag.strip.dataset.target;
    drag = null;
    htmx.ajax("GET", url, { target: target, swap: "innerHTML" });
  });
})();

/* Drawer Activity widget: the collapsed state survives the drawer's live
   refresh (the widget lives in an hx-preserve slot, so its <details>
   keeps its open attribute) and reopening/reload via localStorage. The
   summary line itself refreshes out-of-band on every poll cycle, so the
   header stays live while collapsed. */
(function () {
  var KEY = "drawer-activity-open";
  var load = function () {
    try {
      var raw = localStorage.getItem(KEY);
      if (raw === null) { return true; }
      return raw === "1";
    } catch (e) { return true; }
  };
  var save = function (open) {
    try { localStorage.setItem(KEY, open ? "1" : "0"); } catch (e) { /* ignore */ }
  };
  var apply = function () {
    var open = load();
    document.querySelectorAll('[data-activity-group="activity"]').forEach(function (el) {
      if (el.tagName === "DETAILS" && el.open !== open) { el.open = open; }
    });
  };
  document.addEventListener("toggle", function (e) {
    var el = e.target;
    if (el && el.dataset && el.dataset.activityGroup === "activity") {
      save(el.open);
    }
  }, true);
  document.addEventListener("htmx:afterSwap", apply);
  document.addEventListener("htmx:afterSettle", apply);
  document.addEventListener("htmx:sseMessage", apply);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", apply);
  } else {
    apply();
  }
})();

function copyNextAction() {
  var node = document.getElementById("recovery-next-action");
  if (!node) { return; }
  var text = node.textContent || "";
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text);
  } else {
    var area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    document.body.removeChild(area);
  }
}
