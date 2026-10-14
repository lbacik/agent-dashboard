/* Instance drawer helpers: closing drops the ?instance= parameter so a
   reload does not reopen the drawer; the recovery next action copies. */
function closeDrawer() {
  var holder = document.getElementById("drawer-holder");
  if (holder) { holder.innerHTML = ""; }
  var url = new URL(window.location.href);
  url.searchParams.delete("instance");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}

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
