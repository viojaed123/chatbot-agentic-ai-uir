"""Uji jembatan Hermes Agent API Server tanpa memanggil jaringan."""

import unittest
from unittest import mock

from app import hermes_runtime
from app.hermes_runtime import (
    HermesRuntime,
    HermesTimeout,
    HermesTidakTersedia,
    _model_cocok,
)


class UjiHermesRuntime(unittest.TestCase):
    def setUp(self):
        # Status terverifikasi bersifat lintas instance; kosongkan agar tiap uji
        # tidak mewarisi bukti dari uji lain.
        with hermes_runtime._KUNCI_TERVERIFIKASI:
            hermes_runtime._MODEL_TERVERIFIKASI.update(
                {"model": "", "provider": "", "waktu": 0.0}
            )
        self.runtime = HermesRuntime(
            api_url="http://127.0.0.1:8642",
            api_key="kunci-uji",
            timeout=5,
            poll=0.2,
        )
        self.jawaban = (
            'Visi UIR ada di https://uir.ac.id/profil/ dan '
            'https://uir.ac.id/program-pendidikan/.'
        )
        self.pesan = {
            "data": [
                {
                    "role": "user",
                    "content": "Apa visi UIR?",
                    "tool_name": "",
                    "timestamp": 900,
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "tool_describe"}}],
                    "timestamp": 901,
                },
                {
                    "role": "tool",
                    "tool_name": "tool_describe",
                    "content": '{"tools": {"mcp__uir__cari_uir": {}}}',
                    "timestamp": 902,
                },
                {
                    "role": "tool",
                    "tool_name": "mcp__uir__cari_uir",
                    "content": 'https://uir.ac.id/profil/\\',
                    "timestamp": 1001,
                },
                {
                    "role": "tool",
                    "tool_name": "mcp__uir__ambil_halaman",
                    "content": "https://uir.ac.id/program-pendidikan/",
                    "timestamp": 1002,
                },
            ]
        }
        self.status_selesai = {
            "status": "completed",
            "output": self.jawaban,
            "runtime": {
                "model": "deepseek-v4.1-flash:free",
                "provider": "custom",
            },
            "created_at": 1000,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }

    def _permintaan_berurutan(self, *respons):
        # Daftar exception sebagai side_effect akan dilempar, bukan dikembalikan.
        return mock.Mock(side_effect=list(respons))

    def test_model_cocok_variasi(self):
        self.assertTrue(_model_cocok("deepseek-v4.1-flash:free"))
        self.assertTrue(_model_cocok("deepseek-v4.1-flash"))
        self.assertFalse(_model_cocok("gpt-4"))
        self.assertFalse(_model_cocok(""))

    def test_url_hanya_uir_dan_tanpa_backslash(self):
        url = self.runtime._kumpulkan_url(
            'https://uir.ac.id/profil/\\ lalu https://example.com/halaman'
        )
        self.assertEqual(url, ["https://uir.ac.id/profil/"])

    def test_health_tidak_mengklaim_model_sebelum_inferensi(self):
        with mock.patch.object(self.runtime, "_permintaan", return_value={"status": "ok"}):
            hasil = self.runtime.health()
        self.assertTrue(hasil["ok"])
        self.assertFalse(hasil["model_tersedia"])
        self.assertEqual(hasil["agent_runtime"], "hermes")

    def test_bukti_model_digunakan_health_modul(self):
        hermes_runtime._catat_model_terverifikasi(
            "deepseek-v4.1-flash:free", "custom"
        )
        with mock.patch.object(
            hermes_runtime.HermesRuntime,
            "_permintaan",
            return_value={"status": "ok"},
        ):
            hasil = hermes_runtime.health()
        self.assertTrue(hasil["ok"])
        self.assertTrue(hasil["model_tersedia"])
        self.assertEqual(hasil["model_api"], "deepseek-v4.1-flash:free")

    def test_mulai_run_mengirim_model_dan_instruksi(self):
        with mock.patch.object(
            self.runtime, "_permintaan", return_value={"run_id": "run-1"}
        ) as permintaan:
            run_id = self.runtime._mulai_run("Apa visi UIR?", "uir-sesi-1")
        self.assertEqual(run_id, "run-1")
        args, kwargs = permintaan.call_args
        self.assertEqual(args[0], "/v1/runs")
        self.assertEqual(kwargs["metode"], "POST")
        self.assertEqual(kwargs["session_id"], "uir-sesi-1")
        self.assertEqual(kwargs["data"]["model"], "deepseek-v4.1-flash:free")
        self.assertEqual(kwargs["data"]["provider"], "custom")
        self.assertIn("instructions", kwargs["data"])

    @mock.patch.object(hermes_runtime.time, "sleep")
    def test_tanya_live_tersimulasi_memverifikasi_model_dan_sumber(self, _tidur):
        panggilan = self._permintaan_berurutan(
            {"run_id": "run-1"}, self.status_selesai, self.pesan
        )
        with mock.patch.object(self.runtime, "_permintaan", panggilan):
            hasil = self.runtime.tanya("Apa visi UIR?", "sesi-1")
        self.assertTrue(hasil["ok"])
        self.assertEqual(hasil["runtime"]["model"], "deepseek-v4.1-flash:free")
        self.assertEqual(hasil["langkah"], 2)
        self.assertEqual(
            hasil["sumber_terbaca"],
            ["https://uir.ac.id/profil/", "https://uir.ac.id/program-pendidikan/"],
        )
        self.assertTrue(
            hasil["status_sumber"]["https://uir.ac.id/profil/"]["cited"]
        )
        self.assertTrue(self.runtime.model_terverifikasi)

        with mock.patch.object(
            hermes_runtime.HermesRuntime,
            "_permintaan",
            return_value={"status": "ok"},
        ):
            status_health = hermes_runtime.health()
        self.assertTrue(status_health["model_tersedia"])
        self.assertEqual(status_health["model_api"], "deepseek-v4.1-flash:free")

    @mock.patch.object(hermes_runtime.time, "sleep")
    def test_tanya_menolak_model_yang_tidak_sesuai(self, _tidur):
        status = dict(self.status_selesai)
        status["runtime"] = {"model": "gpt-4", "provider": "custom"}
        panggilan = self._permintaan_berurutan(
            {"run_id": "run-1"}, status, self.pesan
        )
        with mock.patch.object(self.runtime, "_permintaan", panggilan):
            hasil = self.runtime.tanya("Apa visi UIR?", "sesi-1")
        self.assertFalse(hasil["ok"])
        self.assertEqual(hasil["status"], "model_tidak_tersedia")

    @mock.patch.object(hermes_runtime.time, "sleep")
    def test_timeout_dilaporkan_jujur(self, _tidur):
        panggilan = self._permintaan_berurutan(
            {"run_id": "run-1"}, HermesTimeout("lama")
        )
        with mock.patch.object(self.runtime, "_permintaan", panggilan):
            hasil = self.runtime.tanya("Apa visi UIR?", "sesi-1")
        self.assertFalse(hasil["ok"])
        self.assertEqual(hasil["status"], "timeout")
        self.assertEqual(hasil["runtime"]["run_id"], "run-1")

    @mock.patch.object(hermes_runtime.time, "sleep")
    def test_batas_langkah_memberi_pesan_pemulihan(self, _tidur):
        status = {
            "status": "failed",
            "error": "Iteration budget exhausted (5/5)",
            "runtime": {"model": "deepseek-v4.1-flash:free"},
        }
        panggilan = self._permintaan_berurutan({"run_id": "run-1"}, status)
        with mock.patch.object(self.runtime, "_permintaan", panggilan):
            hasil = self.runtime.tanya("Apa visi UIR?", "sesi-1")
        self.assertFalse(hasil["ok"])
        self.assertEqual(hasil["status"], "batas_langkah_tercapai")
        self.assertIn("lebih spesifik", hasil["pesan"])

    @mock.patch.object(hermes_runtime.time, "sleep")
    def test_sesi_berbeda_menghasilkan_header_berbeda(self, _tidur):
        panggilan = self._permintaan_berurutan(
            {"run_id": "run-1"}, self.status_selesai, self.pesan,
            {"run_id": "run-2"}, self.status_selesai, self.pesan,
        )
        with mock.patch.object(self.runtime, "_permintaan", panggilan) as permintaan:
            self.runtime.tanya("Satu?", "sesi-satu")
            self.runtime.tanya("Dua?", "sesi-dua")
        identitas = [
            k.kwargs.get("session_id")
            for k in permintaan.call_args_list
            if k.kwargs.get("session_id")
        ]
        self.assertEqual(identitas[0], "uir-sesi-satu")
        self.assertEqual(identitas[-1], "uir-sesi-dua")
        self.assertNotEqual(identitas[0], identitas[-1])

    def test_sse_mendeteksi_event_terminal(self):
        baris = [
            b'data: {"event":"tool.completed","text":"isi rahasia"}\n',
            b'data: {"event":"run.completed","output":"isi rahasia"}\n',
        ]

        class Tanggapan:
            def __enter__(self):
                return self

            def __exit__(self, *_arg):
                return False

            def __iter__(self):
                return iter(baris)

        with mock.patch.object(
            hermes_runtime.urllib.request, "urlopen", return_value=Tanggapan()
        ) as buka:
            self.assertTrue(self.runtime._tunggu_sse("run-1", "uir-sesi-1"))
        permintaan = buka.call_args.args[0]
        self.assertEqual(permintaan.get_method(), "GET")
        self.assertEqual(permintaan.get_header("Accept"), "text/event-stream")
        self.assertIn("/v1/runs/run-1/events", permintaan.full_url)

    @mock.patch.object(hermes_runtime.time, "sleep")
    def test_sse_gagal_jatuh_ke_polling(self, _tidur):
        with mock.patch.object(
            hermes_runtime.urllib.request, "urlopen", side_effect=OSError("putus")
        ), mock.patch.object(
            self.runtime, "_permintaan", return_value=self.status_selesai
        ) as permintaan:
            status = self.runtime._tunggu_run("run-1", "uir-sesi-1")
        self.assertEqual(status["status"], "completed")
        self.assertGreaterEqual(permintaan.call_count, 1)

    def test_kunci_kosong_ditangani(self):
        kosong = HermesRuntime(api_key="")
        with self.assertRaises(HermesTidakTersedia):
            kosong._permintaan("/health")


if __name__ == "__main__":
    unittest.main()
