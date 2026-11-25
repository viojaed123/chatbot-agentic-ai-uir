"""Regresi untuk pencarian multi-subkueri dan bukti gabungan.

Pengujian ini memakai tool tiruan, sehingga tidak mengakses internet maupun
Ollama. Fokusnya adalah mekanisme: dekomposisi kata kunci, penggabungan bukti,
pembatasan konteks per giliran, verifikasi klaim, dan sitasi per bagian.
"""

import json
import unittest
from unittest import mock

from app import orchestrator as modul
from tools import cari_uir as pencarian


URL_A = "https://uir.ac.id/fasilitas/"
URL_B = "https://uir.ac.id/program-pendidikan/"
URL_C = "https://uir.ac.id/profil/"

TEKS_FASILITAS = (
    "Fasilitas UIR meliputi perpustakaan, laboratorium, dan sarana olahraga "
    "bagi mahasiswa. Informasi fasilitas kampus ini tersedia untuk umum."
)
TEKS_PRODI = (
    "FAKULTAS DAN PASCASARJANA\n"
    "Fakultas Hukum\n"
    "Tabel Akreditasi Program Studi\n"
    "Nama Prodi\n"
    "Jenjang\n"
    "Skor Nasional\n"
    "Ilmu Hukum\n"
    "S1\n"
    "A\n"
    "Universitas Islam Riau menyediakan program studi dan fakultas. " * 5
)


def _respons(konten="", tool_calls=None):
    return {
        "model": "hermes3:8b",
        "message": {
            "role": "assistant",
            "content": konten,
            "tool_calls": tool_calls or [],
        },
    }


def _call(nama, argumen):
    return {"function": {"name": nama, "arguments": argumen}}


class KlienDiam:
    """Klien yang gagal bila dipanggil, untuk memastikan jalur tanpa model."""

    def chat(self, messages, tools=None):
        raise AssertionError("Model tidak boleh dipanggil pada jalur ini.")


def _hasil_cari(items, sumber="wp_rest+sitemap"):
    return {
        "ok": True,
        "status": "ditemukan",
        "query": "uji",
        "count": len(items),
        "sumber": sumber,
        "results": items,
        "subkueri": ["uji"],
        "per_subkueri": [],
        "pesan": "Ditemukan.",
        "waktu": "2026-11-24T00:00:00+00:00",
    }


def _item(url, judul, cuplikan, sub=()):
    return {
        "judul": judul,
        "url": url,
        "cuplikan": cuplikan,
        "_sumber_kandidat": "wp_rest",
        "_subkueri": list(sub),
        "subkueri": list(sub),
        "terverifikasi_sitemap": True,
    }


def _cache(url, judul, teks):
    return {
        "ok": True,
        "status": "ditemukan",
        "url": url,
        "data": {
            "url": url,
            "judul": judul,
            "teks": teks,
            "panjang": len(teks),
            "waktu": "2026-11-24T00:00:00+00:00",
        },
        "waktu_cache": "2026-11-24T00:00:00+00:00",
        "pesan": "Cache ditemukan.",
    }


