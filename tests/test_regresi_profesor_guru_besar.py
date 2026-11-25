"""Regresi kasus Profesor/Guru Besar tanpa internet dan tanpa Ollama.

Pengujian ini memastikan pertanyaan "siapa profesor di teknik?" tidak lagi
berakhir dengan 0 langkah, serta nama/jabatan hanya berasal dari halaman
uir.ac.id yang berhasil dibaca. Tidak ada nama profesor yang di-hardcode;
semua nama pada fixture adalah data tiruan.
"""

import unittest
from unittest import mock

from app import orchestrator as modul
from app import perencana
from tools import cari_uir as pencarian


URL_PROFIL = "https://uir.ac.id/fakultas/teknik/profil-dosen/"
URL_BERITA = "https://uir.ac.id/berita/kunjungan-teknik/"
URL_TAMU = (
    "https://uir.ac.id/dosen-dari-universitas-luar-memberikan-kuliah-3-hari/"
)
URL_TANPA_BUKTI = "https://uir.ac.id/guru-besar/"

TEKS_PROFIL = """Fakultas Teknik

Profil Dosen

Prof. Dr. H. Andi Contoh, S.T., M.T.
Guru Besar Fakultas Teknik Universitas Islam Riau

Prof. Dr. H. Andi Contoh menceritakan kepada Tim Humas UIR.
Prof. H. Andi Contoh pun juga meraih penghargaan KOPERTIS X.
UIR Kukuhkan Prof. Dr. H. Andi Contoh Jadi Guru Besar Ke 1 Teknik Sipil

Prof. Dr. Rina Contoh, M.T.
Dosen Fakultas Teknik Universitas Islam Riau

Dr. Budi Salah, M.T.
Lektor Kepala Fakultas Teknik Universitas Islam Riau
"""

TEKS_TANPA_BUKTI = (
    "Halaman Guru Besar Universitas Islam Riau.\n"
    "Informasi daftar sedang disiapkan.\n"
)

PERTANYAAN = "siapa profesor di teknik?"


class KlienPalsu:
    """Klien Ollama tiruan dengan respons antrean."""

    def __init__(self, *respons):
        self.antrean = list(respons)
        self.panggilan = []

    def chat(self, messages, tools=None):
        self.panggilan.append({
            "messages": [dict(item) for item in messages],
            "tools": tools,
        })
        if not self.antrean:
            raise AssertionError("KlienPalsu kehabisan respons.")
        return self.antrean.pop(0)


def _respons(konten="Saya tidak dapat memastikan dari sumber."):
    return {
        "model": "hermes3:8b",
        "message": {
            "role": "assistant",
            "content": konten,
            "tool_calls": [],
        },
    }


def _hasil_cari(*item):
    return {
        "ok": True,
        "status": "ditemukan" if item else "tidak_ditemukan",
        "query": "profesor teknik",
        "count": len(item),
        "sumber": "wp_rest",
        "results": list(item),
        "pesan": "Ditemukan %d halaman." % len(item),
        "waktu": "2026-11-25T00:00:00+00:00",
    }


def _item_cari(url, judul, cuplikan):
    return {"judul": judul, "url": url, "cuplikan": cuplikan}


def _hasil_baca(url, judul, teks):
    return {
        "ok": True,
        "status": "ditemukan",
        "url": url,
        "data": {
            "url": url,
            "judul": judul,
            "teks": teks,
            "panjang": len(teks),
            "waktu": "2026-11-25T00:00:00+00:00",
        },
        "waktu_cache": "2026-11-25T00:00:00+00:00",
        "kedaluwarsa_pada": "2026-11-26T00:00:00+00:00",
        "pesan": "Cache ditemukan dan masih berlaku.",
    }


