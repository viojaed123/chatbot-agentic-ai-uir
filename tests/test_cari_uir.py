"""Pengujian unit untuk tool ``cari_uir`` tanpa akses internet.

Jalankan dari root proyek::

    python -m unittest discover -s tests -p "test_*.py" -v
"""

import email.message
import json
import socket
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tools import cari_uir as modul


ROBOTS_IZINKAN = "User-agent: *\nDisallow: /wp-admin/\n"
ROBOTS_BLOKIR = "User-agent: *\nDisallow: /\n"

INDEKS_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc><![CDATA[https://uir.ac.id/page-sitemap.xml]]></loc></sitemap>
</sitemapindex>"""

HALAMAN_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
  <url>
    <loc><![CDATA[https://uir.ac.id/informasi-beasiswa/]]></loc>
    <lastmod><![CDATA[2026-07-16T06:47:59+00:00]]></lastmod>
    <image:image>
      <image:loc><![CDATA[https://dummy.webuir.com/placeholder.png]]></image:loc>
    </image:image>
  </url>
</urlset>"""

SITEMAP_FAKULTAS = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc><![CDATA[https://uir.ac.id/fakultas/]]></loc></url>
  <url><loc><![CDATA[https://uir.ac.id/akademik/daftar-fakultas-uir/]]></loc></url>
</urlset>"""

INDEKS_SITEMAP_KATALOG = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc><![CDATA[https://uir.ac.id/page-sitemap.xml]]></loc></sitemap>
  <sitemap><loc><![CDATA[https://uir.ac.id/post-sitemap.xml]]></loc></sitemap>
</sitemapindex>"""

SITEMAP_HALAMAN_KATALOG = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc><![CDATA[https://uir.ac.id/penerimaan-mahasiswa-baru/]]></loc></url>
  <url><loc><![CDATA[https://uir.ac.id/program-pendidikan/]]></loc></url>
</urlset>"""

SITEMAP_POST_BERITA = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc><![CDATA[https://uir.ac.id/apel-akbar-fakultas/]]></loc></url>
</urlset>"""


def _respons(isi, kode=200):
    """Buat tiruan objek respons urlopen yang mendukung context manager."""
    if isinstance(isi, (dict, list)):
        isi = json.dumps(isi)
    if isinstance(isi, str):
        isi = isi.encode("utf-8")

    respons = mock.MagicMock()
    respons.__enter__.return_value = respons
    respons.__exit__.return_value = False
    respons.read.return_value = isi
    respons.getcode.return_value = kode
    respons.status = kode
    return respons


class _ResponsRedirect:
    """Tiruan respons yang kompatibel dengan mesin redirect urllib.

    ``HTTPErrorProcessor`` membaca ``code``, ``msg``, dan ``info()``;
    ``HTTPRedirectHandler`` membaca header ``location`` lalu ``read()``/``close()``.
    """

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


class UjiCariUIR(unittest.TestCase):
    def setUp(self):
        self.panggil = []

        self._cache_robots = mock.patch.object(modul, "_ROBOTS_CACHE", None)
        self._cache_sitemap = mock.patch.object(modul, "_SITEMAP_CACHE", None)
        self._cache_hasil = mock.patch.object(modul, "_HASIL_CACHE", {})
        self._penjaga = mock.patch.object(modul, "_ROBOTS_SEDANG_DIAMBIL", False)
        self._tidur = mock.patch("time.sleep", return_value=None)
        self._cache_robots.start()
        self._cache_sitemap.start()
        self._cache_hasil.start()
        self._penjaga.start()
        self.sleep_mock = self._tidur.start()

        self.addCleanup(self._cache_robots.stop)
        self.addCleanup(self._cache_sitemap.stop)
        self.addCleanup(self._cache_hasil.stop)
        self.addCleanup(self._penjaga.stop)
        self.addCleanup(self._tidur.stop)

    def _pasang_urlopen(self, peta):
        """Pasang opener tiruan; ``peta`` memetakan potongan URL ke isi/Exception."""
        def _buka(request, timeout=None):
            url = request.full_url if hasattr(request, "full_url") else str(request)
            self.panggil.append((url, timeout))
            for kunci, nilai in peta.items():
                if kunci in url:
                    if isinstance(nilai, Exception):
                        raise nilai
                    return _respons(nilai)
            raise AssertionError("URL tidak terduga dalam pengujian: " + url)

        return mock.patch.object(modul._OPENER, "open", side_effect=_buka)

    def _pasang_redirect(self, peta):
        """Pasang opener tanpa proxy + do_open tiruan agar handler asli berjalan.

        ``peta`` memetakan potongan URL ke isi final, Exception, atau
        ``{"status": 3xx, "lokasi": ...}`` untuk respons redirect.
        """
        def _buka(_handler, http_class, req, **kwargs):
            url = req.full_url
            self.panggil.append((url, getattr(req, "timeout", None)))
            for kunci, nilai in peta.items():
                if kunci in url:
                    if isinstance(nilai, Exception):
                        raise nilai
                    if isinstance(nilai, dict) and "lokasi" in nilai:
                        return _ResponsRedirect(
                            kode=nilai.get("status", 302), lokasi=nilai["lokasi"]
                        )
                    return _ResponsRedirect(isi=nilai)
            raise AssertionError("URL tidak terduga dalam pengujian: " + url)

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), modul._RedirectAman()
        )
        return (
            mock.patch.object(modul, "_OPENER", opener),
            mock.patch.object(
                modul.urllib.request.AbstractHTTPHandler, "do_open", new=_buka
            ),
        )

    # ------------------------------------------------------------------
    # 1. Hasil terstruktur + pembersihan HTML
    # ------------------------------------------------------------------
    def test_hasil_terstruktur_dan_html_bersih(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": [{
                "title": "<b>Beasiswa</b> &amp; UIR",
                "url": "https://uir.ac.id/beasiswa/",
                "subtype": "post",
                "id": 1,
            }],
            "/wp-json/wp/v2/posts/": {
                "excerpt": {"rendered": "<p>Info <strong>beasiswa</strong> resmi UIR.</p>"},
                "aioseo_head_json": {"description": "Cadangan deskripsi."},
                "modified": "2026-09-10T08:29:10",
            },
        }

        with self._pasang_urlopen(peta):
            hasil = modul.cari_uir("beasiswa")

        self.assertEqual(hasil["status"], "ditemukan")
        self.assertTrue(hasil["ok"])
        self.assertEqual(hasil["count"], 1)
        self.assertEqual(hasil["sumber"], "wp_rest")
        self.assertIn("status", hasil)
        self.assertIn("results", hasil)
        json.dumps(hasil)  # harus serializable

        item = hasil["results"][0]
        self.assertEqual(item["judul"], "Beasiswa & UIR")
        self.assertEqual(item["url"], "https://uir.ac.id/beasiswa/")
        self.assertEqual(item["cuplikan"], "Info beasiswa resmi UIR.")
        self.assertEqual(item["sumber_cuplikan"], "excerpt")
        self.assertTrue(all(timeout == 10 for _, timeout in self.panggil))

    # ------------------------------------------------------------------
    # 2. URL berbahaya / di luar domain
    # ------------------------------------------------------------------
    def test_url_berbahaya_ditolak(self):
        berbahaya = [
            "https://uir.ac.id.evil.com/halaman/",
            "http://uir.ac.id/halaman/",
            "https://evil.com/?q=uir.ac.id",
            "https://user@uir.ac.id/halaman/",
            "https://uir.ac.id:8443/halaman/",
            "https://127.0.0.1/halaman/",
            "https://uir.ac.id./halaman/",
            "//uir.ac.id/halaman/",
            "javascript:alert(1)",
        ]
        for url in berbahaya:
            self.assertIsNone(modul._validasi_url(url), url)

        self.assertEqual(
            modul._validasi_url("https://uir.ac.id/profil/"),
            "https://uir.ac.id/profil/",
        )

        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": [
                {"title": "Hasil berbahaya", "url": url, "subtype": "post", "id": 1}
                for url in berbahaya
            ],
        }
        with self._pasang_urlopen(peta):
            hasil = modul.cari_uir("uir")

        self.assertEqual(hasil["status"], "tidak_ditemukan")
        self.assertEqual(hasil["results"], [])

    # ------------------------------------------------------------------
    # 3. Hasil kosong
    # ------------------------------------------------------------------
    def test_kata_kunci_tidak_ditemukan(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": [],
        }
        with self._pasang_urlopen(peta):
            hasil = modul.cari_uir("xyzqwerty_tidakada_9988")

        self.assertEqual(hasil["status"], "tidak_ditemukan")
        self.assertEqual(hasil["count"], 0)
        self.assertEqual(hasil["results"], [])
        self.assertIn("Tidak ditemukan", hasil["pesan"])
        self.assertEqual(len(self.panggil), 2)  # robots + pencarian, tanpa detail

    # ------------------------------------------------------------------
    # 4. Situs tidak tersedia (URLError)
    # ------------------------------------------------------------------
    def test_situs_tidak_tersedia(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": urllib.error.URLError("network down"),
            "/sitemap.xml": urllib.error.URLError("network down"),
        }
        with self._pasang_urlopen(peta):
            hasil = modul.cari_uir("beasiswa")

        self.assertEqual(hasil["status"], "situs_tidak_tersedia")
        self.assertFalse(hasil["ok"])
        self.assertEqual(hasil["results"], [])
        self.assertTrue(hasil["pesan"])

    # ------------------------------------------------------------------
    # 5. robots.txt
    # ------------------------------------------------------------------
    def test_robots_melarang_akses(self):
        with self._pasang_urlopen({"robots.txt": ROBOTS_BLOKIR}):
            hasil = modul.cari_uir("beasiswa")

        self.assertEqual(hasil["status"], "diblokir_robots")
        self.assertFalse(hasil["ok"])
        self.assertEqual(hasil["results"], [])
        self.assertEqual(len(self.panggil), 1)  # hanya robots.txt yang diambil

    def test_robots_diperiksa_sebelum_detail(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": [{
                "title": "Beasiswa UIR",
                "url": "https://uir.ac.id/beasiswa/",
                "subtype": "post",
                "id": 1,
            }],
            "/wp-json/wp/v2/posts/": urllib.error.URLError("detail gagal"),
        }
        with self._pasang_urlopen(peta):
            hasil = modul.cari_uir("beasiswa")

        self.assertEqual(hasil["status"], "ditemukan")
        urls = [url for url, _ in self.panggil]
        self.assertTrue(any("robots.txt" in url for url in urls))
        self.assertTrue(any("/wp-json/wp/v2/posts/1" in url for url in urls))
        # Kegagalan detail ditangani: cuplikan jujur berasal dari judul.
        self.assertEqual(hasil["results"][0]["sumber_cuplikan"], "judul")

    # ------------------------------------------------------------------
    # 6. Timeout
    # ------------------------------------------------------------------
    def test_timeout_tidak_bocor(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": socket.timeout("timed out"),
            "/sitemap.xml": socket.timeout("timed out"),
        }
        with self._pasang_urlopen(peta):
            hasil = modul.cari_uir("beasiswa")

        self.assertEqual(hasil["status"], "situs_tidak_tersedia")
        self.assertEqual(hasil["results"], [])
        self.assertTrue(all(timeout == 10 for _, timeout in self.panggil))

    # ------------------------------------------------------------------
    # 7. HTTP 429 / 503 tanpa retry agresif
    # ------------------------------------------------------------------
    def test_http_429_503_tanpa_retry(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": urllib.error.HTTPError(
                "https://uir.ac.id/wp-json/wp/v2/search?search=beasiswa",
                503, "Service Unavailable", {}, None,
            ),
            "/sitemap.xml": urllib.error.HTTPError(
                "https://uir.ac.id/sitemap.xml",
                429, "Too Many Requests", {}, None,
            ),
        }
        with self._pasang_urlopen(peta):
            hasil = modul.cari_uir("beasiswa")

        self.assertEqual(hasil["status"], "situs_tidak_tersedia")
        # robots + REST + sitemap = 3 request; tidak ada retry tambahan.
        self.assertEqual(len(self.panggil), 3)

    # ------------------------------------------------------------------
    # 8. Fallback sitemap + cache memori
    # ------------------------------------------------------------------
    def test_fallback_sitemap_dan_cache(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": urllib.error.URLError("REST mati"),
            "/sitemap.xml": INDEKS_SITEMAP,
            "page-sitemap.xml": HALAMAN_SITEMAP,
        }
        with self._pasang_urlopen(peta):
            pertama = modul.cari_uir("beasiswa")
            jumlah_pertama = len(self.panggil)
            kedua = modul.cari_uir("beasiswa")
            jumlah_kedua = len(self.panggil)

        self.assertEqual(pertama["status"], "ditemukan")
        self.assertEqual(pertama["sumber"], "sitemap")
        self.assertEqual(pertama["count"], 1)
        item = pertama["results"][0]
        self.assertEqual(item["judul"], "Informasi beasiswa")
        self.assertEqual(item["url"], "https://uir.ac.id/informasi-beasiswa/")
        self.assertEqual(item["sumber_cuplikan"], "judul")
        self.assertEqual(item["cuplikan"], "Informasi beasiswa")

        # robots(1) + REST(1) + indeks(1) + anak(1) = 4 request pada pemanggilan pertama.
        self.assertEqual(jumlah_pertama, 4)
        # Pemanggilan kedua memakai cache hasil pencarian, tanpa request baru.
        self.assertEqual(jumlah_kedua, 4)
        self.assertTrue(kedua.get("dari_cache"))
        self.assertEqual(kedua["results"], pertama["results"])
        self.assertFalse(any(
            "/wp-json/wp/v2/posts/" in url or "/wp-json/wp/v2/pages/" in url
            for url, _ in self.panggil
        ))
        self.assertEqual(kedua["sumber"], "sitemap")

    def test_cache_hasil_kedaluwarsa(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": [{
                "title": "Informasi beasiswa",
                "url": "https://uir.ac.id/informasi-beasiswa/",
                "subtype": "page",
                "id": 1,
            }],
            "/wp-json/wp/v2/pages/": {
                "excerpt": {"rendered": "<p>Cuplikan resmi.</p>"}
            },
        }

        with self._pasang_urlopen(peta):
            with mock.patch.object(modul.time, "monotonic", return_value=1000):
                modul.cari_uir("beasiswa")
                kedua = modul.cari_uir("beasiswa")
                jumlah_sebelum = len(self.panggil)

            with mock.patch.object(
                modul.time,
                "monotonic",
                return_value=1000 + modul.HASIL_CACHE_TTL_DETIK + 1,
            ):
                ketiga = modul.cari_uir("beasiswa")

        self.assertTrue(kedua.get("dari_cache"))
        self.assertFalse(ketiga.get("dari_cache"))
        self.assertGreater(len(self.panggil), jumlah_sebelum)

    def test_hasil_gagal_tidak_disimpan_di_cache(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": urllib.error.URLError("REST mati"),
            "/sitemap.xml": urllib.error.URLError("sitemap mati"),
        }

        with self._pasang_urlopen(peta):
            pertama = modul.cari_uir("beasiswa")
            kedua = modul.cari_uir("beasiswa")

        self.assertFalse(kedua.get("dari_cache"))
        self.assertEqual(pertama["status"], "situs_tidak_tersedia")
        self.assertEqual(kedua["status"], "situs_tidak_tersedia")

    # ------------------------------------------------------------------
    # 9. Prioritas Bahasa Indonesia
    # ------------------------------------------------------------------
    def test_halaman_indonesia_diprioritaskan(self):
        kandidat = [
            {"title": "Our Profile", "url": "https://uir.ac.id/en/profile/",
             "subtype": "page", "id": 2},
            {"title": "Profil UIR", "url": "https://uir.ac.id/profil/",
             "subtype": "page", "id": 1},
        ]
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": kandidat,
            "/wp-json/wp/v2/pages/": {"excerpt": {"rendered": "<p>Cuplikan resmi.</p>"}},
        }
        with self._pasang_urlopen(peta):
            hasil = modul.cari_uir("profil")

        self.assertEqual(hasil["status"], "ditemukan")
        self.assertEqual(hasil["count"], 2)
        self.assertEqual(hasil["results"][0]["url"], "https://uir.ac.id/profil/")
        self.assertTrue(hasil["results"][1]["url"].startswith("https://uir.ac.id/en/"))

    # ------------------------------------------------------------------
    # 10. Jeda antar request detail
    # ------------------------------------------------------------------
    def test_jeda_antar_detail(self):
        kandidat = [
            {"title": "Judul %d" % i, "url": "https://uir.ac.id/halaman-%d/" % i,
             "subtype": "post", "id": i}
            for i in range(1, 4)
        ]
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": kandidat,
            "/wp-json/wp/v2/posts/": {"excerpt": {"rendered": "<p>Cuplikan resmi.</p>"}},
        }
        with self._pasang_urlopen(peta):
            hasil = modul.cari_uir("halaman")

        self.assertEqual(hasil["count"], 3)
        self.assertEqual(self.sleep_mock.call_count, 2)
        self.sleep_mock.assert_has_calls([mock.call(1.0), mock.call(1.0)])

    # ------------------------------------------------------------------
    # 11. Validasi input
    # ------------------------------------------------------------------
    def test_validasi_query(self):
        for nilai in ["", "   ", "a" * 201, 123, "baris\nbaru"]:
            hasil = modul.cari_uir(nilai)
            self.assertEqual(hasil["status"], "validasi_gagal", str(nilai)[:20])
            self.assertFalse(hasil["ok"])
        self.assertEqual(self.panggil, [])

    # ------------------------------------------------------------------
    # 12. Sumber cuplikan: meta description + pemotongan
    # ------------------------------------------------------------------
    def test_meta_description_dan_pemotongan(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": [{
                "title": "Informasi Beasiswa",
                "url": "https://uir.ac.id/informasi-beasiswa/",
                "subtype": "page",
                "id": 5,
            }],
            "/wp-json/wp/v2/pages/": {
                "excerpt": {"rendered": "   "},
                "aioseo_head_json": {"description": "Deskripsi meta resmi situs UIR."},
            },
        }
        with self._pasang_urlopen(peta):
            hasil = modul.cari_uir("beasiswa")

        item = hasil["results"][0]
        self.assertEqual(item["sumber_cuplikan"], "meta_description")
        self.assertEqual(item["cuplikan"], "Deskripsi meta resmi situs UIR.")

        panjang = " ".join(["kata"] * 100)
        item_panjang = modul._potong_teks(panjang, modul.PANJANG_CUPLIKAN)
        self.assertLessEqual(len(item_panjang), modul.PANJANG_CUPLIKAN + 1)
        self.assertTrue(item_panjang.endswith("…"))

    # ------------------------------------------------------------------
    # 13. Redirect ke domain eksternal ditolak
    # ------------------------------------------------------------------
    def test_redirect_eksternal_ditolak(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "per_page=5": {"status": 302, "lokasi": "https://evil.example/collect"},
            "/sitemap.xml": urllib.error.URLError("sitemap mati"),
        }
        patch_opener, patch_do = self._pasang_redirect(peta)
        with patch_opener, patch_do:
            with self.assertLogs(modul.LOGGER, level="WARNING") as log:
                hasil = modul.cari_uir("beasiswa")

        self.assertEqual(hasil["status"], "situs_tidak_tersedia")
        self.assertEqual(hasil["results"], [])
        self.assertFalse(any("evil.example" in url for url, _ in self.panggil))
        self.assertIn("Redirect ditolak", "\n".join(log.output))

    # ------------------------------------------------------------------
    # 14. Redirect ke http:// ditolak (skema harus tetap HTTPS)
    # ------------------------------------------------------------------
    def test_redirect_http_ditolak(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "per_page=5": {
                "status": 301,
                "lokasi": "http://uir.ac.id/wp-json/wp/v2/search?search=beasiswa",
            },
            "/sitemap.xml": urllib.error.URLError("sitemap mati"),
        }
        patch_opener, patch_do = self._pasang_redirect(peta)
        with patch_opener, patch_do:
            with self.assertLogs(modul.LOGGER, level="WARNING") as log:
                hasil = modul.cari_uir("beasiswa")

        self.assertEqual(hasil["status"], "situs_tidak_tersedia")
        self.assertFalse(any(url.startswith("http://") for url, _ in self.panggil))
        self.assertIn("Redirect ditolak", "\n".join(log.output))

    # ------------------------------------------------------------------
    # 15. Redirect internal yang aman tetap diikuti + robots dicek ulang
    # ------------------------------------------------------------------
    def test_redirect_internal_aman_diikuti(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "per_page=5": {
                "status": 302,
                "lokasi": "https://uir.ac.id/wp-json/wp/v2/search?search=redirect",
            },
            "search=redirect": [{
                "title": "Hasil Redirect Aman",
                "url": "https://uir.ac.id/hasil-redirect/",
                "subtype": "post",
                "id": 7,
            }],
            "/wp-json/wp/v2/posts/": {
                "excerpt": {"rendered": "<p>Cuplikan setelah redirect.</p>"},
            },
        }
        patch_opener, patch_do = self._pasang_redirect(peta)
        cek = mock.patch.object(modul, "_cek_robots", wraps=modul._cek_robots)
        with patch_opener, patch_do, cek as cek_mock:
            hasil = modul.cari_uir("beasiswa")

        self.assertEqual(hasil["status"], "ditemukan")
        self.assertEqual(hasil["sumber"], "wp_rest")
        self.assertEqual(hasil["count"], 1)
        item = hasil["results"][0]
        self.assertEqual(item["cuplikan"], "Cuplikan setelah redirect.")
        self.assertEqual(item["sumber_cuplikan"], "excerpt")
        self.assertTrue(any("search=redirect" in url for url, _ in self.panggil))
        cek_mock.assert_any_call(
            "https://uir.ac.id/wp-json/wp/v2/search?search=redirect"
        )

    # ------------------------------------------------------------------
    # 16. Redirect ke halaman yang dilarang robots.txt ditolak
    # ------------------------------------------------------------------
    def test_redirect_target_diblokir_robots(self):
        peta = {
            "robots.txt": "User-agent: *\nDisallow: /rahasia/\n",
            "per_page=5": {
                "status": 302,
                "lokasi": "https://uir.ac.id/rahasia/halaman/",
            },
            "/sitemap.xml": urllib.error.URLError("sitemap mati"),
        }
        patch_opener, patch_do = self._pasang_redirect(peta)
        with patch_opener, patch_do:
            hasil = modul.cari_uir("beasiswa")

        self.assertEqual(hasil["status"], "diblokir_robots")
        self.assertEqual(hasil["results"], [])
        self.assertFalse(any("/rahasia/halaman/" in url for url, _ in self.panggil))

    # ------------------------------------------------------------------
    # 17. Normalisasi pertanyaan daftar
    # ------------------------------------------------------------------
    def test_normalisasi_fakultas_dan_program_studi(self):
        self.assertEqual(
            modul._normalisasi_query(
                "Apa saja fakultas yang tersedia di Universitas Islam Riau?"
            ),
            "daftar fakultas",
        )
        self.assertEqual(
            modul._normalisasi_query(
                "Sebutkan seluruh fakultas Universitas Islam Riau."
            ),
            "daftar fakultas",
        )
        self.assertEqual(
            modul._normalisasi_query(
                "Apa saja program studi yang tersedia di UIR?"
            ),
            "daftar program studi",
        )
        self.assertEqual(
            modul._normalisasi_query(
                "Daftar program studi Universitas Islam Riau"
            ),
            "daftar program studi",
        )
        self.assertTrue(modul._adalah_katalog("daftar fakultas"))
        self.assertTrue(modul._adalah_katalog("daftar program studi"))
        self.assertFalse(modul._adalah_katalog("beasiswa"))

    # ------------------------------------------------------------------
    # 18. REST + sitemap digabungkan untuk katalog fakultas
    # ------------------------------------------------------------------
    def test_katalog_fakultas_menggabungkan_rest_dan_sitemap(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": [
                {
                    "title": "Berita fakultas baru di UIR",
                    "url": "https://uir.ac.id/berita/fakultas-baru/",
                    "subtype": "post",
                    "id": 11,
                },
                {
                    "title": "Daftar Fakultas",
                    "url": "https://uir.ac.id/fakultas/",
                    "subtype": "page",
                    "id": 12,
                },
            ],
            "/wp-json/wp/v2/posts/": {
                "excerpt": {"rendered": "<p>Berita kegiatan fakultas.</p>"}
            },
            "/wp-json/wp/v2/pages/": {
                "excerpt": {"rendered": "<p>Cuplikan katalog resmi.</p>"}
            },
            "/sitemap.xml": INDEKS_SITEMAP,
            "page-sitemap.xml": SITEMAP_FAKULTAS,
        }

        with self._pasang_urlopen(peta):
            hasil = modul.cari_uir(
                "Apa saja fakultas yang tersedia di Universitas Islam Riau?"
            )

        self.assertEqual(hasil["status"], "ditemukan")
        self.assertTrue(hasil["ok"])
        self.assertEqual(hasil["query"], "daftar fakultas")
        self.assertEqual(hasil["sumber"], "wp_rest+sitemap")
        self.assertEqual(hasil["count"], 3)

        urls = [item["url"] for item in hasil["results"]]
        self.assertEqual(len(urls), len(set(urls)))  # duplikat dihapus
        self.assertTrue(all(url.startswith("https://uir.ac.id/") for url in urls))

        katalog = "https://uir.ac.id/fakultas/"
        katalog_sitemap = "https://uir.ac.id/akademik/daftar-fakultas-uir/"
        berita = "https://uir.ac.id/berita/fakultas-baru/"

        # Dua halaman katalog (REST dan sitemap) menempati urutan teratas;
        # artikel berita tidak dianggap sebagai katalog resmi.
        self.assertEqual(set(urls[:2]), {katalog, katalog_sitemap})
        self.assertEqual(urls[-1], berita)
        self.assertTrue(all(item["terverifikasi_sitemap"] for item in hasil["results"][:2]))
        self.assertFalse(hasil["results"][-1]["terverifikasi_sitemap"])
        self.assertEqual(hasil["results"][-1]["jenis"], "post")

    # ------------------------------------------------------------------
    # 19. Katalog tetap memakai sitemap bila REST gagal
    # ------------------------------------------------------------------
    def test_katalog_fakultas_fallback_sitemap(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": urllib.error.URLError("REST mati"),
            "/sitemap.xml": INDEKS_SITEMAP,
            "page-sitemap.xml": SITEMAP_FAKULTAS,
        }

        with self._pasang_urlopen(peta):
            hasil = modul.cari_uir("Sebutkan seluruh fakultas Universitas Islam Riau.")

        self.assertEqual(hasil["status"], "ditemukan")
        self.assertEqual(hasil["sumber"], "sitemap")
        self.assertEqual(hasil["query"], "daftar fakultas")
        self.assertEqual(hasil["count"], 2)
        self.assertEqual(
            [item["url"] for item in hasil["results"]],
            [
                "https://uir.ac.id/akademik/daftar-fakultas-uir/",
                "https://uir.ac.id/fakultas/",
            ],
        )
        self.assertTrue(all(
            item["sumber_cuplikan"] == "judul"
            for item in hasil["results"]
        ))

    # ------------------------------------------------------------------
    # 20. Katalog resmi mengalahkan berita pada data sitemap nyata
    # ------------------------------------------------------------------
    def test_katalog_resmi_mengalahkan_artikel_berita(self):
        peta = {
            "robots.txt": ROBOTS_IZINKAN,
            "wp-json/wp/v2/search": [
                {
                    "title": "Berita fakultas baru",
                    "url": "https://uir.ac.id/berita/fakultas-baru/",
                    "subtype": "post",
                    "id": 11,
                },
            ],
            "/wp-json/wp/v2/posts/": {
                "excerpt": {"rendered": "<p>Berita kegiatan fakultas.</p>"}
            },
            "/sitemap.xml": INDEKS_SITEMAP_KATALOG,
            "page-sitemap.xml": SITEMAP_HALAMAN_KATALOG,
            "post-sitemap.xml": SITEMAP_POST_BERITA,
        }

        with self._pasang_urlopen(peta):
            hasil = modul.cari_uir(
                "Apa saja program studi yang tersedia di UIR?"
            )

        self.assertEqual(hasil["status"], "ditemukan")
        self.assertEqual(hasil["query"], "daftar program studi")
        self.assertEqual(hasil["sumber"], "wp_rest+sitemap")

        urls = [item["url"] for item in hasil["results"]]
        self.assertEqual(
            urls[0], "https://uir.ac.id/program-pendidikan/"
        )
        self.assertNotIn("https://uir.ac.id/apel-akbar-fakultas/", urls)
        self.assertTrue(all(
            "program-pendidikan" in url or "penerimaan-mahasiswa" in url
            or "berita" in url
            for url in urls
        ))


if __name__ == "__main__":
    unittest.main()
