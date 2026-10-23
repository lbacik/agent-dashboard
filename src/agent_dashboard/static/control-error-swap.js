/* htmx error swaps for the Control routes (spec section 6.4).
   htmx 2 does not swap 4xx/5xx responses. This opts in only the control
   routes' 403, 409, 422 and 503 responses, because each of them carries a
   fragment to show. Other routes keep the default. */
(function () {
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    var path = (e.detail.pathInfo && e.detail.pathInfo.requestPath) || "";
    if (
      /\/instances\/[^/]+\/control\//.test(path) &&
      [403, 409, 422, 503].indexOf(e.detail.xhr.status) !== -1
    ) {
      e.detail.shouldSwap = true;
      e.detail.isError = false;
    }
  });
})();
