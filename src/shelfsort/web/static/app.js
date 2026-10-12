// Tiny helper: [data-poll] elements refresh themselves while a job runs;
// forms marked [data-ajax] submit via fetch and then reload the page.
(function () {
  document.querySelectorAll("[data-poll]").forEach(function (el) {
    var wasRunning = false;
    function tick() {
      if (!document.body.contains(el)) return;
      fetch(el.dataset.poll).then(function (r) {
        var running = r.headers.get("X-Job-Running") === "true";
        return r.text().then(function (html) {
          el.innerHTML = html;
          if (running) { wasRunning = true; setTimeout(tick, 2000); }
          else if (wasRunning) { location.reload(); }
        });
      }).catch(function () { setTimeout(tick, 2000); });
    }
    tick();
  });
  document.querySelectorAll("form[data-ajax]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      fetch(f.action, { method: f.method || "POST", body: new FormData(f) })
        .then(function (r) {
          if (r.ok) { location.reload(); return; }
          return r.text().then(function (t) {
            var box = document.getElementById("ajax-error");
            if (!box) {
              box = document.createElement("div");
              box.id = "ajax-error"; box.className = "error";
              f.parentNode.insertBefore(box, f);
            }
            box.textContent = t.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim() || ("Request failed: " + r.status);
          });
        })
        .catch(function () { location.reload(); });
    });
  });

  // Review shortcuts: j/k move between cards, a accept, q quarantine the focused file, s save.
  var cards = Array.prototype.slice.call(document.querySelectorAll("[data-card]"));
  if (!cards.length) return;
  document.addEventListener("keydown", function (e) {
    var tag = e.target.tagName;
    if (e.ctrlKey || e.metaKey || e.altKey || tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    var cur = e.target.closest ? e.target.closest("[data-card]") : null;
    if (e.key === "j" || e.key === "k") {
      var i = cards.indexOf(cur) + (e.key === "j" ? 1 : -1);
      cards[Math.max(0, Math.min(cards.length - 1, i))].focus();
    } else if (cur && e.key === "q") {
      var m = e.target.closest("[data-member]");
      var r = m && m.querySelector('input[value="quarantine"]');
      if (r) r.checked = true;
    } else if (cur && (e.key === "a" || e.key === "s")) {
      if (e.key === "a") cur.querySelectorAll('input[value="keep"]').forEach(function (k) { k.checked = true; });
      cur.requestSubmit();
    }
  });
})();
