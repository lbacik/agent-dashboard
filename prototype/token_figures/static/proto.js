// PROTOTYPE switcher: ←/→ cycles ?variant= and reloads (keeps ?instance=).
(function () {
  function go(step) {
    var keys = window.PROTO_VARIANTS, cur = keys.indexOf(window.PROTO_CURRENT);
    var next = keys[(cur + step + keys.length) % keys.length];
    var url = new URL(window.location.href);
    url.searchParams.set("variant", next);
    window.location.replace(url.toString());
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-p-step]");
    if (b) go(parseInt(b.getAttribute("data-p-step"), 10));
  });
  document.addEventListener("keydown", function (e) {
    var el = document.activeElement;
    if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
    if (e.key === "ArrowLeft") go(-1);
    if (e.key === "ArrowRight") go(1);
  });
})();
