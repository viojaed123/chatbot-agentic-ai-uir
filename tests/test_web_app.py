"""Pengujian helper antarmuka web tanpa jaringan dan tanpa model sungguhan."""

import inspect
import json
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

from app.ollama_client import KonfigurasiTidakValid, OllamaTidakTersedia
from web import app as web_app
from web import helpers

URL = "https://uir.ac.id/informasi/beasiswa/"


class _ResponsHTTP:
    def __init__(self, isi):
        self.isi = isi

    def read(self):
        return self.isi

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class UjiWebApp(unittest.TestCase):
    # 1. Aplikasi dapat diimpor
    def test_aplikasi_dapat_diimpor(self):
        self.assertTrue(callable(web_app.main))
        self.assertEqual(len(web_app.CONTOH_PERTANYAAN), 4)
        for pertanyaan in web_app.CONTOH_PERTANYAAN:
            self.assertIsInstance(pertanyaan, str)
            self.assertTrue(pertanyaan.strip())

    # 2. Integrasi orchestrator memakai API yang benar
    def test_integrasi_orchestrator_api(self):
        hasil = {
            "ok": True, "status": "berhasil", "jawaban": "Jawaban UIR.",
            "sumber": [URL], "sumber_terbaca": [URL], "langkah": 2, "pesan": "",
        }
        ork = mock.Mock()
        ork.tanya.return_value = hasil
        catatan = helpers.jalankan_pertanyaan(ork, "  Info beasiswa UIR  ")

        ork.tanya.assert_called_once_with("Info beasiswa UIR")
        self.assertEqual(catatan["content"], "Jawaban UIR.")
        self.assertTrue(catatan["ok"])
        self.assertEqual(catatan["status"], "berhasil")

    # 3. Satu instance orchestrator dipertahankan selama sesi
    def test_satu_instance_orchestrator_dipertahankan(self):
        state = {}
        pembuat = mock.Mock(return_value="ORK-1")
        pemeriksa = mock.Mock(return_value={"ok": True, "model_tersedia": True})
        helpers.siapkan_state(state, pembuat=pembuat, pemeriksa=pemeriksa)
        helpers.siapkan_state(state, pembuat=pembuat, pemeriksa=pemeriksa)

        self.assertEqual(state["orkestrator"], "ORK-1")
        pembuat.assert_called_once()
        pemeriksa.assert_called_once()

    # 4. Riwayat percakapan tidak hilang
    def test_riwayat_percakapan_tidak_hilang(self):
        state = {}
        helpers.siapkan_state(
            state, pembuat=mock.Mock(return_value="ORK"),
            pemeriksa=mock.Mock(return_value={"ok": True}),
        )
        helpers.tambah_pesan(state, "user", "Pertanyaan pertama")
        helpers.tambah_pesan(state, "assistant", "Jawaban pertama", sumber=[URL])
        helpers.tambah_pesan(state, "user", "Pertanyaan kedua")
        helpers.tambah_pesan(state, "assistant", "Jawaban kedua", sumber=[URL])

        self.assertEqual(
            [p["content"] for p in state["pesan"]],
            ["Pertanyaan pertama", "Jawaban pertama", "Pertanyaan kedua", "Jawaban kedua"],
        )
        self.assertEqual(state["pesan"][1]["sumber"], [URL])
        self.assertEqual(state["orkestrator"], "ORK")

    # 5. Tombol percakapan baru mengatur ulang sesi
    def test_chat_baru_mengatur_ulang_sesi(self):
        state = {}
        pembuat = mock.Mock(side_effect=["ORK-1", "ORK-2"])
        helpers.siapkan_state(
            state, pembuat=pembuat,
            pemeriksa=mock.Mock(return_value={"ok": True}),
        )
        helpers.tambah_pesan(state, "user", "Pertanyaan lama")
        helpers.reset_state(
            state, pembuat=pembuat,
            pemeriksa=mock.Mock(return_value={"ok": True, "model_tersedia": True}),
        )

        self.assertEqual(state["pesan"], [])
        self.assertEqual(state["orkestrator"], "ORK-2")

    # 6. Sumber hanya ditampilkan bila tersedia
    def test_sumber_hanya_ditampilkan_jika_tersedia(self):
        self.assertEqual(helpers.sumber_terverifikasi({"sumber_terbaca": []}), [])
        self.assertEqual(helpers.sumber_terverifikasi({}), [])
        self.assertEqual(helpers.sumber_terverifikasi(None), [])
        # Kandidat hasil pencarian BUKAN sumber terverifikasi.
        self.assertEqual(helpers.sumber_terverifikasi({"sumber": [URL]}), [])
        self.assertEqual(
            helpers.sumber_terverifikasi({"sumber_terbaca": [URL, URL]}), [URL]
        )

    # 7. URL eksternal tidak ditampilkan sebagai sumber resmi
    def test_url_eksternal_ditolak(self):
        buruk = [
            "https://example.com/beasiswa",
            "http://uir.ac.id/beasiswa",
            "https://www.uir.ac.id/beasiswa",
            "https://mahasiswa.uir.ac.id/beasiswa",
            "https://uir.ac.id:8443/beasiswa",
            "bukan-url",
        ]
        self.assertEqual(helpers.sumber_terverifikasi({"sumber_terbaca": buruk}), [])
        self.assertEqual(
            helpers.sumber_terverifikasi({"sumber_terbaca": buruk + [URL]}), [URL]
        )

    # 8. Error Ollama ditangani ramah
    def test_error_ollama_ditangani(self):
        ork = mock.Mock()
        ork.tanya.side_effect = OllamaTidakTersedia("detail teknis rahasia")
        catatan = helpers.jalankan_pertanyaan(ork, "Halo")

        self.assertFalse(catatan["ok"])
        self.assertEqual(catatan["status"], "ollama_tidak_tersedia")
        self.assertIn("Pastikan server Ollama", catatan["pesan"])
        self.assertNotIn("detail teknis rahasia", json.dumps(catatan, ensure_ascii=False))

        ork2 = mock.Mock()
        ork2.tanya.side_effect = RuntimeError("Traceback rahasia internal")
        catatan2 = helpers.jalankan_pertanyaan(ork2, "Halo")
        self.assertEqual(catatan2["status"], "kesalahan_internal")
        self.assertNotIn("Traceback", json.dumps(catatan2, ensure_ascii=False))

    # 9. UI tidak mengubah verifikasi bukti orchestrator
    def test_ui_tidak_mengubah_verifikasi_bukti(self):
        hasil = {
            "ok": True, "status": "berhasil", "jawaban": "Teks asli dari orchestrator.",
            "sumber": ["https://uir.ac.id/kandidat-belum-dibaca/"],
            "sumber_terbaca": [URL], "langkah": 3, "pesan": "",
        }
        ork = mock.Mock()
        ork.tanya.return_value = hasil
        catatan = helpers.jalankan_pertanyaan(ork, "Info")

        self.assertEqual(catatan["content"], hasil["jawaban"])
        self.assertEqual(catatan["sumber"], [URL])
        self.assertNotIn("belum-dibaca", catatan["content"])

        kode_ui = inspect.getsource(web_app).lower()
        self.assertNotIn("verifikasi_wajib", kode_ui)
        self.assertNotIn("max_langkah", kode_ui)
        self.assertNotIn("simpan_cache", kode_ui)

    # 10. Tidak ada koneksi model cloud
    def test_tidak_ada_koneksi_model_cloud(self):
        for modul in (web_app, helpers):
            kode = inspect.getsource(modul).lower()
            for penanda in ("openai", "anthropic", "gemini", "claude", "api.cloud"):
                self.assertNotIn(penanda, kode)
        self.assertEqual(helpers.url_tags(), "http://localhost:11434/api/tags")
        with self.assertRaises(KonfigurasiTidakValid):
            helpers.url_tags("https://api.contoh-ai.example/v1/chat")

    # Tambahan: status koneksi benar-benar diperiksa
    def test_status_ollama_diperiksa_secara_nyata(self):
        isi = json.dumps({
            "models": [{"name": "hermes3:8b"}, {"name": "qwen2.5:7b"}]
        }).encode("utf-8")
        with mock.patch.object(
            helpers.urllib.request, "urlopen", return_value=_ResponsHTTP(isi)
        ) as buka:
            status = helpers.cek_ollama()
        self.assertTrue(status["ok"])
        self.assertTrue(status["model_tersedia"])
        self.assertEqual(status["jumlah_model"], 2)
        self.assertEqual(buka.call_args.args[0].full_url, "http://localhost:11434/api/tags")

    def test_model_tidak_tersedia_tidak_dilaporkan_online(self):
        isi = json.dumps({"models": [{"name": "qwen2.5:7b"}]}).encode("utf-8")
        with mock.patch.object(
            helpers.urllib.request, "urlopen", return_value=_ResponsHTTP(isi)
        ):
            status = helpers.cek_ollama()
        self.assertTrue(status["ok"])
        self.assertFalse(status["model_tersedia"])

    def test_ollama_mati_tidak_dilaporkan_online(self):
        with mock.patch.object(
            helpers.urllib.request, "urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            status = helpers.cek_ollama()
        self.assertFalse(status["ok"])
        self.assertFalse(status["model_tersedia"])
        self.assertIn("belum berjalan", status["pesan"])

    def test_timeout_tidak_dilaporkan_online(self):
        with mock.patch.object(
            helpers.urllib.request, "urlopen", side_effect=TimeoutError()
        ):
            status = helpers.cek_ollama()
        self.assertFalse(status["ok"])
        self.assertIn("batas waktu", status["pesan"])

    def test_pesan_status_tersedia(self):
        self.assertIn("Pastikan server Ollama", helpers.pesan_status("ollama_tidak_tersedia"))
        self.assertIn("belum tersedia pada penyedia", helpers.pesan_status("model_tidak_tersedia"))
        self.assertIn("batas waktu", helpers.pesan_status("timeout"))
        self.assertIn("belum berhasil diverifikasi", helpers.pesan_status("sumber_tidak_tersedia"))

    def test_pesan_status_penolakan_saling_berbeda(self):
        statuses = (
            "sumber_tidak_tersedia",
            "bukti_tidak_cukup",
            "tool_tidak_dikenal",
            "batas_langkah",
            "batas_langkah_tercapai",
        )
        pesan = {
            status: helpers.pesan_status(status) for status in statuses
        }
        self.assertEqual(len(set(pesan.values())), len(statuses))
        self.assertIn("bukti", pesan["bukti_tidak_cukup"].lower())
        self.assertIn("tool", pesan["tool_tidak_dikenal"].lower())
        self.assertIn("batas", pesan["batas_langkah"].lower())
        self.assertIn("batas", pesan["batas_langkah_tercapai"].lower())

    # Uji langsung antarmuka: menjalankan web/app.py sungguhan dengan AppTest.
    # Patch ditujukan ke web.helpers karena AppTest mengeksekusi ulang file
    # sebagai script __main__ dan mengimpor helper dari modul tersebut.
    def test_apptest_render_dan_interaksi_langsung(self):
        status = {
            "ok": True,
            "model": "deepseek-v4.1-flash:free",
            "model_api": "deepseek-v4.1-flash:free",
            "model_tersedia": True,
            "penyedia": "tokenharbor",
            "lokal": False,
            "pesan": "Penyedia terhubung; model deepseek-v4.1-flash:free tersedia.",
        }
        rekam = {
            "content": "Jawaban uji dari orchestrator.",
            "sumber": [URL], "status": "berhasil", "ok": True, "pesan": "",
        }
        berkas = Path(__file__).resolve().parents[1] / "web" / "app.py"
        with mock.patch("web.helpers.cek_model", return_value=status), \
             mock.patch("web.helpers.jalankan_pertanyaan", return_value=rekam) as panggil:
            at = AppTest.from_file(str(berkas), default_timeout=30).run()

            self.assertEqual(at.exception, [])
            self.assertEqual(len(at.button), 6)
            self.assertEqual(len(at.chat_input), 1)
            self.assertEqual([s.value for s in at.sidebar.success],
                             ["TokenHarbor (DeepSeek): terhubung"])

            at.button[0].click().run()
            self.assertEqual(at.exception, [])
            self.assertEqual(panggil.call_count, 1)
            self.assertEqual(
                [(p["role"], p["content"]) for p in at.session_state["pesan"]],
                [("user", web_app.CONTOH_PERTANYAAN[0]),
                 ("assistant", "Jawaban uji dari orchestrator.")],
            )
            self.assertTrue(any(URL in m.value for m in at.markdown))
            self.assertEqual(len(at.chat_message), 2)

            at.chat_input[0].set_value("Pertanyaan kedua?").run()
            self.assertEqual(at.exception, [])
            self.assertEqual(panggil.call_count, 2)
            self.assertEqual(len(at.session_state["pesan"]), 4)

            at.sidebar.button[1].click().run()  # Percakapan baru
            self.assertEqual(at.exception, [])
            self.assertEqual(at.session_state["pesan"], [])


if __name__ == "__main__":
    unittest.main()
