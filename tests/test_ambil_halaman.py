"""Pengujian unit tool ``ambil_halaman`` tanpa akses internet.

Jalankan dari root proyek::

    python -m unittest discover -s tests -p "test_ambil_halaman.py" -v
"""

import email.message
import inspect
import json
import socket
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tools import cari_uir
from tools import ambil_halaman as modul


URL = "https://uir.ac.id/informasi/beasiswa/"
ROBOTS_IZINKAN = "User-agent: *\nDisallow: /wp-admin/\n"


class _ResponsHTTP:
    """Tiruan respons yang kompatibel dengan mesin redirect urllib."""

    def __init__(self, isi=b"", kode=200, lokasi=None):
        self.code = kode
        self.status = kode
        self.msg = "OK"
        self.headers = email.message.Message()
        if lokasi:
            self.headers["Location"] = lokasi
        if isinstance(isi, (dict, list)):
            isi = json.dumps(isi)
        self._isi = isi.encode("utf-8") if isinstance(isi, str) else isi

    def info(self):
        return self.headers

    def getcode(self):
        return self.code

    def read(self):
        return self._isi

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class UjiAmbilHalaman(unittest.TestCase):
    """Uji ekstraksi dan validasi dengan ``_http_get``/``_cek_robots`` di-mock."""

    def setUp(self):
        self.konten = b""
        self.panggil = []

        patcher_http = mock.patch.object(modul, "_http_get")
        patcher_robots = mock.patch.object(modul, "_cek_robots")
        self.http_mock = patcher_http.start()
        self.robots_mock = patcher_robots.start()
        self.addCleanup(patcher_http.stop)
        self.addCleanup(patcher_robots.stop)

        def _http(url):
            self.panggil.append(("http", url))
            isi = self.konten
            return isi.encode("utf-8") if isinstance(isi, str) else isi

        def _robots(url):
            self.panggil.append(("robots", url))

        self.http_mock.side_effect = _http
        self.robots_mock.side_effect = _robots

    def _ambil(self, url=URL):
        self.konten = (
            "<html><head><title>Beasiswa UIR</title></head><body>"
            "<article><p>Informasi beasiswa resmi UIR.</p></article>"
            "</body></html>"
        )
        return modul.ambil_halaman(url)

    # ------------------------------------------------------------------
    # 1. URL valid berhasil diambil
    # ------------------------------------------------------------------
    def test_url_valid_berhasil(self):
        hasil = self._ambil()
        self.assertEqual(hasil["status"], "berhasil")
        self.assertTrue(hasil["ok"])
        self.assertEqual(hasil["url"], URL)
        self.assertEqual(hasil["pesan"], "Berhasil mengambil halaman publik uir.ac.id.")
        self.http_mock.assert_called_once_with(URL)

    # ------------------------------------------------------------------
    # 2. Judul diekstrak
    # ------------------------------------------------------------------
    def test_judul_terekstrak(self):
        hasil = self._ambil()
        self.assertEqual(hasil["judul"], "Beasiswa UIR")

    # ------------------------------------------------------------------
    # 3. Isi <article> diekstrak
    # ------------------------------------------------------------------
    def test_isi_article_terekstrak(self):
        hasil = self._ambil()
        self.assertIn("Informasi beasiswa resmi UIR.", hasil["teks"])

    # ------------------------------------------------------------------
    # 4. Isi <main> diekstrak
    # ------------------------------------------------------------------
    def test_isi_main_terekstrak(self):
        self.konten = (
            "<html><head><title>Profil</title></head><body>"
            "<main><h1>Profil UIR</h1><p>Universitas Islam Riau.</p></main>"
            "</body></html>"
        )
        hasil = modul.ambil_halaman(URL)
        self.assertIn("Profil UIR", hasil["teks"])
        self.assertIn("Universitas Islam Riau.", hasil["teks"])

    # ------------------------------------------------------------------
    # 5. Fallback bila <main>/<article> tidak ada
    # ------------------------------------------------------------------
    def test_fallback_tanpa_main_article(self):
        self.konten = (
            "<html><head><title>Fallback</title></head><body>"
            "<div id='content'><p>Teks dari isi halaman.</p></div>"
            "</body></html>"
        )
        hasil = modul.ambil_halaman(URL)
        self.assertEqual(hasil["status"], "berhasil")
        self.assertIn("Teks dari isi halaman.", hasil["teks"])

    # ------------------------------------------------------------------
    # 6. Script dan style dibuang
    # ------------------------------------------------------------------
    def test_script_dan_style_dibuang(self):
        self.konten = (
            "<html><head><title>Bersih</title>"
            "<script>var rahasia = 'RAHASIA_SCRIPT';</script>"
            "<style>.x { content: 'RAHASIA_STYLE'; }</style>"
            "</head><body><article><p>Teks sahaja.</p></article></body></html>"
        )
        hasil = modul.ambil_halaman(URL)
        self.assertIn("Teks sahaja.", hasil["teks"])
        self.assertNotIn("RAHASIA_SCRIPT", hasil["teks"])
        self.assertNotIn("RAHASIA_STYLE", hasil["teks"])
        self.assertNotIn("var rahasia", hasil["teks"])

    # ------------------------------------------------------------------
    # 7. Navigasi, footer, dan form dibuang
    # ------------------------------------------------------------------
    def test_nav_footer_form_dibuang(self):
        self.konten = (
            "<html><head><title>Bersih</title></head><body>"
            "<nav>MENU_NAVIGASI</nav><header>HEADER_NAV</header>"
            "<article><p>Isi utama.</p></article>"
            "<footer>FOOTER_HALAMAN</footer>"
            "<form><input value='FORM_INPUT'>FORM_LABEL</form>"
            "</body></html>"
        )
        hasil = modul.ambil_halaman(URL)
        self.assertEqual(hasil["teks"], "Isi utama.")
        for penanda in ("MENU_NAVIGASI", "HEADER_NAV", "FOOTER_HALAMAN", "FORM_LABEL"):
            self.assertNotIn(penanda, hasil["teks"])

    # ------------------------------------------------------------------
    # 8. Entity HTML didekode
    # ------------------------------------------------------------------
    def test_entity_html_didekode(self):
        self.konten = (
            "<html><head><title>Beasiswa &amp; UIR</title></head><body>"
            "<article><p>A &amp; B &lt; C &nbsp; D &#39;E&#39;.</p></article>"
            "</body></html>"
        )
        hasil = modul.ambil_halaman(URL)
        self.assertEqual(hasil["judul"], "Beasiswa & UIR")
        self.assertIn("A & B < C D 'E'.", hasil["teks"])
        self.assertNotIn("&amp;", hasil["teks"])
        self.assertNotIn("&nbsp;", hasil["teks"])

    # ------------------------------------------------------------------
    # 9-12. Validasi URL ditolak
    # ------------------------------------------------------------------
    def test_domain_luar_ditolak(self):
        hasil = modul.ambil_halaman("https://evil.example/halaman/")
        self.assertEqual(hasil["status"], "validasi_gagal")
        self.assertFalse(hasil["ok"])
        self.assertEqual(hasil["teks"], "")
        self.http_mock.assert_not_called()

    def test_http_ditolak(self):
        hasil = modul.ambil_halaman("http://uir.ac.id/halaman/")
        self.assertEqual(hasil["status"], "validasi_gagal")
        self.http_mock.assert_not_called()

    def test_subdomain_ditolak(self):
        for url in (
            "https://www.uir.ac.id/halaman/",
            "https://mahasiswa.uir.ac.id/halaman/",
            "https://uir.ac.id.evil.example/halaman/",
        ):
            hasil = modul.ambil_halaman(url)
            self.assertEqual(hasil["status"], "validasi_gagal", url)
        self.http_mock.assert_not_called()

    def test_port_tidak_diizinkan_ditolak(self):
        hasil = modul.ambil_halaman("https://uir.ac.id:8443/halaman/")
        self.assertEqual(hasil["status"], "validasi_gagal")
        self.http_mock.assert_not_called()

    # ------------------------------------------------------------------
    # 13. robots.txt melarang
    # ------------------------------------------------------------------
    def test_diblokir_robots(self):
        self.robots_mock.side_effect = modul._DiblokirRobots(
            "robots.txt melarang akses ke " + URL
        )
        hasil = modul.ambil_halaman(URL)
        self.assertEqual(hasil["status"], "diblokir_robots")
        self.assertFalse(hasil["ok"])
        self.assertIn("melarang", hasil["pesan"])
        self.http_mock.assert_not_called()

    # ------------------------------------------------------------------
    # 16. Konten kosong
    # ------------------------------------------------------------------
    def test_konten_kosong(self):
        self.konten = (
            "<html><head><title>Halaman Kosong</title></head>"
            "<body><script>console.log('x')</script></body></html>"
        )
        hasil = modul.ambil_halaman(URL)
        self.assertEqual(hasil["status"], "konten_kosong")
        self.assertEqual(hasil["judul"], "Halaman Kosong")
        self.assertEqual(hasil["teks"], "")
        self.assertEqual(hasil["panjang"], 0)
        self.assertFalse(hasil["ok"])

    # ------------------------------------------------------------------
    # 17. Timeout tidak bocor
    # ------------------------------------------------------------------
    def test_timeout_tidak_bocor(self):
        self.http_mock.side_effect = modul._SitusTidakTersedia(
            "Gagal menghubungi situs UIR: timed out"
        )
        hasil = modul.ambil_halaman(URL)
        self.assertEqual(hasil["status"], "situs_tidak_tersedia")
        self.assertFalse(hasil["ok"])
        self.assertIn("timed out", hasil["pesan"])
        self.assertEqual(hasil["teks"], "")

    # ------------------------------------------------------------------
    # 18. json.dumps
    # ------------------------------------------------------------------
    def test_hasil_dapat_diserialisasi(self):
        hasil = self._ambil()
        teks_json = json.dumps(hasil, ensure_ascii=False)
        self.assertIsInstance(teks_json, str)
        self.assertEqual(json.loads(teks_json)["status"], "berhasil")

    # ------------------------------------------------------------------
    # 19. Panjang dibatasi konstanta
    # ------------------------------------------------------------------
    def test_panjang_dibatasi(self):
        self.konten = (
            "<html><head><title>Panjang</title></head><body><article><p>"
            + "kata " * 5000
            + "</p></article></body></html>"
        )
        hasil = modul.ambil_halaman(URL)
        self.assertEqual(hasil["status"], "berhasil")
        self.assertTrue(hasil["teks"].endswith("…"))
        self.assertLessEqual(len(hasil["teks"]), modul.BATAS_TEKS + 1)
        self.assertEqual(hasil["panjang"], len(hasil["teks"]))

    # ------------------------------------------------------------------
    # 20. Satu parameter
    # ------------------------------------------------------------------
    def test_fungsi_satu_parameter(self):
        tanda_tangan = inspect.signature(modul.ambil_halaman)
        self.assertEqual(list(tanda_tangan.parameters), ["url"])
        parameter = tanda_tangan.parameters["url"]
        self.assertEqual(parameter.default, inspect.Parameter.empty)
        self.assertFalse(any(
            p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
            for p in tanda_tangan.parameters.values()
        ))

    # ------------------------------------------------------------------
    # Tambahan: urutan robots sebelum HTTP
    # ------------------------------------------------------------------
    def test_cek_robots_sebelum_http(self):
        self._ambil()
        self.assertEqual(
            self.panggil,
            [("robots", URL), ("http", URL)],
        )

    # ------------------------------------------------------------------
    # Tambahan: username/password ditolak
    # ------------------------------------------------------------------
    def test_username_password_ditolak(self):
        hasil = modul.ambil_halaman("https://user:sandi@uir.ac.id/halaman/")
        self.assertEqual(hasil["status"], "validasi_gagal")
        self.http_mock.assert_not_called()

    # ------------------------------------------------------------------
    # Tambahan: tidak memuat aset eksternal
    # ------------------------------------------------------------------
    def test_tidak_mengambil_aset_eksternal(self):
        self.konten = (
            "<html><head><title>Aset</title>"
            "<link rel='stylesheet' href='https://cdn.example/style.css'>"
            "<script src='https://cdn.example/app.js'></script>"
            "</head><body><article>"
            "<p>Teks saja.</p>"
            "<img src='https://cdn.example/gambar.png'>"
            "<iframe src='https://evil.example/frame'></iframe>"
            "</article></body></html>"
        )
        hasil = modul.ambil_halaman(URL)
        self.assertEqual(hasil["status"], "berhasil")
        self.assertEqual(hasil["teks"], "Teks saja.")
        self.http_mock.assert_called_once_with(URL)
        self.assertEqual(len(self.panggil), 2)  # robots + satu halaman

    # ------------------------------------------------------------------
    # Tambahan: elemen tersembunyi dibuang
    # ------------------------------------------------------------------
    def test_elemen_tersembunyi_dibuang(self):
        self.konten = (
            "<html><head><title>Tersembunyi</title></head><body><article>"
            "<p>Terlihat.</p>"
            "<div style='display: none'>RAHASIA_DISPLAY</div>"
            "<span hidden>RAHASIA_HIDDEN</span>"
            "<b aria-hidden='true'>RAHASIA_ARIA</b>"
            "</article></body></html>"
        )
        hasil = modul.ambil_halaman(URL)
        self.assertEqual(hasil["teks"], "Terlihat.")
        for penanda in ("RAHASIA_DISPLAY", "RAHASIA_HIDDEN", "RAHASIA_ARIA"):
            self.assertNotIn(penanda, hasil["teks"])

    # ------------------------------------------------------------------
    # Tambahan: kelas aksesibilitas tersembunyi dibuang
    # ------------------------------------------------------------------
    def test_kelas_tersembunyi_dibuang(self):
        self.konten = (
            "<html><head><title>Uji</title></head><body><article>"
            "<a class='skip-link screen-reader-text'>Lewati ke konten</a>"
            "<p>Isi tampak.</p></article></body></html>"
        )
        hasil = modul.ambil_halaman(URL)
        self.assertEqual(hasil["teks"], "Isi tampak.")

    # ------------------------------------------------------------------
    # Tambahan: tag tidak seimbang tidak boleh menghilangkan konten
    # ------------------------------------------------------------------
    def test_tag_tidak_seimbang_tidak_menghilangkan_konten(self):
        self.konten = (
            "<html><head><title>Uji</title></head><body>"
            "<nav>Menu <div>Submenu</nav>"
            "<article><p>Konten utama.</p></article>"
            "</body></html>"
        )
        hasil = modul.ambil_halaman(URL)
        self.assertEqual(hasil["status"], "berhasil")
        self.assertIn("Konten utama.", hasil["teks"])
        self.assertNotIn("Submenu", hasil["teks"])

    # ------------------------------------------------------------------
    # Tambahan: <article> kecil (kartu terkait) kalah dari fallback
    # ------------------------------------------------------------------
    def test_article_kecil_tidak_mengalahkan_fallback(self):
        self.konten = (
            "<html><head><title>Uji</title></head><body>"
            "<article>Kartu terkait.</article>"
            "<div><p>" + "Isi utama yang panjang. " * 20 + "</p></div>"
            "</body></html>"
        )
        hasil = modul.ambil_halaman(URL)
        self.assertEqual(hasil["status"], "berhasil")
        self.assertIn("Isi utama yang panjang.", hasil["teks"])
        self.assertIn("Kartu terkait.", hasil["teks"])

    # ------------------------------------------------------------------
    # Tambahan: input bukan teks / exception tak terduga tidak bocor
    # ------------------------------------------------------------------
    def test_input_non_teks_ditolak(self):
        for nilai in (None, 123, [], {}):
            hasil = modul.ambil_halaman(nilai)
            self.assertEqual(hasil["status"], "validasi_gagal", str(nilai))
        self.http_mock.assert_not_called()

    def test_exception_tak_terduga_tidak_bocor(self):
        self.http_mock.side_effect = ValueError("parsing gagal")
        hasil = modul.ambil_halaman(URL)
        self.assertEqual(hasil["status"], "situs_tidak_tersedia")
        self.assertFalse(hasil["ok"])


