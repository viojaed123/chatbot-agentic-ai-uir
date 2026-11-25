"""Regresi guard relevansi akhir dan jawaban jumlah fakultas.

Semua teks di sini adalah data tiruan. Tidak ada angka resmi UIR yang
disimpan di dalam test; angka pada teks katalog hanya contoh struktur kalimat.
"""

import unittest

from app import orchestrator as modul


URL_KATALOG = "https://uir.ac.id/program-pendidikan/"


def _rekam_katalog(teks):
    return {
        "url": URL_KATALOG,
        "judul": "Program Pendidikan - Universitas Islam Riau",
        "teks": teks,
        "sumber": "baca_cache",
    }


class UjiGuardRelevansi(unittest.TestCase):
    def test_daftar_fakultas_universitas_bukan_nama_unit(self):
        pertanyaan = "Sebutkan daftar fakultas Universitas Islam Riau."
        self.assertEqual(modul._unit_fakultas(pertanyaan), [])

        jawaban = (
            "Berdasarkan halaman resmi UIR, daftar fakultasnya adalah:\n"
            "- Fakultas Hukum\n- Fakultas Teknik\n- Fakultas Ekonomi"
        )
        self.assertTrue(
            modul._jawaban_menjawab_pertanyaan(pertanyaan, jawaban)
        )

    def test_wakil_rektor_tidak_diisi_oleh_rektor(self):
        jawaban = (
            "Dari halaman profil pimpinan UIR, Rektor UIR adalah "
            "Prof. Dr. Contoh, S.H., MCL.\n\nSumber:\n"
            "- https://uir.ac.id/profil/pimpinan-uir/"
        )
        self.assertFalse(
            modul._jawaban_menjawab_pertanyaan(
                "Siapa wakil rektor UIR?", jawaban
            )
        )

    def test_wakil_rektor_diisi_dengan_bukti_yang_tepat(self):
        jawaban = (
            "Wakil rektor UIR yang tercantum pada halaman resmi adalah "
            "Prof. Contoh."
        )
        self.assertTrue(
            modul._jawaban_menjawab_pertanyaan(
                "Siapa wakil rektor UIR?", jawaban
            )
        )

    def test_berita_fakultas_lain_tidak_menjawab_dekan_teknik(self):
        jawaban = (
            "Universitas Islam Riau menyambut mahasiswa Fakultas Pertanian "
            "Program Studi Agroteknologi yang meraih prestasi internasional."
        )
        self.assertFalse(
            modul._jawaban_menjawab_pertanyaan(
                "Siapa dekan Fakultas Teknik UIR?", jawaban
            )
        )

    def test_tanggal_artikel_bukan_jumlah_fakultas(self):
        jawaban = (
            "Fakultas Hukum UIR membuka pendaftaran pendidikan advokat. "
            "Kegiatan berlangsung mulai 09 Februari hingga 03 Maret 2019 "
            "di Jalan Kaharuddin Nasution 113 Pekanbaru."
        )
        self.assertFalse(
            modul._jawaban_menjawab_pertanyaan(
                "Berapa jumlah fakultas di UIR?", jawaban
            )
        )

    def test_jumlah_fakultas_dengan_angka_topik_diterima(self):
        jawaban = (
            "Berdasarkan halaman resmi UIR:\n"
            "- Universitas Islam Riau memiliki 12 fakultas dan "
            "pascasarjana yang tercantum pada halaman program pendidikan."
        )
        self.assertTrue(
            modul._jawaban_menjawab_pertanyaan(
                "Berapa jumlah fakultas di UIR?", jawaban
            )
        )

    def test_jumlah_profesor_tidak_terkena_guard_fakultas(self):
        jawaban = (
            "Ditemukan 3 nama profesor pada halaman profil dosen "
            "Fakultas Teknik. Angka ini bukan jumlah resmi dan tidak "
            "dinyatakan lengkap."
        )
        self.assertTrue(
            modul._jawaban_menjawab_pertanyaan(
                "Ada berapa profesor di Fakultas Teknik UIR?", jawaban
            )
        )

    def test_alasan_penolakan_menyebut_topik_yang_diminta(self):
        self.assertIn(
            "wakil rektor",
            modul._alasan_jawaban_melenceng(
                "Siapa wakil rektor UIR?"
            ).lower(),
        )
        self.assertIn(
            "dekan",
            modul._alasan_jawaban_melenceng(
                "Siapa dekan Fakultas Teknik UIR?"
            ).lower(),
        )


class UjiJumlahFakultas(unittest.TestCase):
    def test_jumlah_diambil_dari_kalimat_katalog(self):
        teks = (
            "FAKULTAS DAN PASCASARJANA\n"
            "Universitas Islam Riau menyediakan 12 fakultas dan "
            "satu program pascasarjana berdasarkan halaman program pendidikan.\n"
            "Daftar fakultas dan program studi tercantum pada tabel akreditasi "
            "program studi ini. " * 12
        )
        rekam = _rekam_katalog(teks)
        jawaban = modul._jumlah_fakultas_answer(
            [rekam], "Berapa jumlah fakultas di UIR?"
        )
        self.assertIsNotNone(jawaban)
        self.assertIn("12 fakultas", jawaban)

    def test_berita_tidak_dijadikan_jumlah(self):
        rekam = {
            "url": "https://uir.ac.id/berita/pendaftaran-advokat/",
            "judul": "Fakultas Hukum UIR Buka Pendaftaran Pendidikan Advokat",
            "teks": (
                "Kegiatan berlangsung mulai 09 Februari hingga 03 Maret "
                "2019 di Fakultas Hukum UIR. " * 4
            ),
            "sumber": "baca_cache",
        }
        self.assertIsNone(
            modul._jumlah_fakultas_answer(
                [rekam], "Berapa jumlah fakultas di UIR?"
            )
        )

    def test_pertanyaan_satu_fakultas_tidak_dijawab_jumlah_uir(self):
        self.assertIsNone(
            modul._jumlah_fakultas_answer(
                [_rekam_katalog("Universitas Islam Riau memiliki 12 fakultas.")],
                "Berapa jumlah fakultas di Fakultas Teknik UIR?",
            )
        )


if __name__ == "__main__":
    unittest.main()
