/* PROTOTYPE (throwaway): variant switcher plus collapse state that
   survives the SSE re-render of the fleet table. */
(function () {
  var bar = document.querySelector(".proto-switcher");
  if (bar) {
    var keys = bar.dataset.variants.split(",");
    var go = function (step) {
      var i = keys.indexOf(bar.dataset.current);
      var next = keys[(i + step + keys.length) % keys.length];
      if (bar.dataset.cookie) {
        document.cookie = bar.dataset.cookie + "=" + next + ";path=/;SameSite=Lax";
        window.location.reload();
        return;
      }
      var url = new URL(window.location.href);
      url.searchParams.set("variant", next);
      window.location.replace(url.toString());
    };
    bar.addEventListener("click", function (e) {
      var btn = e.target.closest("button[data-step]");
      if (btn) { go(parseInt(btn.dataset.step, 10)); }
    });
    document.addEventListener("keydown", function (e) {
      var t = e.target;
      if (t && (t.matches("input, textarea, select") || t.isContentEditable)) { return; }
      if (e.key === "ArrowLeft") { go(-1); }
      if (e.key === "ArrowRight") { go(1); }
    });
  }

  var KEY = "proto-open-groups";
  var load = function () {
    try { return JSON.parse(sessionStorage.getItem(KEY) || "{}"); } catch (e) { return {}; }
  };
  var save = function (state) {
    try { sessionStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* ignore */ }
  };
  var apply = function () {
    var state = load();
    document.querySelectorAll("[data-proto-group]").forEach(function (el) {
      var saved = state[el.dataset.protoGroup];
      var open = saved === undefined ? el.hasAttribute("data-proto-default-open") : !!saved;
      if (el.tagName === "DETAILS") { el.open = open; } else { el.classList.toggle("is-open", open); }
      var btn = el.querySelector("[data-proto-toggle]");
      if (btn) { btn.setAttribute("aria-expanded", open ? "true" : "false"); }
    });
  };
  document.addEventListener("toggle", function (e) {
    var el = e.target;
    if (el.dataset && el.dataset.protoGroup) {
      var state = load(); state[el.dataset.protoGroup] = el.open; save(state);
    }
  }, true);
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-proto-toggle]");
    if (!btn) { return; }
    var el = btn.closest("[data-proto-group]");
    var state = load(); state[el.dataset.protoGroup] = !el.classList.contains("is-open"); save(state);
    apply();
  });
  document.addEventListener("htmx:afterSwap", apply);
  document.addEventListener("htmx:afterSettle", apply);
  document.addEventListener("htmx:sseMessage", apply);
  apply();

  /* Drawer: keep the scroll position across in-place refreshes. */
  var drawerScroll = null;
  document.addEventListener("htmx:beforeSwap", function (e) {
    if (e.detail.target && e.detail.target.id === "drawer") { drawerScroll = e.detail.target.scrollTop; }
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    if (drawerScroll !== null && e.detail.target && e.detail.target.id === "drawer") {
      e.detail.target.scrollTop = drawerScroll;
      drawerScroll = null;
    }
  });

  /* Diagnostic: why does the drawer close by itself? Keeps the last htmx and
     SSE events and reports when the drawer vanishes or turns into "gone"
     without the close button. */
  var trail = [];
  var note = function (what) {
    trail.push(new Date().toISOString().slice(11, 23) + " " + what);
    if (trail.length > 40) { trail.shift(); }
  };
  var desc = function (el) {
    if (!el || !el.tagName) { return String(el); }
    return el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") + (el.className && typeof el.className === "string" ? "." + el.className.split(" ")[0] : "");
  };
  ["htmx:beforeRequest", "htmx:beforeSwap", "htmx:responseError", "htmx:sendError",
   "htmx:sseError", "htmx:sseOpen", "htmx:sseClose", "htmx:historyRestore", "htmx:historyCacheMiss"]
    .forEach(function (name) {
      document.addEventListener(name, function (e) {
        var d = e.detail || {};
        var path = (d.requestConfig && d.requestConfig.path) || (d.pathInfo && d.pathInfo.requestPath) || "";
        var status = d.xhr ? " " + d.xhr.status : "";
        note(name.replace("htmx:", "") + " " + desc(e.target) + (d.target ? " -> " + desc(d.target) : "") + " " + path + status);
      });
    });
  window.addEventListener("popstate", function () { note("popstate " + location.search); });
  window.addEventListener("pagehide", function () { note("pagehide"); });

  var explicitClose = 0;
  if (typeof window.closeDrawer === "function") {
    var original = window.closeDrawer;
    window.closeDrawer = function () { explicitClose = Date.now(); note("closeDrawer()"); original(); };
  }
  var report = function (reason) {
    console.warn("[proto] drawer: " + reason + "\n" + trail.join("\n"));
    var box = document.querySelector(".proto-diag");
    if (!box) { box = document.createElement("div"); box.className = "proto-diag"; document.body.appendChild(box); }
    box.textContent = "Drawer " + reason + " at " + new Date().toLocaleTimeString() + " — details in the browser console ([proto]).";
  };
  var holder = document.getElementById("drawer-holder");
  if (holder) {
    var hadDrawer = !!holder.querySelector("#drawer");
    var wasGone = false;
    new MutationObserver(function () {
      var drawer = holder.querySelector("#drawer");
      if (hadDrawer && !drawer && Date.now() - explicitClose > 1000) { report("closed without the close button"); }
      var gone = !!drawer && /This instance is gone/.test(drawer.textContent);
      if (gone && !wasGone) { report("turned into the \"gone\" fragment"); }
      wasGone = gone;
      hadDrawer = !!drawer;
    }).observe(holder, { childList: true, subtree: true });
  }

  /* Range card: remember the chosen range (cookie read by the server). */
  document.addEventListener("htmx:afterSwap", function (e) {
    var card = e.detail.target && e.detail.target.querySelector && e.detail.target.querySelector("[data-range-query]");
    if (card) {
      document.cookie = "proto_range=" + encodeURIComponent(card.dataset.rangeQuery) + ";path=/;SameSite=Lax";
    }
  });

  /* Range card C: click a day or drag across days on the activity strip. */
  var drag = null;
  var stripIndex = function (strip, e) {
    var rect = strip.querySelector("svg").getBoundingClientRect();
    var n = strip.dataset.days.split(",").length;
    var i = Math.floor((e.clientX - rect.left) / rect.width * n);
    return Math.max(0, Math.min(n - 1, i));
  };
  var markDrag = function () {
    var lo = Math.min(drag.a, drag.b), hi = Math.max(drag.a, drag.b);
    drag.strip.classList.add("is-dragging");
    drag.strip.querySelectorAll(".proto-strip-day").forEach(function (r) {
      var i = parseInt(r.dataset.i, 10);
      r.classList.toggle("is-drag", i >= lo && i <= hi);
    });
  };
  document.addEventListener("pointerdown", function (e) {
    var strip = e.target.closest && e.target.closest(".proto-strip");
    if (!strip || e.button !== 0) { return; }
    e.preventDefault();
    var i = stripIndex(strip, e);
    drag = { strip: strip, a: i, b: i };
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
    var url = drag.strip.dataset.base + "mode=dates&from=" + days[lo] + "&to=" + days[hi];
    var target = drag.strip.dataset.target;
    drag = null;
    htmx.ajax("GET", url, { target: target, swap: "innerHTML" });
  });
})();