class UjiDekomposisiPencarian(unittest.TestCase):
    def test_pertanyaan_sederhana_tetap_satu_subkueri(self):
        hasil = pencarian._urai_subkueri("beasiswa")
        self.assertEqual(len(hasil), 1)
        self.assertEqual(hasil[0], "beasiswa")

    def test_pertanyaan_multi_aspek_diecah_beberapa_subkueri(self):
        hasil = pencarian._urai_subkueri(
            "Apa saja fakultas dan program studi di UIR?"
        )

        self.assertGreaterEqual(len(hasil), 3)
        self.assertEqual(len(hasil), len(set(hasil)))
        self.assertTrue(any("fakultas" in kata for kata in hasil))
        self.assertTrue(any("program studi" in kata for kata in hasil))

    def test_jumlah_subkueri_dibatasi(self):
        hasil = pencarian._urai_subkueri(
            "Jelaskan fakultas, program studi, akreditasi, sejarah, "
            "dan fasilitas UIR"
        )

        self.assertLessEqual(len(hasil), pencarian.MAX_SUBKUERI)

    def test_gabungkan_kandidat_menghapus_duplikat_dan_menyimpan_provenan(self):
        rest = [_item(URL_B, "Program Pendidikan", "Katalog", ("program studi",))]
        sitemap = [{
            "judul": "Program Pendidikan",
            "url": URL_B,
            "_subkueri": ["daftar program studi"],
        }]

        hasil = pencarian._gabungkan_kandidat(
            rest,
            sitemap,
            "daftar program studi",
            ["daftar program studi", "program studi"],
        )

        self.assertEqual(len(hasil), 1)
        self.assertTrue(hasil[0]["terverifikasi_sitemap"])
        self.assertIn("program studi", hasil[0]["_subkueri"])
        self.assertIn("daftar program studi", hasil[0]["_subkueri"])

    def test_topik_fasilitas_juga_memakai_sitemap(self):
        self.assertTrue(pencarian._perlu_sitemap("fasilitas UIR"))
        self.assertTrue(pencarian._perlu_sitemap("pimpinan UIR"))
        self.assertTrue(pencarian._perlu_sitemap("akreditasi UIR"))
        self.assertFalse(pencarian._perlu_sitemap("beasiswa"))

        # Normalisasi "uir"/"halaman" menjadi "profil UIR". Kata profil
        # tidak boleh memicu sitemap karena REST sudah cukup dan test lama
        # mengharapkan perilaku tersebut.
        for kata in ("profil", "halaman", "uir"):
            self.assertFalse(
                pencarian._perlu_sitemap(
                    pencarian._normalisasi_query(kata)
                ),
                kata,
            )

    def test_halaman_fasilitas_mengalahkan_artikel(self):
        artikel = {
            "judul": "Digital CORNER UIR, Fasilitas Internet Unggulan Kampus",
            "url": (
                "https://uir.ac.id/digital-corner-uir-fasilitas-"
                "internet-unggulan-kampus/"
            ),
            "cuplikan": "",
            "jenis": "post",
        }
        halaman = {
            "judul": "Fasilitas",
            "url": "https://uir.ac.id/fasilitas/",
            "cuplikan": "",
            "jenis": "sitemap",
        }

        self.assertGreater(
            pencarian._skor_kandidat("fasilitas", halaman),
            pencarian._skor_kandidat("fasilitas", artikel),
        )

    def test_cari_utama_memakai_semua_subkueri(self):
        sub = ["daftar fakultas", "program pendidikan"]

        with mock.patch.object(
            pencarian, "_cari_via_rest", return_value=[]
        ) as rest, mock.patch.object(
            pencarian, "_cari_di_sitemap", return_value=[]
        ) as sitemap, mock.patch.object(
            pencarian, "_jeda"
        ):
            hasil = pencarian._cari_utama(sub[0], sub)

        self.assertEqual(rest.call_count, 2)
        self.assertEqual(sitemap.call_count, 2)
        self.assertEqual(hasil["subkueri"], sub)
        self.assertEqual(len(hasil["per_subkueri"]), 2)


