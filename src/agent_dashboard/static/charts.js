/* Attempt history chart tooltips: one floating note for hover and focus.
   Marks carry their full text in `data-tip` (mirrored in <title>), so the
   charts stay readable with JavaScript disabled; this script only upgrades
   the native title into a readable, keyboard-accessible tooltip. No
   dependencies, no network fetches: safe offline over an SSH tunnel. */
(function() {
  var tip = document.getElementById("chart-tip");
  if (!tip) { return; }

  function place(x, y) {
    var pad = 12;
    var rect = tip.getBoundingClientRect();
    var left = x + 14;
    var top = y + 16;
    if (left + rect.width > window.innerWidth - pad) {
      left = x - rect.width - 14;
    }
    if (top + rect.height > window.innerHeight - pad) {
      top = y - rect.height - 14;
    }
    tip.style.left = Math.max(pad, left) + "px";
    tip.style.top = Math.max(pad, top) + "px";
  }

  function show(el, x, y) {
    var text = el.getAttribute("data-tip");
    if (!text) { return; }
    tip.textContent = text;
    tip.hidden = false;
    if (typeof x === "number" && typeof y === "number") {
      place(x, y);
    } else {
      var rect = el.getBoundingClientRect();
      place(rect.left + rect.width / 2, rect.top);
    }
  }

  function hide() {
    tip.hidden = true;
  }

  function bind(el) {
    el.addEventListener("mouseenter", function(e) { show(el, e.clientX, e.clientY); });
    el.addEventListener("mousemove", function(e) { place(e.clientX, e.clientY); });
    el.addEventListener("mouseleave", hide);
    el.addEventListener("focus", function() { show(el); });
    el.addEventListener("blur", hide);
  }

  var marks = document.querySelectorAll("[data-tip]");
  for (var i = 0; i < marks.length; i++) { bind(marks[i]); }
})();
