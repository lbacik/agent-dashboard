/* PROTOTYPE (#97), throwaway: variant switcher, scenario selects, the B
   sheet dialog, dismissable results, and C's live command preview. */
(function () {
  var bar = document.querySelector(".proto-switcher");
  if (bar) {
    var keys = bar.dataset.variants.split(",");
    var go = function (step) {
      var i = keys.indexOf(bar.dataset.current);
      var url = new URL(window.location.href);
      url.searchParams.set("variant", keys[(i + step + keys.length) % keys.length]);
      window.location.replace(url.toString());
    };
    bar.addEventListener("click", function (e) {
      var btn = e.target.closest("button[data-step]");
      if (btn) { go(parseInt(btn.dataset.step, 10)); }
    });
    document.addEventListener("keydown", function (e) {
      var t = e.target;
      if (t && (t.matches("input, textarea, select") || t.isContentEditable)) { return; }
      if (document.querySelector("dialog[open]")) { return; }
      if (e.key === "ArrowLeft") { go(-1); }
      if (e.key === "ArrowRight") { go(1); }
    });
    bar.querySelectorAll("select[data-scenario]").forEach(function (sel) {
      sel.addEventListener("change", function () {
        var body = new URLSearchParams(); body.set(sel.name, sel.value);
        fetch("/proto/scenario", { method: "POST", body: body }).then(function () {
          if (sel.hasAttribute("data-reload")) { window.location.reload(); }
        });
      });
    });
  }

  // 4xx/5xx Control action replies carry a fragment to show (422 input, 403
  // refused, 409 stale binding, 503 audit failure): swap them like 200.
  document.addEventListener("htmx:beforeSwap", function (e) {
    var st = e.detail.xhr.status;
    if (st === 403 || st === 409 || st === 422 || st === 503) {
      e.detail.shouldSwap = true; e.detail.isError = false;
    }
  });

  var dialog = document.getElementById("proto-ctl-dialog");
  document.addEventListener("htmx:afterSwap", function (e) {
    if (dialog && e.detail.target && e.detail.target.id === "proto-ctl-dialog-body") {
      if (e.detail.target.innerHTML.trim() && !dialog.open) { dialog.showModal(); }
    }
  });
  document.body.addEventListener("proto-close-dialog", function () {
    if (dialog && dialog.open) { dialog.close(); }
  });
  if (dialog) {
    dialog.addEventListener("close", function () { document.getElementById("proto-ctl-dialog-body").innerHTML = ""; });
  }
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-close-dialog]") && dialog) { dialog.close(); }
    var d = e.target.closest("[data-dismiss]");
    if (d) { d.closest(".ctl-result").remove(); }
  });

  // Toasts that reached "completed" fade out on their own.
  var sweep = function () {
    document.querySelectorAll(".ctl-result[data-autodismiss]:not([data-timer])").forEach(function (el) {
      el.setAttribute("data-timer", "1");
      setTimeout(function () { el.remove(); }, parseInt(el.dataset.autodismiss, 10));
    });
  };
  document.addEventListener("htmx:afterSettle", sweep);

  // Variant C: show the exact command for the chosen action and parameter.
  var preview = function (form) {
    var out = form.querySelector("[data-preview]");
    var opt = form.querySelector('input[name="action"]:checked');
    if (!out) { return; }
    if (!opt) { out.textContent = "(choose an action)"; return; }
    var n = form.querySelector('input[name="n"]').value || "N";
    var issue = form.querySelector('input[name="issue"]').value || "<n>";
    out.textContent = opt.dataset.cmd.replace("{n}", n).replace("{issue}", issue);
  };
  var all = function () { document.querySelectorAll(".ctl-c-composer").forEach(preview); };
  document.addEventListener("input", function (e) { var f = e.target.closest(".ctl-c-composer"); if (f) { preview(f); } });
  document.addEventListener("change", function (e) { var f = e.target.closest(".ctl-c-composer"); if (f) { preview(f); } });
  document.addEventListener("htmx:afterSettle", all);
  all();
})();