class UjiBuktiGabungan(unittest.TestCase):
    def _orkestrator(self, hasil_cari, jawaban="Jawaban uji."):
        klien = mock.Mock()
        klien.chat.return_value = _respons(jawaban)
        return modul.Orkestrator(client=klien, verifikasi_wajib=True)

    def test_metadata_kandidat_tersimpan_saat_pencarian(self):
        ork = self._orkestrator(None)
        ork._record_search(_hasil_cari([
            _item(URL_A, "Fasilitas UIR", "Daftar fasilitas.", ("fasilitas",)),
        ]))

        info = ork.info_kandidat[URL_A]

        self.assertEqual(info["judul"], "Fasilitas UIR")
        self.assertEqual(info["subkueri"], ["fasilitas"])
        self.assertEqual(info["sumber"], "wp_rest")

    def test_bootstrap_kanonik_dijalankan_sebelum_model(self):
        kandidat = [
            _item(
                URL_B,
                "Program Pendidikan",
                "Katalog program studi.",
                ("program studi",),
            ),
        ]

        with mock.patch.object(
            modul, "cari_uir", return_value=_hasil_cari(kandidat)
        ), mock.patch.object(
            modul,
            "baca_cache",
            return_value=_cache(URL_B, "Program Pendidikan", TEKS_PRODI),
        ), mock.patch.object(
            modul, "ambil_halaman", return_value={"ok": False}
        ), mock.patch.object(
            modul, "simpan_cache", return_value={"ok": True}
        ):
            ork = self._orkestrator(None)
            hasil = ork.tanya(
                "Apa saja program studi yang tersedia di UIR?"
            )

        self.assertIn(URL_B, hasil["sumber_terbaca"])
        self.assertEqual(hasil["langkah"], 2)
        self.assertLessEqual(hasil["langkah"], modul.MAX_LANGKAH)

    def test_klaim_angka_model_dibuang_dari_jawaban_sebagian(self):
        kandidat = [
            _item(URL_A, "Fasilitas UIR", TEKS_FASILITAS, ("fasilitas",)),
        ]
        jawaban_model = (
            "Berikut 5 fasilitas UIR:\n"
            "- Perpustakaan\n- Laboratorium\n- Ditambahkan tahun 2023"
        )

        with mock.patch.object(
            modul, "cari_uir", return_value=_hasil_cari(kandidat)
        ), mock.patch.object(
            modul,
            "baca_cache",
            return_value=_cache(URL_A, "Fasilitas UIR", TEKS_FASILITAS),
        ), mock.patch.object(
            modul, "ambil_halaman", return_value={"ok": False}
        ), mock.patch.object(
            modul, "simpan_cache", return_value={"ok": True}
        ):
            ork = self._orkestrator(jawaban_model)
            hasil = ork.tanya("Apa saja fasilitas yang tersedia di UIR?")

        self.assertTrue(hasil["ok"])
        self.assertIn(URL_A, hasil["sumber_terbaca"])
        self.assertNotIn("2023", hasil["jawaban"])
        self.assertNotIn("5 fasilitas", hasil["jawaban"])

    def test_bootstrap_hanya_untuk_topik_berindeks(self):
        ork = self._orkestrator(None)

        for pertanyaan in (
            "Apa saja program studi yang tersedia di UIR?",
            "Siapa pimpinan Universitas Islam Riau?",
            "Apa saja fasilitas yang tersedia di UIR?",
            "Berapa jumlah fakultas di UIR?",
        ):
            ork.pertanyaan = pertanyaan
            self.assertTrue(
                ork._perlu_bootstrap_evidence(),
                pertanyaan,
            )

        for pertanyaan in (
            "Info beasiswa UIR",
            "Kapan UIR berdiri?",
            "Berapa jumlah fakultas di Fakultas Teknik UIR?",
        ):
            ork.pertanyaan = pertanyaan
            self.assertFalse(
                ork._perlu_bootstrap_evidence(),
                pertanyaan,
            )

    def test_pertanyaan_multi_topik_membaca_beberapa_halaman(self):
        kandidat = [
            _item(URL_A, "Fasilitas UIR", TEKS_FASILITAS, ("fasilitas",)),
            _item(URL_B, "Program Pendidikan", TEKS_PRODI, ("program studi",)),
            _item(URL_C, "Profil UIR", "Sejarah UIR.", ("sejarah",)),
        ]

        def baca(url):
            if url == URL_A:
                return _cache(url, "Fasilitas UIR", TEKS_FASILITAS)
            if url == URL_B:
                return _cache(url, "Program Pendidikan", TEKS_PRODI)
            return _cache(url, "Profil UIR", "Informasi profil UIR. " * 5)

        with mock.patch.object(
            modul, "cari_uir", return_value=_hasil_cari(kandidat)
        ), mock.patch.object(
            modul, "baca_cache", side_effect=baca
        ), mock.patch.object(
            modul, "ambil_halaman", return_value={"ok": False}
        ), mock.patch.object(
            modul, "simpan_cache", return_value={"ok": True}
        ):
            ork = self._orkestrator(None)
            hasil = ork.tanya("Apa saja fasilitas dan program studi di UIR?")

        self.assertGreaterEqual(len(hasil["sumber_terbaca"]), 2)
        self.assertLessEqual(hasil["langkah"], modul.MAX_LANGKAH)
        self.assertTrue(
            any("bagian" in record for record in ork.bukti)
        )

    def test_bukti_lama_tidak_dipakai_untuk_topik_baru(self):
        lama = {
            "url": URL_A,
            "judul": "Fasilitas UIR",
            "teks": TEKS_FASILITAS,
            "sumber": "baca_cache",
            "pertanyaan": "fasilitas",
            "bagian": [TEKS_FASILITAS],
        }
        baru = {
            "url": URL_B,
            "judul": "Program Pendidikan",
            "teks": TEKS_PRODI,
            "sumber": "baca_cache",
            "pertanyaan": "program studi",
            "bagian": [TEKS_PRODI[:200]],
        }

        ork = self._orkestrator(None)
        ork.bukti = [lama, baru]
        ork.turn_url = [URL_B]
        ork.pertanyaan = "Apa saja program studi di UIR?"

        self.assertEqual(
            [record["url"] for record in ork._relevant_records()],
            [URL_B],
        )