class UjiPemahamanProfesor(unittest.TestCase):
    def test_istilah_dan_tiga_makna_tidak_disamakan(self):
        variasi = {
            "Guru Besar Fakultas Teknik": "guru_besar_fakultas_teknik",
            "dosen teknik yang sudah menjadi profesor": (
                "dosen_profesor_fakultas_teknik"
            ),
            "profesor di lingkungan UIR": "profesor_lingkungan_uir",
            "Siapa saja Guru Besar Universitas Islam Riau?": "guru_besar_uir",
        }

        mode = {}
        for pertanyaan, diharapkan in variasi.items():
            info = perencana.pahami_dosen(pertanyaan)
            self.assertTrue(info["aktif"])
            self.assertEqual(info["mode"], diharapkan)
            mode[pertanyaan] = info["mode"]

        self.assertEqual(len(set(mode.values())), len(mode))

        info = perencana.pahami_dosen(PERTANYAAN)
        self.assertEqual(info["kategori"], "profesor")
        self.assertEqual(info["unit"], "Fakultas Teknik")
        self.assertIn("nama", info["atribut"])
        self.assertIn("jabatan", info["atribut"])

        jumlah = perencana.pahami_dosen(
            "Ada berapa profesor di Fakultas Teknik UIR?"
        )
        self.assertIn("jumlah", jumlah["atribut"])

    def test_pemetaan_teknik_bukan_katalog_fakultas(self):
        self.assertFalse(modul._adalah_pertanyaan_di_luar_cakupan(PERTANYAAN))
        self.assertEqual(modul._topic(PERTANYAAN), "")
        self.assertIn("dosen", perencana.topik_pertanyaan(PERTANYAAN))
        self.assertIn("dosen", modul._topik_pertanyaan(PERTANYAAN))
        self.assertTrue(perencana.pahami_dosen(PERTANYAAN)["teknik"])

        untuk_fakultas = "Siapa saja Guru Besar Fakultas Teknik?"
        self.assertEqual(modul._topic(untuk_fakultas), "")
        self.assertFalse(modul._butuh_katalog(untuk_fakultas))

    def test_multi_variasi_kata_kunci_dan_normalisasi(self):
        strategi = perencana.strategi_pencarian(PERTANYAAN, batas=10)
        subkueri = strategi["subkueri"]

        self.assertGreaterEqual(len(subkueri), 8)
        self.assertEqual(len(subkueri), len(set(subkueri)))
        self.assertTrue(strategi["butuh_sitemap"])
        for wajib in (
            'site:uir.ac.id "Guru Besar" "Teknik"',
            'site:uir.ac.id "Profesor" "Fakultas Teknik"',
            'site:uir.ac.id "Guru Besar" UIR',
            'site:uir.ac.id "Fakultas Teknik" dosen profesor',
            "site:uir.ac.id profesor Universitas Islam Riau",
        ):
            self.assertIn(wajib, subkueri)

        self.assertEqual(pencarian._urai_subkueri(PERTANYAAN), subkueri)
        self.assertEqual(
            pencarian._tanpa_site(subkueri[0]),
            "Profesor Fakultas Teknik",
        )
        self.assertNotEqual(
            pencarian._normalisasi_query(
                "Siapa profesor di Fakultas Teknik UIR?"
            ),
            "daftar fakultas",
        )

    def test_prioritas_profil_dosen_di_atas_berita(self):
        self.assertEqual(modul._prioritas_url_dosen(URL_PROFIL), 0)
        self.assertLess(
            modul._prioritas_url_dosen(URL_PROFIL),
            modul._prioritas_url_dosen(
                "https://uir.ac.id/fakultas/teknik/"
            ),
        )
        self.assertLess(
            modul._prioritas_url_dosen(URL_PROFIL),
            modul._prioritas_url_dosen(URL_BERITA),
        )

        item = _item_cari(
            URL_PROFIL,
            "Profil Guru Besar Fakultas Teknik",
            "Dosen profesor UIR",
        )
        berita = _item_cari(
            URL_BERITA,
            "Guru Besar Teknik Paparkan Hasil Penelitian",
            "Berita pengukuhan guru besar",
        )
        self.assertGreaterEqual(pencarian._bonus_dosen(item), 10)
        self.assertGreater(
            pencarian._bonus_dosen(item),
            pencarian._bonus_dosen(berita),
        )
        self.assertGreater(
            pencarian._skor_kandidat('site:uir.ac.id "Guru Besar" "Teknik"', item),
            pencarian._skor_kandidat("kunjungan teknik", item),
        )
        self.assertLess(
            modul._prioritas_url_dosen(URL_PROFIL),
            modul._prioritas_url_dosen(URL_TAMU),
        )
        self.assertLess(
            pencarian._bonus_dosen(_item_cari(
                URL_TAMU,
                "Profesor Tamu Memberikan Kuliah 3 Hari",
                "Dosen dari Arab Saudi",
            )),
            0,
        )

    def test_profesor_tamu_luar_uir_ditolak(self):
        rekam = {
            "url": URL_TAMU,
            "judul": "Dosen dari Arab Saudi Memberikan Kuliah 3 Hari",
            "teks": (
                "Prof. Nama Tamu dari luar negeri memberikan kuliah tiga hari "
                "di Fakultas Teknik Universitas Islam Riau."
            ),
            "sumber": "ambil_halaman",
            "waktu": "2026-11-25T00:00:00+00:00",
        }

        self.assertTrue(modul._adalah_profesor_tamu(rekam))
        self.assertEqual(modul._profesor_evidence([rekam], PERTANYAAN), [])

    def test_webinar_dan_mitra_luar_tidak_menjadi_bukti(self):
        rekam_webinar = {
            "url": (
                "https://uir.ac.id/dosen-fh-asoc-prof-dr-husnu-abadi-"
                "tampil-di-webinar-politik-hukum/"
            ),
            "judul": (
                "Dosen FH, Asoc. Prof Dr Husnu Abadi Tampil di "
                "Webinar Politik Hukum"
            ),
            "teks": (
                "Assoc. Prof. Dr. Husnu Abadi, M.Hum, Dosen Senior "
                "Fakultas Hukum UIR tampil sebagai pembicara dalam Webinar. "
                "Pada sesi tanya jawab, Prof. Dr. I Made Arya Utama dari "
                "Universitas Udayana mengenai politik hukum."
            ),
            "sumber": "baca_cache",
            "waktu": "2026-11-25T00:00:00+00:00",
        }
        rekam_mou = {
            "url": (
                "https://uir.ac.id/apu-dan-fakultas-teknik-uir-jalin-"
                "kerjasama-tingkatkan-dunia-pendidikan/"
            ),
            "judul": (
                "APU dan Fakultas Teknik UIR Jalin Kerjasama "
                "Tingkatkan Dunia Pendidikan"
            ),
            "teks": (
                "Dalam hal ini Ir. H. Abdul Kudus Zaini, MT sebagai Dekan "
                "Fakultas Teknik UIR bersama Prof. Ron Edwards Ph.D telah "
                "menandatangani MoU, hadir juga perwakilan APU."
            ),
            "sumber": "baca_cache",
            "waktu": "2026-11-25T00:00:00+00:00",
        }

        self.assertTrue(modul._adalah_profesor_tamu(rekam_webinar))
        self.assertTrue(modul._adalah_profesor_tamu(rekam_mou))
        self.assertEqual(
            modul._profesor_evidence(
                [rekam_webinar], "Siapa profesor di lingkungan UIR?"
            ),
            [],
        )
        self.assertEqual(
            modul._profesor_evidence(
                [rekam_mou], "Sebutkan nama profesor dari Fakultas Teknik."
            ),
            [],
        )


