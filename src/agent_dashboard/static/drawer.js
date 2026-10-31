/* Instance drawer helpers: closing drops the ?instance= parameter so a
   reload does not reopen the drawer; the recovery next action copies. */
function closeDrawer() {
  var holder = document.getElementById("drawer-holder");
  if (holder) { holder.innerHTML = ""; }
  var url = new URL(window.location.href);
  url.searchParams.delete("instance");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}

/* Closing the cockpit is explicit: the drawer's back/close buttons call
   closeDrawer(), Escape closes, and Back/Forward (popstate) keeps the
   workspace in step with ?instance=. Clicking blank space never closes. */
document.addEventListener("keydown", function (e) {
  if (e.key !== "Escape" && e.key !== "Esc") { return; }
  if (!document.getElementById("drawer")) { return; }
  closeDrawer();
});

window.addEventListener("popstate", function () {
  var holder = document.getElementById("drawer-holder");
  if (!holder) { return; }
  var params = new URLSearchParams(window.location.search);
  var name = params.get("instance") || "";
  if (!name) {
    holder.innerHTML = "";
    return;
  }
  if (window.htmx && window.htmx.ajax) {
    window.htmx.ajax("GET", "/instances/" + encodeURIComponent(name), {
      target: "#drawer-holder",
      swap: "innerHTML",
    });
  } else {
    fetch("/instances/" + encodeURIComponent(name))
      .then(function (r) { return r.text(); })
      .then(function (html) { holder.innerHTML = html; })
      .catch(function () { /* keep the current workspace on fetch failure */ });
  }
});

/* The inspected rail item stays highlighted across `fleet` SSE swaps: the
   server marks it on the initial `?instance=` render and this reapplies it
   from the URL after every swap. */
(function () {
  function syncRailSelection() {
    var params;
    try {
      params = new URLSearchParams(window.location.search);
    } catch (e) { return; }
    var name = params.get("instance") || "";
    var items = document.querySelectorAll(".vc-rail-item");
    for (var i = 0; i < items.length; i++) {
      var el = items[i];
      var href = el.getAttribute("hx-get") || "";
      var prefix = "/instances/";
      var itemName = "";
      if (href.indexOf(prefix) === 0) {
        try {
          itemName = decodeURIComponent(href.slice(prefix.length));
        } catch (e) {
          itemName = href.slice(prefix.length);
        }
      }
      var on = !!name && itemName === name;
      if (on) {
        el.classList.add("is-selected");
        el.setAttribute("aria-current", "true");
      } else {
        el.classList.remove("is-selected");
        el.removeAttribute("aria-current");
      }
    }
  }
  document.addEventListener("htmx:afterSwap", syncRailSelection);
  document.addEventListener("htmx:afterSettle", syncRailSelection);
  document.addEventListener("htmx:sseMessage", syncRailSelection);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", syncRailSelection);
  } else {
    syncRailSelection();
  }
})();

/* Scroll position across the drawer's live refresh is kept by the shared
   keeper (`scroll-keep.js`): the drawer's `<aside id="drawer">` opts in
   with `data-scroll-key="drawer"`. */

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
