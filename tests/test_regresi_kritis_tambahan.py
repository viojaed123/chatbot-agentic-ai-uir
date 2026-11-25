"""Regresi untuk perbaikan retrieval, bukti, dan sitasi tiga kasus kritis.

Pengujian ini tidak mengakses internet maupun Ollama. Fokusnya adalah
mekanisme: variasi pencarian, jawaban yang hanya memuat klaim terbukti, dan
status sumber dari ``retrieved`` sampai ``cited``.
"""

import unittest
from unittest import mock

from app import orchestrator as modul
from app import perencana
from tools import cari_uir as pencarian


URL_PIMPINAN = "https://uir.ac.id/profil/pimpinan-uir/"
URL_BEASISWA = "https://uir.ac.id/beasiswa/"
URL_PMB = "https://uir.ac.id/penerimaan-mahasiswa-baru/"

TEKS_PIMPINAN = (
    "Pimpinan Universitas Islam Riau\n\n"
    "Rektor\n"
    "Prof. Dr. rer. pol. H. Syafrinaldi, S.H.,MCL\n\n"
    "Wakil Rektor\n"
    "Dr. H. Aulia Rachman, M.Pd.\n"
)

TEKS_BEASISWA = (
    "Informasi Beasiswa UIR\n"
    "Beasiswa KIP dibuka untuk mahasiswa.\n"
    "Beasiswa BSM tersedia bagi mahasiswa UIR.\n"
    "Beasiswa Lama sudah ditutup tahun 2022.\n"
)

TEKS_PMB = (
    "Penerimaan Mahasiswa Baru Universitas Islam Riau tahun akademik "
    "2026/2027 membuka jalur masuk bagi calon mahasiswa. " * 4
    + "\nJalur Khusus tersedia bagi calon mahasiswa yang memenuhi syarat.\n"
    + "Gelombang I dibuka pada periode pendaftaran tertentu.\n"
    + "Pembelian akun PMB dilakukan melalui laman resmi penerimaan "
      "mahasiswa baru.\n"
)


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


