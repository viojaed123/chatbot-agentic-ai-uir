"""Pengujian instrumentasi telemetri tanpa jaringan dan tanpa Ollama."""

import threading
import time
import unittest

from app import telemetri


class UjiTelemetri(unittest.TestCase):
    def test_tanpa_sesi_tidak_menyimpan_apa_pun(self):
        with telemetri.ukur("model"):
            telemetri.tambah("panggilan_model")
        telemetri.catat_tool("cari_uir", "ditemukan")
        telemetri.catat_cache("ditemukan")
        telemetri.catat_token(10, 5)

        self.assertEqual(telemetri.selesai(), {})

    def test_ringkasan_memiliki_field_yang_dibutuhkan(self):
        telemetri.mulai()

        with telemetri.ukur("model"):
            time.sleep(0.01)
        telemetri.tambah("panggilan_model")
        telemetri.tambah("langkah", 2)
        telemetri.catat_tool("baca_cache", "ditemukan")
        telemetri.catat_cache("ditemukan")
        telemetri.catat_token(120, 30)

        hasil = telemetri.selesai()

        for kunci in (
            "total_detik", "tahapan", "paling_lambat",
            "panggilan_model", "langkah", "tool", "cache", "token",
        ):
            self.assertIn(kunci, hasil)

        self.assertEqual(hasil["panggilan_model"], 1)
        self.assertEqual(hasil["langkah"], 2)
        self.assertEqual(
            {k: hasil["cache"][k] for k in ("hit", "miss")},
            {"hit": 1, "miss": 0},
        )
        self.assertEqual(hasil["token"]["total"], 150)
        self.assertGreaterEqual(hasil["tahapan"]["model"]["jumlah"], 1)

    def test_tidak_menyimpan_isi_percakapan(self):
        telemetri.mulai()
        telemetri.catat_tool("cari_uir", "ditemukan")
        hasil = telemetri.selesai()

        self.assertNotIn("pertanyaan", hasil)
        self.assertNotIn("jawaban", hasil)
        self.assertNotIn("teks", hasil)

    def test_rincian_memisahkan_setiap_panggilan_model(self):
        telemetri.mulai()
        telemetri.tandai("api_terima")

        for nomor in (1, 2, 3):
            with telemetri.ukur(
                "model", panggilan_ke=nomor, fase="utama"
            ):
                time.sleep(0.01)

        hasil = telemetri.selesai()
        rincian = [
            item for item in hasil["rincian"]
            if item["tahap"] == "model"
        ]

        self.assertEqual(len(rincian), 3)
        self.assertEqual(
            [item["panggilan_ke"] for item in rincian],
            [1, 2, 3],
        )
        self.assertEqual([item["ke"] for item in rincian], [1, 2, 3])
        self.assertIn("api_terima", hasil["penanda"])

    def test_durasi_tool_dipisah_per_nama(self):
        telemetri.mulai()

        for nama, jeda in (("cari_uir", 0.01), ("baca_cache", 0.02)):
            with telemetri.ukur("tool", tool=nama, alat_ke=1):
                time.sleep(jeda)

        hasil = telemetri.selesai()

        self.assertEqual(
            set(hasil["per_tool"]), {"cari_uir", "baca_cache"}
        )
        self.assertEqual(hasil["per_tool"]["cari_uir"]["jumlah"], 1)
        self.assertEqual(hasil["per_tool"]["baca_cache"]["jumlah"], 1)
        rincian = [item for item in hasil["rincian"] if item["tahap"] == "tool"]
        self.assertEqual(
            [item["tool"] for item in rincian],
            ["cari_uir", "baca_cache"],
        )

    def test_detail_prompt_diisi_dari_dalam_blok(self):
        telemetri.mulai()
        info = {}

        with telemetri.ukur("prompt", detail=info):
            info["karakter"] = 1234
            info["pesan"] = 5

        hasil = telemetri.selesai()
        prompt = hasil["rincian"][0]

        self.assertEqual(prompt["karakter"], 1234)
        self.assertEqual(prompt["pesan"], 5)
        for nilai in prompt.values():
            self.assertIsInstance(nilai, (str, int, float, bool))

    def test_sesi_antar_thread_terpisah(self):
        hasil = {}
        penghalang = threading.Barrier(2)

        def pekerja(nama, jeda):
            penghalang.wait()
            telemetri.mulai()
            with telemetri.ukur("model"):
                time.sleep(jeda)
            telemetri.tambah("panggilan_model")
            hasil[nama] = telemetri.selesai()

        utas1 = threading.Thread(target=pekerja, args=("cepat", 0.01))
        utas2 = threading.Thread(target=pekerja, args=("lambat", 0.06))
        utas1.start()
        utas2.start()
        utas1.join()
        utas2.join()

        self.assertEqual(hasil["cepat"]["panggilan_model"], 1)
        self.assertEqual(hasil["lambat"]["panggilan_model"], 1)
        self.assertGreaterEqual(
            hasil["lambat"]["tahapan"]["model"]["total_detik"],
            hasil["cepat"]["tahapan"]["model"]["total_detik"],
        )


if __name__ == "__main__":
    unittest.main()
