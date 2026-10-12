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
        .finally(function () { location.reload(); });
    });
  });
})();
