/* PROTOTYPE (#151), throwaway: variant switcher, scenario controls, the
   secure-context check, the delay countdown, password meters, and the
   redirect after the SSE stream closes. */
(function () {
  var bar = document.querySelector(".proto-switcher");
  var post = function (body) {
    return fetch("/proto/scenario", { method: "POST", body: new URLSearchParams(body) });
  };
  if (bar) {
    var keys = bar.dataset.variants.split(",");
    var go = function (step) {
      var i = keys.indexOf(bar.dataset.current);
      var url = new URL(window.location.href);
      url.searchParams.set("variant", keys[(i + step + keys.length) % keys.length]);
      window.location.replace(url.toString());
    };
    bar.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) { return; }
      if (b.dataset.step) { go(parseInt(b.dataset.step, 10)); }
      if (b.dataset.scenario) {
        var kv = b.dataset.scenario.split("=");
        var body = {}; body[kv[0]] = kv[1];
        // Expiry: leave the page alone so the stream close / next htmx request shows
        // what really happens. Everything else reloads.
        post(body).then(function () { if (kv[0] !== "expire") { window.location.reload(); } });
      }
    });
    bar.querySelectorAll("select[data-field]").forEach(function (sel) {
      sel.addEventListener("change", function () {
        var body = {}; body[sel.dataset.field] = sel.value;
        post(body).then(function () { window.location.reload(); });
      });
    });
    document.addEventListener("keydown", function (e) {
      var t = e.target;
      if (t && (t.matches("input, textarea, select") || t.isContentEditable)) { return; }
      if (e.key === "ArrowLeft") { go(-1); }
      if (e.key === "ArrowRight") { go(1); }
    });
  }

  // ADR 0011: the form is off unless window.isSecureContext (prototype: or simulated).
  var secure = window.isSecureContext && !window.PROTO_INSECURE;
  document.querySelectorAll("form[data-secure-form]").forEach(function (f) {
    if (secure) { return; }
    f.querySelectorAll("input, button").forEach(function (el) { el.disabled = true; });
  });
  document.querySelectorAll("[data-insecure-note]").forEach(function (el) { el.hidden = secure; });
  var led = document.querySelector("[data-secure-led]");
  if (led) {
    led.classList.add(secure ? "proto-led-on" : "proto-led-bad");
    document.querySelector("[data-secure-text]").textContent = secure ? "yes" : "no: sign-in is off";
    var https = window.location.protocol === "https:";
    document.querySelector("[data-cookie-kind]").textContent = https
      ? "__Host-, Secure, HttpOnly, SameSite=Strict"
      : "HttpOnly, SameSite=Strict (loopback http: no Secure)";
  }

  // Growing delay: count down on the button (B, C) and in the log line (C).
  document.querySelectorAll("button[data-countdown]").forEach(function (btn) {
    var left = parseInt(btn.dataset.countdown, 10) || 0;
    var text = document.querySelector("[data-countdown-text]");
    var tick = function () {
      if (left <= 0 || !secure) {
        if (secure) { btn.disabled = false; }
        btn.textContent = btn.dataset.label;
        if (text) { text.textContent = "0"; }
        return;
      }
      btn.disabled = true;
      btn.textContent = btn.dataset.label + " (" + left + " s)";
      if (text) { text.textContent = String(left); }
      left -= 1;
      setTimeout(tick, 1000);
    };
    if (left > 1) { tick(); }
  });

  // Password length: counter (A, C) and meter + match (B).
  var count = function (s) { return Array.from(s).length; };
  document.querySelectorAll("[data-pw-count]").forEach(function (inp) {
    var out = inp.closest("form").querySelector("[data-pw-count-out]");
    var upd = function () {
      var n = count(inp.value);
      out.textContent = n ? "(" + n + (n < 15 ? ", " + (15 - n) + " to go)" : ")") : "";
    };
    inp.addEventListener("input", upd); upd();
  });
  document.querySelectorAll("[data-pw-meter]").forEach(function (inp) {
    var form = inp.closest("form");
    var fill = form.querySelector("[data-pw-meter-fill]");
    var out = form.querySelector("[data-pw-meter-out]");
    var conf = form.querySelector("[data-pw-confirm]");
    var mout = form.querySelector("[data-pw-match-out]");
    var upd = function () {
      var n = count(inp.value);
      fill.style.width = Math.min(100, n / 128 * 100) + "%";
      fill.classList.toggle("is-ok", n >= 15);
      out.textContent = n + " / 15–128" + (n && n < 15 ? " · " + (15 - n) + " more" : "");
      if (conf.value) {
        mout.textContent = conf.value === inp.value ? "✓ match" : "✗ differs";
      } else { mout.textContent = ""; }
    };
    inp.addEventListener("input", upd); conf.addEventListener("input", upd); upd();
  });

  // When the sign-in session ends the server closes /stream; the reconnect gets a 401.
  // Ask a gated URL through htmx so the 401 + HX-Redirect takes the page to /login.
  var asked = false;
  var check = function () {
    if (asked || !window.htmx) { return; }
    asked = true;
    window.htmx.ajax("GET", "/proto-whoami", { target: "body", swap: "none" })
      .finally(function () { setTimeout(function () { asked = false; }, 3000); });
  };
  document.addEventListener("htmx:sseError", check);
  document.addEventListener("htmx:sseClose", check);
})();
