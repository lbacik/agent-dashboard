/* Shared scroll keeper for live-refreshed regions on the main page.

   Any scroll container inside a live swap opts in with a stable key:

       <aside id="drawer" class="drawer" data-scroll-key="drawer">…

   Before a live swap (the `fleet` SSE table re-render, the drawer's
   `sse:fleet` refresh, the Activity widget's poll-cycle reload) the
   keeper records the horizontal and vertical scroll position of every
   keyed container inside the swap target (the target itself included),
   and after the swap it reapplies the saved position to the container
   with the same key. The browser clamps an out-of-range value to the
   new content, so a shorter list simply settles at its end.

   Positions live in page memory only (a per-swap pending list plus
   nothing else): a full page reload starts every view at its initial
   position, and nothing is written to browser storage. A key whose
   container is absent after the swap is simply skipped, with no
   errors. */
(function () {
  var pending = [];
  function readKey(el) {
    return el && el.getAttribute ? el.getAttribute("data-scroll-key") : null;
  }
  function collect(target) {
    var out = [];
    if (!target || !target.querySelectorAll) { return out; }
    var key = readKey(target);
    if (key) {
      out.push({
        key: key,
        left: target.scrollLeft,
        top: target.scrollTop,
      });
    }
    var found = target.querySelectorAll("[data-scroll-key]");
    for (var i = 0; i < found.length; i++) {
      /* Skip the target itself when the selector engine also returns it. */
      if (found[i] === target) { continue; }
      out.push({
        key: readKey(found[i]),
        left: found[i].scrollLeft,
        top: found[i].scrollTop,
      });
    }
    return out;
  }
  function findByKey(root, key) {
    if (!root || !root.querySelector) { return null; }
    try {
      return root.querySelector('[data-scroll-key="' + key + '"]');
    } catch (e) { return null; }
  }
  function restore(target) {
    for (var i = 0; i < pending.length; i++) {
      var saved = pending[i];
      var el = null;
      if (readKey(target) === saved.key) {
        el = target;
      } else {
        el = findByKey(target, saved.key);
      }
      if (!el) {
        el = findByKey(document, saved.key);
      }
      /* A key absent after the swap is simply skipped. */
      if (!el) { continue; }
      try {
        el.scrollLeft = saved.left;
        el.scrollTop = saved.top;
      } catch (e) { /* ignore: keep the live refresh quiet */ }
    }
    pending = [];
  }
  document.addEventListener("htmx:beforeSwap", function (e) {
    var target = e.detail && e.detail.target;
    pending = collect(target);
  });
  /* htmx fires htmx:beforeSwap only for AJAX responses; the `fleet` table
     arrives over SSE (htmx.swap), so record before that swap too. The
     sse-swap element is its own swap target here. */
  document.addEventListener("htmx:sseBeforeMessage", function (e) {
    pending = collect(e.target);
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    var target = e.detail && e.detail.target;
    restore(target);
  });
})();
