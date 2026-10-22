/* Instance drawer helpers: closing drops the ?instance= parameter so a
   reload does not reopen the drawer; the recovery next action copies. */
function closeDrawer() {
  var holder = document.getElementById("drawer-holder");
  if (holder) { holder.innerHTML = ""; }
  var url = new URL(window.location.href);
  url.searchParams.delete("instance");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}

/* The drawer refresh swaps only the aside's inner content (hx-target="this"),
   so the `<aside id="drawer">` scroll container stays in place. Changing its
   content can still clamp scrollTop, so keep the position across the swap.
   Load-once parts of the drawer (hx-preserve) survive the same swap. */
(function () {
  var drawerScrollTop = null;
  document.addEventListener("htmx:beforeSwap", function (e) {
    var target = e.detail && e.detail.target;
    if (target && target.id === "drawer") { drawerScrollTop = target.scrollTop; }
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    var target = e.detail && e.detail.target;
    if (target && target.id === "drawer" && drawerScrollTop !== null) {
      target.scrollTop = drawerScrollTop;
      drawerScrollTop = null;
    }
  });
})();

function copyNextAction() {
  var node = document.getElementById("recovery-next-action");
  if (!node) { return; }
  var text = node.textContent || "";
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text);
  } else {
    var area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    document.body.removeChild(area);
  }
}
