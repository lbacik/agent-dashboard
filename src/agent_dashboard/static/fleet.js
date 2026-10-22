/* Fleet table helpers: keep collapsible sections (such as "Not running")
   open or closed across the live table refresh. Every SSE `fleet` event
   re-renders the table, so the open state is remembered in sessionStorage
   and reapplied after each swap. */
(function () {
  var KEY = "fleet-open-sections";
  var load = function () {
    try { return JSON.parse(sessionStorage.getItem(KEY) || "{}"); } catch (e) { return {}; }
  };
  var save = function (state) {
    try { sessionStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* ignore */ }
  };
  var apply = function () {
    var state = load();
    document.querySelectorAll("details[data-persist]").forEach(function (el) {
      var name = el.getAttribute("data-persist");
      if (Object.prototype.hasOwnProperty.call(state, name)) {
        el.open = !!state[name];
      }
    });
  };
  document.addEventListener("toggle", function (e) {
    var el = e.target;
    if (el && el.tagName === "DETAILS" && el.hasAttribute("data-persist")) {
      var state = load();
      state[el.getAttribute("data-persist")] = el.open;
      save(state);
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