class UjiAlurProfesor(unittest.TestCase):
    def setUp(self):
        self.palsu = {}

    def _pasang_tool(self, hasil_cari, peta_baca=None):
        patchers = {
            "cari": mock.patch.object(
                modul, "cari_uir", return_value=hasil_cari
            ),
            "baca": mock.patch.object(
                modul,
                "baca_cache",
                side_effect=lambda url: (
                    peta_baca or {}
                ).get(url, {
                    "ok": False,
                    "status": "tidak_ditemukan",
                    "url": url,
                    "data": None,
                    "pesan": "Tidak ada cache.",
                }),
            ),
            "ambil": mock.patch.object(
                modul,
                "ambil_halaman",
                return_value={
                    "ok": False,
                    "status": "situs_tidak_tersedia",
                    "url": "",
                    "judul": "",
                    "teks": "",
                    "panjang": 0,
                    "pesan": "Tidak mengambil halaman pada uji ini.",
                },
            ),
            "simpan": mock.patch.object(
                modul,
                "simpan_cache",
                return_value={"ok": True, "status": "tersimpan"},
            ),
        }
        for nama, patcher in patchers.items():
            self.palsu[nama] = patcher.start()
            self.addCleanup(patcher.stop)

    def _orkestrator(self, *respons):
        return modul.Orkestrator(
            client=KlienPalsu(*respons), verifikasi_wajib=True
        )

    def test_profil_dibaca_dan_jawaban_terstruktur(self):
        self._pasang_tool(
            _hasil_cari(_item_cari(
                URL_PROFIL,
                "Profil Dosen dan Guru Besar Fakultas Teknik",
                "Nama dan jabatan dosen profesor UIR",
            )),
            {URL_PROFIL: _hasil_baca(
                URL_PROFIL, "Profil Dosen Fakultas Teknik", TEKS_PROFIL
            )},
        )
        ork = self._orkestrator(_respons())

        hasil = ork.tanya(PERTANYAAN)

        self.assertTrue(hasil["ok"])
        self.assertGreaterEqual(hasil["langkah"], 1)
        self.assertIn(
            "**Profesor dan Guru Besar Fakultas Teknik UIR**",
            hasil["jawaban"],
        )
        self.assertIn("H. Andi Contoh", hasil["jawaban"])
        self.assertIn("Guru Besar", hasil["jawaban"])
        self.assertIn("Fakultas Teknik", hasil["jawaban"])
        self.assertIn(URL_PROFIL, hasil["jawaban"])
        self.assertIn(URL_PROFIL, hasil["sumber"])
        self.assertTrue(hasil["status_sumber"][URL_PROFIL]["cited"])
        self.assertTrue(hasil["status_sumber"][URL_PROFIL]["evidence"])
        self.assertNotIn("Budi Salah", hasil["jawaban"])

    def test_validasi_jabatan_menolak_doktor_dan_nama_rekaan(self):
        rekam = {
            "url": URL_PROFIL,
            "judul": "Profil Dosen Fakultas Teknik",
            "teks": TEKS_PROFIL,
            "sumber": "baca_cache",
            "waktu": "2026-11-25T00:00:00+00:00",
        }
        bukti = modul._profesor_evidence([rekam], PERTANYAAN)
        nama = {item["nama"] for item in bukti}

        self.assertEqual(nama, {"H. Andi Contoh", "Rina Contoh"})
        self.assertNotIn("Budi Salah", nama)
        self.assertNotIn("H. Andi Contoh Tim Humas", nama)
        self.assertNotIn("H. Andi Contoh KOPERTIS X", nama)

        jabatan = {item["nama"]: item["jabatan"] for item in bukti}
        self.assertEqual(jabatan["H. Andi Contoh"], "Guru Besar")
        self.assertEqual(jabatan["Rina Contoh"], "Profesor")
        self.assertEqual(
            modul._jabatan_profesor("Dr. Budi Salah, Lektor Kepala"),
            "",
        )

        benar = modul._profesor_answer([rekam], PERTANYAAN)
        self.assertEqual(
            modul._klaim_profesor_didukung(benar, [rekam], PERTANYAAN),
            (True, ""),
        )

        rekaan = benar.replace("H. Andi Contoh", "Nama Rekaan UIR")
        self.assertFalse(
            modul._klaim_profesor_didukung(rekaan, [rekam], PERTANYAAN)[0]
        )

    def test_sitasi_hanya_untuk_halaman_pendukung(self):
        self._pasang_tool(
            _hasil_cari(
                _item_cari(
                    URL_PROFIL,
                    "Profil Guru Besar Fakultas Teknik",
                    "Nama dan jabatan profesor UIR",
                ),
                _item_cari(
                    URL_BERITA,
                    "Kunjungan Fakultas Teknik",
                    "Berita kunjungan tanpa jabatan profesor",
                ),
            ),
            {URL_PROFIL: _hasil_baca(
                URL_PROFIL, "Profil Dosen Fakultas Teknik", TEKS_PROFIL
            )},
        )
        ork = self._orkestrator(_respons())

        hasil = ork.tanya(PERTANYAAN)

        self.assertIn(URL_PROFIL, hasil["sumber"])
        self.assertNotIn(URL_BERITA, hasil["sumber"])
        self.assertNotIn(URL_BERITA, hasil["jawaban"])

    def test_jawaban_tidak_mengklaim_daftar_lengkap(self):
        self._pasang_tool(
            _hasil_cari(_item_cari(
                URL_PROFIL,
                "Profil Guru Besar Fakultas Teknik",
                "Nama dan jabatan profesor UIR",
            )),
            {URL_PROFIL: _hasil_baca(
                URL_PROFIL, "Profil Dosen Fakultas Teknik", TEKS_PROFIL
            )},
        )
        ork = self._orkestrator(_respons())

        hasil = ork.tanya(
            "Ada berapa profesor di Fakultas Teknik UIR?"
        )

        self.assertIn("Angka ini bukan jumlah resmi", hasil["jawaban"])
        self.assertIn("tidak dinyatakan lengkap", hasil["jawaban"])
        self.assertNotIn("daftar lengkap", hasil["jawaban"].lower())
        self.assertRegex(hasil["jawaban"], r"Ditemukan \d+ nama")

    def test_penolakan_transparan_bila_pencarian_memadai_tanpa_bukti(self):
        self._pasang_tool(
            _hasil_cari(_item_cari(
                URL_TANPA_BUKTI,
                "Halaman Guru Besar UIR",
                "Halaman informasi Guru Besar",
            )),
            {URL_TANPA_BUKTI: _hasil_baca(
                URL_TANPA_BUKTI,
                "Halaman Guru Besar UIR",
                TEKS_TANPA_BUKTI,
            )},
        )
        ork = self._orkestrator(_respons())

        hasil = ork.tanya(PERTANYAAN)

        self.assertFalse(hasil["ok"])
        self.assertEqual(hasil["status"], "bukti_tidak_cukup")
        self.assertIn("Saya sudah mencari beberapa variasi", hasil["jawaban"])
        self.assertIn("membaca 1 halaman", hasil["jawaban"])
        self.assertIn("belum menemukan teks", hasil["jawaban"])
        self.assertIn("silakan klarifikasi", hasil["jawaban"].lower())
        self.assertNotIn("H. Andi Contoh", hasil["jawaban"])


if __name__ == "__main__":
    unittest.main()
