/* Control console composer: the live `$ agentctl …` preview and the
   per-action parameter field. The server renders the preview of the
   preselected action, so without this script the line simply stays put;
   with it, the preview follows the choice and the typed value, and only
   the chosen action's parameter field shows. Scoped to `.ctl-composer`
   so an SSE refresh that swaps the console body keeps working. */
(function () {
  function commandFor(input) {
    var template = input.getAttribute("data-cmd") || "";
    var form = input.closest("form");
    var n = form ? form.querySelector('input[name="n"]') : null;
    var issue = form ? form.querySelector('input[name="issue"]') : null;
    var nText = (n && n.value !== "") ? n.value : "N";
    var issueText = (issue && issue.value !== "") ? issue.value : "<n>";
    return template.replace("{n}", nText).replace("{issue}", issueText);
  }
  function sync(form) {
    if (!form) { return; }
    var checked = form.querySelector('input[name="action"]:checked');
    var preview = form.querySelector("[data-preview]");
    if (preview && checked) { preview.textContent = commandFor(checked); }
    var key = checked ? checked.value : "";
    form.querySelectorAll(".ctl-params label[data-for]").forEach(function (label) {
      if (label.getAttribute("data-for") === key) {
        label.removeAttribute("hidden");
      } else {
        label.setAttribute("hidden", "");
      }
    });
  }
  function formFor(target) {
    return target && target.closest ? target.closest(".ctl-composer") : null;
  }
  document.addEventListener("input", function (e) {
    sync(formFor(e.target));
  });
  document.addEventListener("change", function (e) {
    sync(formFor(e.target));
  });
  document.addEventListener("htmx:afterSettle", function (e) {
    var scope = e.target && e.target.querySelectorAll ? e.target : document;
    scope.querySelectorAll(".ctl-composer").forEach(sync);
  });
})();
