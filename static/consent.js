(function () {
  var ID = window.GA_ID, KEY = "ga_consent";
  if (!ID) return;
  function get() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function set(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }
  function load() {
    if (window.__gaLoaded) return; window.__gaLoaded = true;
    var s = document.createElement("script"); s.async = true;
    s.src = "https://www.googletagmanager.com/gtag/js?id=" + ID; document.head.appendChild(s);
    window.dataLayer = window.dataLayer || [];
    window.gtag = function () { dataLayer.push(arguments); };
    gtag("js", new Date());
    gtag("config", ID);
    // Klicks auf Visit-X-Links als Ereignis zählen
    document.addEventListener("click", function (ev) {
      var a = ev.target.closest && ev.target.closest('a[rel~="sponsored"]');
      if (a) gtag("event", "affiliate_click", { link_url: a.href, link_text: (a.textContent || "").trim().slice(0, 80), page_path: location.pathname });
    }, true);
  }
  function banner() {
    if (document.getElementById("consent")) return;
    var b = document.createElement("div");
    b.id = "consent"; b.setAttribute("role", "dialog"); b.setAttribute("aria-label", "Cookie-Einstellungen");
    b.innerHTML = '<p>Wir möchten mit <strong>Google Analytics</strong> messen, welche Seiten gelesen werden, um die Seite zu verbessern. ' +
      'Dafür werden Cookies gesetzt und Daten an Google übertragen. <a href="/datenschutz/">Mehr dazu</a></p>' +
      '<div class="cbtn"><button type="button" class="no">Ablehnen</button><button type="button" class="yes">Akzeptieren</button></div>';
    document.body.appendChild(b);
    b.querySelector(".yes").addEventListener("click", function () { set("yes"); b.remove(); load(); });
    b.querySelector(".no").addEventListener("click", function () { set("no"); b.remove(); });
  }
  var c = get();
  if (c === "yes") load();
  else if (c !== "no") {
    // erst nach der Altersabfrage zeigen
    var t = setInterval(function () { if (!document.getElementById("agegate")) { clearInterval(t); banner(); } }, 400);
  }
  document.addEventListener("click", function (ev) {
    var l = ev.target.closest && ev.target.closest("[data-consent-open]");
    if (l) { ev.preventDefault(); try { localStorage.removeItem(KEY); } catch (e) {} banner(); }
  });
})();