def _hasil_cari(items):
    return {
        "ok": True,
        "status": "ditemukan",
        "query": "uji",
        "count": len(items),
        "sumber": "wp_rest+sitemap",
        "results": items,
        "subkueri": ["uji"],
        "per_subkueri": [],
        "pesan": "Ditemukan.",
        "waktu": "2026-11-24T00:00:00+00:00",
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


def _rekam(url, judul, teks):
    return {
        "url": url,
        "judul": judul,
        "teks": teks,
        "sumber": "baca_cache",
        "pertanyaan": "uji",
    }


def _respons(konten):
    return {
        "model": "hermes3:8b",
        "message": {
            "role": "assistant",
            "content": konten,
            "tool_calls": [],
        },
    }


class UjiRegresiKritisTambahan(unittest.TestCase):
    def test_variasi_pencarian_sesuai_jenis_dan_topik(self):
        strategi = perencana.strategi_pencarian(
            "Apa saja beasiswa yang tersedia di UIR?"
        )
        self.assertEqual(strategi["jenis"], "daftar")
        self.assertEqual(strategi["topik"], ["beasiswa"])
        self.assertEqual(len(strategi["subkueri"]), 8)
        self.assertTrue(strategi["butuh_sitemap"])

        strategi = perencana.strategi_pencarian(
            "Bagaimana cara pendaftaran mahasiswa baru UIR?"
        )
        self.assertEqual(strategi["jenis"], "prosedur")
        self.assertEqual(strategi["topik"], ["pmb"])
        self.assertEqual(len(strategi["subkueri"]), 10)

        strategi = perencana.strategi_pencarian("siapa rektor uir")
        self.assertEqual(strategi["jenis"], "identitas")
        self.assertEqual(strategi["topik"], ["pimpinan"])
        self.assertEqual(len(strategi["subkueri"]), 5)

        # Perilaku lama dengan sengaja dipertahankan.
        self.assertEqual(pencarian._urai_subkueri("beasiswa"), ["beasiswa"])
        self.assertFalse(pencarian._perlu_sitemap("beasiswa"))

    def test_jawaban_terstruktur_dan_verifikasi_per_klaim(self):
        identitas = [_rekam(URL_PIMPINAN, "Pimpinan UIR", TEKS_PIMPINAN)]
        jawaban = modul._identitas_answer(identitas, "siapa rektor uir")
        self.assertIn("Syafrinaldi", jawaban)
        self.assertEqual(
            modul._klaim_identitas_didukung(
                jawaban, identitas, "siapa rektor uir"
            ),
            (True, ""),
        )

        laporan = [_rekam(
            "https://uir.ac.id/profil/laporan-rektor-uir/",
            "Laporan Rektor",
            "Laporan tahunan Rektor tahun 2023.",
        )]
        self.assertIsNone(modul._identitas_answer(laporan, "siapa rektor uir"))

        beasiswa = [_rekam(URL_BEASISWA, "Beasiswa", TEKS_BEASISWA)]
        jawaban = modul._beasiswa_answer(
            beasiswa, "Apa saja beasiswa yang tersedia di UIR?"
        )
        self.assertIn("Beasiswa KIP", jawaban)
        self.assertIn("ditutup", jawaban.lower())
        self.assertNotIn("lengkap", jawaban.lower())

        tambahan = (
            "Beasiswa Rekaan dibuka untuk mahasiswa."
        )
        self.assertFalse(modul._klaim_beasiswa_didukung(
            jawaban + "\n" + tambahan, beasiswa,
            "Apa saja beasiswa yang tersedia di UIR?",
        )[0])

        pmb = [_rekam(URL_PMB, "Penerimaan Mahasiswa Baru", TEKS_PMB)]
        jawaban = modul._prosedur_answer(
            pmb, "Bagaimana cara pendaftaran mahasiswa baru UIR?"
        )
        self.assertIn("Jalur Khusus", jawaban)
        self.assertIn("akun PMB", jawaban)
        self.assertIn("belum ditemukan", jawaban.lower())

    def test_status_sumber_dari_retrieved_sampai_cited(self):
        klien = mock.Mock()
        klien.chat.return_value = _respons("Jawaban dari model.")

        with mock.patch.object(
            modul, "cari_uir",
            return_value=_hasil_cari([
                _item(
                    URL_PIMPINAN,
                    "Pimpinan UIR",
                    "Halaman pimpinan resmi.",
                    ("pimpinan UIR",),
                ),
            ]),
        ), mock.patch.object(
            modul, "baca_cache",
            return_value=_cache(
                URL_PIMPINAN, "Pimpinan UIR", TEKS_PIMPINAN
            ),
        ), mock.patch.object(
            modul, "ambil_halaman", return_value={"ok": False}
        ), mock.patch.object(
            modul, "simpan_cache", return_value={"ok": True}
        ):
            ork = modul.Orkestrator(
                client=klien,
                verifikasi_wajib=True,
            )
            hasil = ork.tanya("siapa rektor uir")

        self.assertTrue(hasil["ok"])
        self.assertIn(URL_PIMPINAN, hasil["sumber"])

        status = hasil["status_sumber"][URL_PIMPINAN]
        for tahap in ("retrieved", "read", "evidence", "used", "cited"):
            self.assertTrue(status[tahap], tahap)

        self.assertEqual(status["http"], 200)
        self.assertGreater(status["panjang_teks"], 0)
        self.assertTrue(status["alasan"])
        self.assertIn("Syafrinaldi", hasil["jawaban"])

    def test_jumlah_program_studi_diambil_dari_kalimat_katalog(self):
        teks = (
            "Program Pendidikan\n\n"
            "FAKULTAS DAN PASCASARJANA\n\n"
            "Fakultas Hukum\n\nFakultas Teknik\n\n"
            "Nama Prodi\n\nIlmu Hukum S1 A\n\n"
            + ("program studi " * 4)
            + "\n\nDi Universitas Islam Riau sebagai lembaga pendidikan "
            "yang menganut Catur Darma perguruan tinggi, maka UIR "
            "menyediakan 34 program studi S1 dan 2 program profesi yang "
            "tersebar di 9 fakultas dengan mayoritas UNGGUL dan BAIK "
            "SEKALI pada akreditasinya.\n"
        )
        rekam = _rekam(
            "https://uir.ac.id/program-pendidikan/",
            "Program Pendidikan",
            teks,
        )

        jawaban = modul._jumlah_program_studi_answer(
            [rekam], "Berapa jumlah program studi di UIR?"
        )
        self.assertIsNotNone(jawaban)
        self.assertTrue(
            jawaban.startswith("Berdasarkan halaman resmi UIR:\n- ")
        )
        self.assertIn("34 program studi", jawaban)

        # Angka keseluruhan tidak dipakai untuk pertanyaan fakultas tertentu.
        self.assertIsNone(modul._jumlah_program_studi_answer(
            [rekam], "Berapa program studi Fakultas Teknik di UIR?"
        ))

    def test_berapa_tanpa_angka_tidak_lolos_sebagai_berhasil(self):
        klien = mock.Mock()
        klien.chat.return_value = _respons(
            "Daftar Kuliah di UIR Jangan Asal Pilih Program Studi\n"
            "Saat melakukan Pendaftaran Mahasiswa Baru di UIR, mahasiswa "
            "perlu memahami jenis program studi yang akan dipilih."
        )
        artikel = (
            "https://uir.ac.id/daftar-kuliah-di-uir-jangan-asal-pilih-"
            "program-studi/"
        )
        teks = (
            "Daftar Kuliah di UIR Jangan Asal Pilih Program Studi\n\n"
            "Saat melakukan Pendaftaran Mahasiswa Baru di UIR, mahasiswa "
            "perlu memahami dan mengetahui jenis program studi yang akan "
            "dipilih sesuai minat dan bakat. " * 4
        )

        with mock.patch.object(
            modul, "cari_uir",
            return_value=_hasil_cari([
                _item(
                    artikel,
                    "Daftar Kuliah di UIR",
                    "Informasi program studi.",
                    ("program studi",),
                ),
            ]),
        ), mock.patch.object(
            modul, "baca_cache",
            return_value=_cache(artikel, "Daftar Kuliah di UIR", teks),
        ), mock.patch.object(
            modul, "ambil_halaman", return_value={"ok": False}
        ), mock.patch.object(
            modul, "simpan_cache", return_value={"ok": True}
        ), mock.patch.object(
            modul, "_identitas_answer", return_value=None
        ), mock.patch.object(
            modul, "_beasiswa_answer", return_value=None
        ), mock.patch.object(
            modul, "_prosedur_answer", return_value=None
        ):
            hasil = modul.Orkestrator(
                client=klien, verifikasi_wajib=True
            ).tanya("Berapa jumlah program studi di UIR?")

        self.assertFalse(hasil["ok"])
        self.assertIn(
            hasil["status"],
            ("bukti_tidak_cukup", "sumber_tidak_tersedia"),
        )
        self.assertNotIn("34", hasil["jawaban"])


if __name__ == "__main__":
    unittest.main()
