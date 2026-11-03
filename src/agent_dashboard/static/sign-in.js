// Sign-in only happens from a secure context (spec 8.1): outside one,
// disable every sign-in form and show the warning box. An open dashboard
// page also notices when its sign-in session ends (spec 5.6): on an SSE
// error or close, ask /whoami once; an ended session follows HX-Redirect
// to /login, while a dashboard restart leaves the SSE reconnect alone.
// Included only in LAN mode.
(function () {
  if (!window.isSecureContext) {
    document.querySelectorAll("form[data-secure-form]").forEach(function (form) {
      form
        .querySelectorAll("input, select, textarea, button")
        .forEach(function (field) {
          field.disabled = true;
        });
    });
    document.querySelectorAll("[data-secure-warn]").forEach(function (box) {
      box.hidden = false;
    });
  }

  var checking = false;
  function checkSession() {
    if (checking) {
      return;
    }
    if (typeof htmx === "undefined" || typeof htmx.ajax !== "function") {
      return;
    }
    checking = true;
    try {
      var result = htmx.ajax("GET", "/whoami", {swap: "none"});
      if (result && typeof result.finally === "function") {
        result.finally(function () {
          checking = false;
        });
      } else if (result && typeof result.then === "function") {
        result.then(
          function () {
            checking = false;
          },
          function () {
            checking = false;
          }
        );
      } else {
        checking = false;
      }
    } catch (err) {
      checking = false;
    }
  }
  document.body.addEventListener("htmx:sseError", checkSession);
  document.body.addEventListener("htmx:sseClose", checkSession);
})();
