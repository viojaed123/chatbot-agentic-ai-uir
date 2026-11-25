"""Regresi guard pertanyaan lanjutan dan isolasi sesi.

Pertanyaan seperti "Dari jawaban sebelumnya, sebutkan jabatannya saja."
tidak mengulang kata UIR/topik. Guard luar-cakupan hanya boleh dikendurkan
bila sesi yang sama memang memiliki jawaban UIR sebelumnya yang sah.
"""

import unittest

from app import orchestrator as modul


PERTANYAAN_LANJUTAN = (
    "Dari jawaban sebelumnya, sebutkan jabatannya saja."
)
PERTANYAAN_LUAR = "Bagaimana cuaca di kota saya hari ini?"


def _riwayat_sah():
    return [
        {"role": "system", "content": "petunjuk sistem"},
        {"role": "user", "content": "Siapa rektor UIR?"},
        {
            "role": "assistant",
            "content": "Rektor Universitas Islam Riau adalah seorang profesor. "
                       "(Sumber: https://uir.ac.id/profil/pimpinan-uir/)",
        },
    ]


class UjiPertanyaanLanjutan(unittest.TestCase):
    def _orkestrator(self, riwayat=None):
        ork = modul.Orkestrator(
            client=object(),
            verifikasi_wajib=True,
            jalur_cepat=False,
        )
        if riwayat is not None:
            ork.riwayat = riwayat
        return ork

    def test_lanjutan_dengan_jawaban_sesi_sama_diizinkan(self):
        ork = self._orkestrator(_riwayat_sah())
        ork.pertanyaan = PERTANYAAN_LANJUTAN
        ork._awal_giliran = len(ork.riwayat)

        self.assertTrue(modul._adalah_pertanyaan_lanjutan(ork.pertanyaan))
        self.assertTrue(modul._adalah_pertanyaan_di_luar_cakupan(ork.pertanyaan))
        self.assertTrue(ork._ada_jawaban_terverifikasi_sebelumnya())

    def test_lanjutan_tanpa_riwayat_tetap_ditolak(self):
        ork = self._orkestrator()
        ork.pertanyaan = PERTANYAAN_LANJUTAN
        ork._awal_giliran = len(ork.riwayat)

        self.assertFalse(ork._ada_jawaban_terverifikasi_sebelumnya())

    def test_riwayat_sesi_lain_tidak_dipakai(self):
        sesi_a = self._orkestrator(_riwayat_sah())
        sesi_a.pertanyaan = PERTANYAAN_LANJUTAN
        sesi_a._awal_giliran = len(sesi_a.riwayat)
        sesi_b = self._orkestrator()
        sesi_b.pertanyaan = PERTANYAAN_LANJUTAN
        sesi_b._awal_giliran = len(sesi_b.riwayat)

        self.assertTrue(sesi_a._ada_jawaban_terverifikasi_sebelumnya())
        self.assertFalse(sesi_b._ada_jawaban_terverifikasi_sebelumnya())

    def test_penolakan_sebelumnya_bukan_konteks_sah(self):
        riwayat = _riwayat_sah()
        riwayat[-1] = {
            "role": "assistant",
            "content": "Maaf, saya belum dapat memastikan jawaban tersebut "
                       "dari isi halaman resmi UIR yang berhasil dibaca.",
        }
        ork = self._orkestrator(riwayat)
        ork.pertanyaan = PERTANYAAN_LANJUTAN
        ork._awal_giliran = len(ork.riwayat)

        self.assertFalse(ork._ada_jawaban_terverifikasi_sebelumnya())

    def test_klarifikasi_sebelumnya_bukan_konteks_sah(self):
        riwayat = _riwayat_sah()
        riwayat[-1] = {
            "role": "assistant",
            "content": "Maksud Anda salah satu dari informasi mana?",
        }
        ork = self._orkestrator(riwayat)
        ork.pertanyaan = PERTANYAAN_LANJUTAN
        ork._awal_giliran = len(ork.riwayat)

        self.assertFalse(ork._ada_jawaban_terverifikasi_sebelumnya())

    def test_rujukan_topik_diambil_dari_sesi_sama(self):
        ork = self._orkestrator(_riwayat_sah())
        ork.pertanyaan = PERTANYAAN_LANJUTAN
        ork._awal_giliran = len(ork.riwayat)

        rujukan = ork._pertanyaan_rujukan()

        self.assertEqual(rujukan, "Siapa rektor UIR?")
        self.assertIn("pimpinan", modul._topik_pertanyaan(rujukan))

    def test_rujukan_pertanyaan_baru_tetap_diri_sendiri(self):
        ork = self._orkestrator(_riwayat_sah())
        ork.pertanyaan = "Apa saja fakultas di UIR?"
        ork._awal_giliran = len(ork.riwayat)

        self.assertEqual(ork._pertanyaan_rujukan(), ork.pertanyaan)

    def test_pertanyaan_baru_luar_cakupan_tetap_ditolak(self):
        ork = self._orkestrator(_riwayat_sah())
        dipanggil = []

        ork._cycle = lambda: dipanggil.append("cycle") or {
            "ok": True,
            "status": "berhasil",
        }

        hasil = ork.tanya(PERTANYAAN_LUAR)

        self.assertEqual(dipanggil, [])
        self.assertEqual(hasil["status"], "bukti_tidak_cukup")
        self.assertFalse(hasil["ok"])

    def test_lanjutan_sah_mencapai_agen(self):
        ork = self._orkestrator(_riwayat_sah())
        dipanggil = []

        ork._perlu_bootstrap_evidence = lambda: False

        def palsu():
            dipanggil.append("cycle")
            return {"ok": True, "status": "berhasil"}

        ork._cycle = palsu

        hasil = ork.tanya(PERTANYAAN_LANJUTAN)

        self.assertEqual(dipanggil, ["cycle"])
        self.assertEqual(hasil["status"], "berhasil")


if __name__ == "__main__":
    unittest.main()
