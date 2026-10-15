(function () {
  var KEY = "ag18";
  try { if (localStorage.getItem(KEY) === "1") return; } catch (e) {}
  var wrap = document.createElement("div");
  wrap.id = "agegate";
  wrap.setAttribute("role", "dialog");
  wrap.setAttribute("aria-modal", "true");
  wrap.innerHTML =
    '<div class="box"><h2>Nur für Erwachsene</h2>' +
    "<p>Diese Seite enthält Informationen zu Angeboten für Erwachsene und richtet sich ausschließlich an Personen ab 18 Jahren.</p>" +
    '<button class="yes" type="button">Ich bin 18 oder älter</button>' +
    '<button class="no" type="button">Verlassen</button></div>';
  document.body.appendChild(wrap);
  wrap.querySelector(".yes").addEventListener("click", function () {
    try { localStorage.setItem(KEY, "1"); } catch (e) {}
    wrap.remove();
  });
  wrap.querySelector(".no").addEventListener("click", function () {
    window.location.href = "https://www.google.de";
  });
})();
