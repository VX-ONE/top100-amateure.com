"""Schnelle Regressionstests für die geschäftskritischen Teile (Sperrliste, Affiliate-Links, Header, Audit-Parser).

Ausführen: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from seo import config, keywords, render, videos, audit  # noqa: E402

CFG = config.load(None)
PARAMS = {"w": "40150", "ws": "2610001", "pc": "2XITGA"}


class Sperrliste(unittest.TestCase):
    def test_ganzwort(self):
        self.assertIsNone(keywords.blocked_hit("wir gehen im schlafzimmer weiter", ["im schlaf!"]))
        self.assertEqual(keywords.blocked_hit("sie liegt im schlaf da", ["im schlaf!"]), "im schlaf")

    def test_wortanfang(self):
        self.assertEqual(keywords.blocked_hit("eine teenie story", ["teen"]), "teen")
        self.assertIsNone(keywords.blocked_hit("kantine am see", ["teen"]))

    def test_video_sperrliste_enthaelt_kernbegriffe(self):
        terms = videos.video_blocked(CFG)
        for t in ("teen", "schülerin", "nachhilfe", "heimlich gefilmt", "vergewalt"):
            self.assertIn(t, terms)


class Affiliate(unittest.TestCase):
    def test_deeplink_hat_partnerparameter(self):
        q = parse_qs(urlparse(videos.deeplink(CFG, "/de/videos/123-test.html")).query)
        for k, v in PARAMS.items():
            self.assertEqual(q.get(k, [None])[0], v, k)

    def test_konfigurierte_visitx_links(self):
        def walk(x):
            if isinstance(x, dict):
                for v in x.values():
                    yield from walk(v)
            elif isinstance(x, list):
                for v in x:
                    yield from walk(v)
            elif isinstance(x, str) and x.startswith("http") and "visit-x" in x:
                yield x
        links = list(walk(CFG.get("affiliate", {})))
        self.assertTrue(links)
        for l in links:
            q = parse_qs(urlparse(l).query)
            for k, v in PARAMS.items():
                self.assertEqual(q.get(k, [None])[0], v, f"{l}: {k}")


class Header(unittest.TestCase):
    def test_csp_mit_hash(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "_headers"
            p.write_text("/*\n  X-Frame-Options: DENY\n", encoding="utf-8")
            render._security_headers({"site": {"ga_id": "G-TEST"}}, Path(d))
            t = p.read_text(encoding="utf-8")
            self.assertIn("Content-Security-Policy:", t)
            self.assertIn("'sha256-", t)
            self.assertIn("Strict-Transport-Security", t)
            render._security_headers({"site": {"ga_id": "G-TEST"}}, Path(d))
            self.assertEqual(p.read_text(encoding="utf-8").count("Content-Security-Policy"), 1)


class AuditParser(unittest.TestCase):
    def test_hauptinhalt_ohne_bedienelemente(self):
        p = audit._Page()
        p.feed('<html lang="de"><head><title>T</title><meta name="rating" content="adult"><meta name="RATING" content="RTA">'
               '<link rel="canonical" href="https://x/"></head><body><header><a href="/">Home</a></header><main>'
               '<h1>Titel</h1><p>Eigener Text hier.</p><div class="vcta"><a class="btn" href="https://visit-x.net/?w=1" '
               'rel="sponsored">Jetzt ansehen</a><p class="paynote">Bezahlen mit PayPal</p></div></main></body></html>')
        main = " ".join(p.text_main)
        self.assertIn("Eigener Text", main)
        self.assertNotIn("PayPal", main)
        self.assertNotIn("Jetzt ansehen", main)
        self.assertEqual(p.meta["rating"], "adult")
        self.assertEqual(p.canonical, "https://x/")
        self.assertEqual(p.buttons, 1)


if __name__ == "__main__":
    unittest.main()