class UjiRedirectAmbilHalaman(unittest.TestCase):
    """Uji redirect memakai _http_get dan _RedirectAman asli dari Tool 1."""

    def setUp(self):
        self.panggil = []
        patcher_cache = mock.patch.object(cari_uir, "_ROBOTS_CACHE", None)
        patcher_penjaga = mock.patch.object(cari_uir, "_ROBOTS_SEDANG_DIAMBIL", False)
        patcher_cache.start()
        patcher_penjaga.start()
        self.addCleanup(patcher_cache.stop)
        self.addCleanup(patcher_penjaga.stop)

    def _pasang(self, peta):
        def _buka(_handler, http_class, req, **kwargs):
            url = req.full_url
            self.panggil.append(url)
            for kunci, nilai in peta.items():
                if kunci in url:
                    if isinstance(nilai, Exception):
                        raise nilai
                    if isinstance(nilai, dict) and "lokasi" in nilai:
                        return _ResponsHTTP(
                            kode=nilai.get("status", 302), lokasi=nilai["lokasi"]
                        )
                    return _ResponsHTTP(isi=nilai)
            raise AssertionError("URL tidak terduga dalam pengujian: " + url)

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), cari_uir._RedirectAman()
        )
        return (
            mock.patch.object(cari_uir, "_OPENER", opener),
            mock.patch.object(
                urllib.request.AbstractHTTPHandler, "do_open", new=_buka
            ),
        )

    # ------------------------------------------------------------------
    # 14. Redirect ke domain luar ditolak
    # ------------------------------------------------------------------
    def test_redirect_eksternal_ditolak(self):
        url = "https://uir.ac.id/halaman-redirect/"
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "halaman-redirect/": {
                "status": 302, "lokasi": "https://evil.example/collect",
            },
        }
        patch_opener, patch_do = self._pasang(peta)
        with patch_opener, patch_do:
            hasil = modul.ambil_halaman(url)

        self.assertEqual(hasil["status"], "situs_tidak_tersedia")
        self.assertEqual(hasil["teks"], "")
        self.assertFalse(any("evil.example" in alamat for alamat in self.panggil))

    # ------------------------------------------------------------------
    # 15. Redirect ke HTTP ditolak
    # ------------------------------------------------------------------
    def test_redirect_http_ditolak(self):
        url = "https://uir.ac.id/halaman-redirect/"
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "halaman-redirect/": {
                "status": 301,
                "lokasi": "http://uir.ac.id/halaman-tujuan/",
            },
        }
        patch_opener, patch_do = self._pasang(peta)
        with patch_opener, patch_do:
            hasil = modul.ambil_halaman(url)

        self.assertEqual(hasil["status"], "situs_tidak_tersedia")
        self.assertFalse(any(a.startswith("http://") for a in self.panggil))

    # ------------------------------------------------------------------
    # Tambahan: redirect internal aman tetap diikuti
    # ------------------------------------------------------------------
    def test_redirect_internal_aman_diikuti(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "/halaman-awal/": {
                "status": 302,
                "lokasi": "https://uir.ac.id/halaman-tujuan/",
            },
            "/halaman-tujuan/": (
                "<html><head><title>Tujuan Aman</title></head><body>"
                "<article><p>Isi halaman tujuan.</p></article></body></html>"
            ),
        }
        patch_opener, patch_do = self._pasang(peta)
        with patch_opener, patch_do:
            hasil = modul.ambil_halaman("https://uir.ac.id/halaman-awal/")

        self.assertEqual(hasil["status"], "berhasil")
        self.assertEqual(hasil["judul"], "Tujuan Aman")
        self.assertEqual(hasil["teks"], "Isi halaman tujuan.")
        self.assertTrue(any("/halaman-tujuan/" in a for a in self.panggil))


if __name__ == "__main__":
    unittest.main()