class UjiVerifikasiDanFinalisasi(unittest.TestCase):
    def test_pertanyaan_ambigu_tanpa_model_dan_tanpa_sumber(self):
        ork = modul.Orkestrator(client=KlienDiam(), verifikasi_wajib=True)
        hasil = ork.tanya("Salah satunya?")

        self.assertFalse(hasil["ok"])
        self.assertEqual(hasil["status"], "butuh_klarifikasi")
        self.assertEqual(hasil["sumber"], [])
        self.assertIn("topik", hasil["jawaban"].lower())

    def test_klaim_angka_tanpa_bukti_ditolak(self):
        rekam = [{
            "url": URL_A,
            "judul": "Beasiswa UIR",
            "teks": "Informasi beasiswa untuk mahasiswa UIR.",
            "sumber": "baca_cache",
        }]

        valid, alasan = modul._claims_supported(
            "Beasiswa UIR tahun 2030 senilai 5 juta.",
            rekam,
            "Info beasiswa",
        )

        self.assertFalse(valid)
        self.assertIn("Angka", alasan)

    def test_ringkasan_umum_tanpa_klaim_spesifik_diterima(self):
        rekam = [{
            "url": URL_A,
            "judul": "Beasiswa UIR",
            "teks": "Informasi beasiswa untuk mahasiswa UIR.",
            "sumber": "baca_cache",
        }]

        valid, _ = modul._claims_supported(
            "Beasiswa dapat dipakai sebagai bahan pertimbangan.",
            rekam,
            "Info beasiswa",
        )

        self.assertTrue(valid)

    def test_konflik_sumber_didokumentasikan(self):
        rekam = [
            {
                "url": URL_C,
                "judul": "Profil UIR",
                "teks": (
                    "Universitas Islam Riau berdiri pada tahun 1962 "
                    "di Pekanbaru."
                ),
            },
            {
                "url": URL_A,
                "judul": "Sejarah UIR",
                "teks": (
                    "Universitas Islam Riau berdiri pada tahun 1968 "
                    "di Pekanbaru."
                ),
            },
        ]

        konflik = modul._deteksi_konflik(rekam)

        self.assertTrue(konflik)
        self.assertIn("1962", konflik[0])
        self.assertIn("1968", konflik[0])

    def test_sitasi_dikelompokkan_per_bagian(self):
        rekam = [
            {
                "url": URL_B,
                "judul": "Program Pendidikan",
                "teks": TEKS_PRODI,
                "bagian": ["Daftar fakultas dan program studi."],
            },
            {
                "url": URL_A,
                "judul": "Fasilitas UIR",
                "teks": TEKS_FASILITAS,
                "bagian": ["Fasilitas UIR meliputi perpustakaan."],
            },
        ]

        jawaban = modul._sumber_per_bagian(
            "Bagian fakultas.\n\nBagian program studi.",
            rekam,
            "fakultas dan program studi",
        )

        self.assertIn("Sumber berdasarkan bagian:", jawaban)
        self.assertIn("fakultas:", jawaban)
        self.assertIn("program studi:", jawaban)
        self.assertIn(URL_B, jawaban)

    def test_jawaban_multi_aspek_disusun_dari_parser(self):
        rekam = [{
            "url": URL_B,
            "judul": "Program Pendidikan",
            "teks": TEKS_PRODI,
            "sumber": "baca_cache",
        }]

        jawaban = modul._jawaban_multi_aspek(
            rekam,
            "Apa saja fakultas dan program studi di UIR?",
            "",
        )

        self.assertIsNotNone(jawaban)
        self.assertIn("Fakultas Hukum", jawaban)
        self.assertIn("Ilmu Hukum", jawaban)

    def test_jawaban_sebagian_mengakui_bagian_yang_belum_ada(self):
        teks = (
            "FAKULTAS DAN PASCASARJANA\n"
            "Fakultas Hukum\n"
            "Fakultas Teknik\n"
            "Halaman ini memuat daftar resmi fakultas UIR. " * 5
        )
        rekam = [{
            "url": URL_B,
            "judul": "Program Pendidikan",
            "teks": teks,
            "sumber": "baca_cache",
            "bagian": [teks[:200]],
        }]

        jawaban = modul._jawaban_sebagian(
            rekam,
            "Apa saja fakultas dan program studi di UIR?",
            "Fakultas Hukum dan Fakultas Teknik.",
        )

        self.assertIsNotNone(jawaban)
        self.assertIn("Fakultas Hukum", jawaban)
        self.assertIn("Belum ditemukan", jawaban)
        self.assertIn("program studi", jawaban.lower())


