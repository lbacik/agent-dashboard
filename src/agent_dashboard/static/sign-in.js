// Sign-in only happens from a secure context (spec 8.1): outside one,
// disable every sign-in form and show the warning box. Included only in
// LAN mode, on the sign-in pages.
(function () {
  if (window.isSecureContext) {
    return;
  }
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
})();