class UjiPerbaikanRegresiTerakhir(unittest.TestCase):
    def test_berita_dengan_kata_daftar_bukan_katalog(self):
        berita = {
            "url": (
                "https://uir.ac.id/daftar-kuliah-di-uir-jangan-asal-"
                "pilih-program-studi/"
            ),
            "judul": "Daftar Kuliah di UIR Jangan Asal Pilih Program Studi",
            "teks": (
                "Saat melakukan Pendaftaran Mahasiswa Baru di UIR, "
                "mahasiswa perlu memahami program studi yang dipilih. " * 10
            ),
            "sumber": "baca_cache",
        }

        self.assertFalse(
            modul._authoritative_catalog(berita, "program studi")
        )
        self.assertFalse(modul._ada_data_katalog([berita], "program studi"))

    def test_artikel_rest_tidak_mendapat_bonus_katalog(self):
        artikel = {
            "judul": "Program Studi Doktor Sains Manajemen Raih Unggul",
            "url": (
                "https://uir.ac.id/program-studi-doktor-sains-manajemen-"
                "uir-sukses-raih-akreditasi-unggul/"
            ),
            "cuplikan": "",
            "jenis": "post",
        }
        halaman = {
            "judul": "Fasilitas UIR",
            "url": URL_A,
            "cuplikan": "Fasilitas kampus UIR.",
        }

        self.assertTrue(pencarian._adalah_artikel(artikel))
        self.assertLess(
            pencarian._skor_kandidat("fasilitas", artikel),
            pencarian._skor_kandidat("fasilitas", halaman),
        )

    def test_halaman_kanonik_dibedakan_dari_artikel(self):
        artikel = {
            "url": URL_A + "digital-corner-uir/",
            "judul": "Digital CORNER UIR",
            "jenis": "post",
        }
        halaman = {
            "url": URL_A,
            "judul": "Fasilitas Kampus",
            "jenis": "page",
        }

        self.assertFalse(modul._ada_halaman_kanonik([artikel]))
        self.assertTrue(modul._ada_halaman_kanonik([halaman]))

    def test_label_umum_bukan_nama_rekaan(self):
        rekam = [{
            "url": URL_C,
            "judul": "Pimpinan UIR",
            "teks": (
                "Pimpinan Universitas Islam Riau dapat dihubungi melalui "
                "kontak resmi. Alamat dan jam layanan tersedia di halaman "
                "tersebut."
            ),
        }]

        valid, alasan = modul._klaim_umum_didukung(
            "Telepon dan email dapat dilihat pada bagian Kontak.",
            rekam,
            "Siapa pimpinan UIR?",
        )

        self.assertTrue(valid, alasan)

        valid, alasan = modul._klaim_umum_didukung(
            "Struktur Organisasi dapat dilihat pada halaman Pimpinan UIR.",
            rekam,
            "Siapa pimpinan UIR?",
        )

        self.assertTrue(valid, alasan)

        valid, alasan = modul._klaim_umum_didukung(
            "Telepon Syafrinaldi aktif sejak 1999.",
            rekam,
            "Siapa pimpinan UIR?",
        )

        self.assertFalse(valid)
        self.assertIn("Angka", alasan)

    def test_sumber_ditambahkan_tanpa_bagian_ganda(self):
        jawaban = (
            "Berdasarkan katalog UIR, terdapat daftar fakultas.\n\n"
            "Sumber:\n- " + URL_B
        )

        hasil = modul._tambah_sumber(
            jawaban,
            [URL_B, URL_A],
        )

        self.assertEqual(hasil.count("Sumber:"), 1)
        self.assertIn(URL_A, hasil)
        self.assertIn(URL_B, hasil)

    def test_sumber_hanya_halaman_yang_dipakai(self):
        jawaban = (
            "Berdasarkan katalog UIR, terdapat 34 program studi.\n\n"
            "Sumber:\n- " + URL_A
        )
        rekam = [
            {
                "url": URL_B,
                "judul": "Program Pendidikan",
                "teks": TEKS_PRODI,
                "sumber": "baca_cache",
                "bagian": [TEKS_PRODI[:200]],
            },
            {
                "url": URL_A,
                "judul": "Berita fasilitas",
                "teks": "Berita tentang kegiatan kampus UIR. " * 10,
                "sumber": "baca_cache",
                "bagian": ["Berita tentang kegiatan kampus UIR."],
            },
        ]

        dipakai = modul._sumber_dipakai(
            jawaban,
            rekam,
            "Apa saja program studi yang tersedia di UIR?",
        )
        akhir = modul._ganti_sumber(jawaban, dipakai)

        self.assertEqual(dipakai, [URL_B])
        self.assertEqual(akhir.count("Sumber:"), 1)
        self.assertIn(URL_B, akhir)
        self.assertNotIn(URL_A, akhir)

    def test_markup_tool_call_dibersihkan(self):
        mentah = (
            "Jawaban resmi.\n\n"
            "<tool_call>{\"name\": \"cari_uir\"}</tool_call>"
        )

        self.assertEqual(modul._bersihkan_jawaban(mentah), "Jawaban resmi.")


if __name__ == "__main__":
    unittest.main()
