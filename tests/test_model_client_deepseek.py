"""Uji adapter penyedia OpenAI-compatible (TokenHarbor) tanpa jaringan nyata."""

import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from app import model_client, telemetri
from app.model_client import (
    DeepSeekClient,
    KonfigurasiTidakValid,
    KunciTidakTersedia,
    ModelTidakTersedia,
    ModelTimeout,
    PenyediaTidakTersedia,
    ResponsTidakValid,
)

KUNCI_TIRUAN = "kunci-tiruan-untuk-test"
MODEL = "deepseek-v4.1-flash:free"
TOKENHARBOR = "https://tokenharbor.ai/v1"
DEEPSEEK_RESMI = "https://api.deepseek.com"


class _ResponsHTTP:
    def __init__(self, isi, status=200):
        self._isi = json.dumps(isi).encode("utf-8")
        self.status = status

    def read(self):
        return self._isi

    def __enter__(self):
        return self

    def __exit__(self, *arg):
        return False


def _respons_chat(konten="hai", model=MODEL, **tambahan):
    isi = {
        "choices": [{"message": {"role": "assistant", "content": konten}}],
        "model": model,
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    isi.update(tambahan)
    return isi


class UjiKonfigurasiPenyedia(unittest.TestCase):
    def test_kunci_kosong_ditolak(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            with self.assertRaises(KunciTidakTersedia):
                DeepSeekClient()

    def test_base_url_http_eksternal_ditolak(self):
        with self.assertRaises(KonfigurasiTidakValid):
            model_client._validasi_base_url("http://tokenharbor.ai/v1")

    def test_base_url_tidak_boleh_muat_kredensial(self):
        with self.assertRaises(KonfigurasiTidakValid):
            model_client._validasi_base_url("https://user:pass@tokenharbor.ai/v1")

    def test_base_url_lokal_diizinkan_untuk_pengujian(self):
        self.assertEqual(
            model_client._validasi_base_url("http://127.0.0.1:9999/"),
            "http://127.0.0.1:9999",
        )

    def test_kunci_tidak_muncul_di_repr(self):
        klien = DeepSeekClient(base_url=TOKENHARBOR, api_key=KUNCI_TIRUAN)
        self.assertNotIn(KUNCI_TIRUAN, repr(klien))
        self.assertIn("tokenharbor", repr(klien))

    def test_default_tokenharbor(self):
        self.assertEqual(model_client.PROVIDER, "deepseek")
        self.assertEqual(model_client.DEEPSEEK_BASE_URL, TOKENHARBOR)
        self.assertEqual(model_client.DEEPSEEK_MODEL, MODEL)

    def test_penyedia_terdeteksi_dari_host(self):
        self.assertEqual(model_client._nama_penyedia(TOKENHARBOR), "tokenharbor")
        self.assertEqual(model_client._nama_penyedia(DEEPSEEK_RESMI), "deepseek")

    def test_penyedia_aktif_tokenharbor_dan_rollback(self):
        self.assertEqual(model_client.penyedia_aktif(), "tokenharbor")
        with mock.patch.object(model_client, "PROVIDER", "ollama"):
            self.assertEqual(model_client.penyedia_aktif(), "ollama")

    def test_model_cocok_variant_free(self):
        self.assertTrue(model_client._model_cocok(MODEL, MODEL))
        self.assertTrue(model_client._model_cocok(MODEL, "DeepSeek-V4.1-Flash"))
        self.assertFalse(model_client._model_cocok(MODEL, "hermes3:8b"))
        self.assertTrue(model_client._model_cocok(MODEL, ""))

    def test_model_gratis_tanpa_estimasi_biaya(self):
        self.assertEqual(
            model_client._estimasi_biaya(MODEL, {
                "prompt_tokens": 1000, "completion_tokens": 1000,
            }),
            0.0,
        )

    def test_endpoint_model_dan_endpoint_saldo(self):
        self.assertEqual(
            model_client._endpoint_model(TOKENHARBOR),
            "https://tokenharbor.ai/v1/models",
        )
        self.assertEqual(
            model_client._endpoint_chat(TOKENHARBOR),
            "https://tokenharbor.ai/v1/chat/completions",
        )
        self.assertEqual(
            model_client._endpoint_saldo(DEEPSEEK_RESMI),
            "https://api.deepseek.com/user/balance",
        )


class UjiTransformasiPesan(unittest.TestCase):
    def test_tool_call_dan_tool_result_mempunyai_id_yang_cocok(self):
        pesan = [
            {"role": "system", "content": "instruksi"},
            {"role": "user", "content": "cari fakultas"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": "cari_uir",
                        "arguments": '{"query": "fakultas"}',
                    }
                }],
            },
            {
                "role": "tool",
                "tool_name": "cari_uir",
                "content": '{"ok": true}',
            },
        ]

        hasil = model_client._ke_deepseek(pesan)
        panggilan = hasil[2]["tool_calls"][0]

        self.assertEqual(panggilan["id"], "call_2_0")
        self.assertEqual(panggilan["type"], "function")
        self.assertEqual(panggilan["function"]["name"], "cari_uir")
        self.assertEqual(
            json.loads(panggilan["function"]["arguments"]),
            {"query": "fakultas"},
        )
        self.assertEqual(hasil[3]["tool_call_id"], "call_2_0")
        self.assertEqual(hasil[3]["role"], "tool")


class UjiDeepSeekClient(unittest.TestCase):
    def _klien(self, base_url=TOKENHARBOR):
        return DeepSeekClient(
            base_url=base_url,
            model=MODEL,
            api_key=KUNCI_TIRUAN,
            max_retry=0,
        )

    def test_payload_memakai_format_openai_compatible(self):
        klien = self._klien()
        tangkapan = {}

        def palsu(permintaan, timeout=None):
            tangkapan["permintaan"] = permintaan
            tangkapan["timeout"] = timeout
            return _ResponsHTTP(_respons_chat())

        with mock.patch.object(model_client.urllib.request, "urlopen", palsu):
            hasil = klien.chat(
                [{"role": "user", "content": "halo"}],
                tools=[{"type": "function", "function": {"name": "cari_uir"}}],
            )

        isi = json.loads(tangkapan["permintaan"].data.decode("utf-8"))
        self.assertEqual(isi["model"], MODEL)
        # TokenHarbor adalah proxy OpenAI-compatible: tidak mengirim ``thinking``.
        self.assertNotIn("thinking", isi)
        self.assertTrue(isi["tools"])
        self.assertEqual(isi["max_tokens"], model_client.DEEPSEEK_MAX_TOKENS_TOOL)
        self.assertEqual(isi["stream"], False)
        self.assertEqual(isi["temperature"], 0)
        self.assertIn(
            "Bearer " + KUNCI_TIRUAN,
            tangkapan["permintaan"].headers["Authorization"],
        )
        self.assertEqual(hasil["message"]["content"], "hai")
        self.assertEqual(hasil["model_dari_api"], MODEL)

    def test_payload_deepseek_resmi_memakai_thinking(self):
        klien = self._klien(DEEPSEEK_RESMI)
        tangkapan = {}

        def palsu(permintaan, timeout=None):
            tangkapan["permintaan"] = permintaan
            return _ResponsHTTP(_respons_chat())

        with mock.patch.object(model_client.urllib.request, "urlopen", palsu):
            klien.chat([{"role": "user", "content": "halo"}])

        isi = json.loads(tangkapan["permintaan"].data.decode("utf-8"))
        self.assertEqual(isi["thinking"], {"type": "disabled"})

    def test_reasoning_content_tidak_diteruskan(self):
        klien = self._klien()
        isi = _respons_chat(konten="jawaban akhir")
        isi["choices"][0]["message"]["reasoning_content"] = (
            "rangkuman internal yang tidak boleh tampil"
        )
        isi["choices"][0]["message"]["tool_calls"] = [{
            "id": "call_abc",
            "type": "function",
            "function": {
                "name": "baca_cache",
                "arguments": '{"url": "https://uir.ac.id/"}',
            },
        }]

        with mock.patch.object(
            model_client.urllib.request, "urlopen",
            return_value=_ResponsHTTP(isi),
        ):
            hasil = klien.chat([{"role": "user", "content": "uji"}])

        pesan = hasil["message"]
        self.assertNotIn("reasoning_content", pesan)
        self.assertNotIn("rangkuman internal", json.dumps(pesan))
        self.assertEqual(pesan["tool_calls"][0]["id"], "call_abc")

    def test_respons_tanpa_choices_ditolak(self):
        klien = self._klien()
        with mock.patch.object(
            model_client.urllib.request, "urlopen",
            return_value=_ResponsHTTP({"model": MODEL}),
        ):
            with self.assertRaises(ResponsTidakValid):
                klien.chat([{"role": "user", "content": "uji"}])

    def test_model_dari_api_harus_cocok(self):
        klien = self._klien()
        with mock.patch.object(
            model_client.urllib.request, "urlopen",
            return_value=_ResponsHTTP(_respons_chat(model="model-lain")),
        ):
            with self.assertRaises(ModelTidakTersedia):
                klien.chat([{"role": "user", "content": "uji"}])

    def test_model_variant_tanpa_free_diterima(self):
        klien = self._klien()
        with mock.patch.object(
            model_client.urllib.request, "urlopen",
            return_value=_ResponsHTTP(_respons_chat(model="deepseek-v4.1-flash")),
        ):
            hasil = klien.chat([{"role": "user", "content": "uji"}])
        self.assertEqual(hasil["model_dari_api"], "deepseek-v4.1-flash")

    def test_http_401_dipetakan_ke_kunci(self):
        klien = self._klien()
        galat = urllib.error.HTTPError(
            TOKENHARBOR + "/chat/completions",
            401, "Unauthorized", {}, io.BytesIO(b"")
        )
        with mock.patch.object(
            model_client.urllib.request, "urlopen", side_effect=galat
        ):
            with self.assertRaises(KunciTidakTersedia):
                klien.chat([{"role": "user", "content": "uji"}])

    def test_retry_terbatas(self):
        klien = DeepSeekClient(
            base_url=TOKENHARBOR,
            model=MODEL,
            api_key=KUNCI_TIRUAN,
            max_retry=1,
        )
        galat = urllib.error.HTTPError(
            TOKENHARBOR + "/chat/completions",
            429, "Too Many Requests", {}, io.BytesIO(b"")
        )
        with mock.patch.object(
            model_client.urllib.request, "urlopen", side_effect=galat
        ) as urlopen, mock.patch.object(model_client.time, "sleep"):
            with self.assertRaises(PenyediaTidakTersedia):
                klien.chat([{"role": "user", "content": "uji"}])
        self.assertEqual(urlopen.call_count, 2)

    def test_timeout_dipetakan(self):
        klien = self._klien()
        with mock.patch.object(
            model_client.urllib.request, "urlopen",
            side_effect=TimeoutError("timeout"),
        ):
            with self.assertRaises(ModelTimeout):
                klien.chat([{"role": "user", "content": "uji"}])

    def test_telemetri_mencatat_api_tanpa_kredensial(self):
        telemetri.mulai()
        try:
            klien = self._klien()
            isi = _respons_chat()
            isi["usage"] = {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_cache_hit_tokens": 10,
            }
            with mock.patch.object(
                model_client.urllib.request, "urlopen",
                return_value=_ResponsHTTP(isi),
            ):
                klien.chat([{"role": "user", "content": "uji"}])

            ringkasan = telemetri.selesai()
            self.assertEqual(ringkasan["api"]["penyedia"], "tokenharbor")
            self.assertEqual(ringkasan["api"]["model"], MODEL)
            self.assertEqual(ringkasan["api"]["panggilan"], 1)
            self.assertEqual(ringkasan["api"]["token_prompt"], 100)
            self.assertEqual(ringkasan["api"]["biaya_estimasi_usd"], 0.0)
            self.assertNotIn(KUNCI_TIRUAN, json.dumps(ringkasan))
        finally:
            telemetri.selesai()


class UjiCekKoneksiPenyedia(unittest.TestCase):
    def test_tanpa_kunci_tidak_memanggil_jaringan(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}), \
             mock.patch.object(model_client.urllib.request, "urlopen") as urlopen:
            hasil = model_client.cek_koneksi()
        self.assertFalse(hasil["ok"])
        self.assertFalse(hasil["model_tersedia"])
        self.assertEqual(hasil["model_api"], "")
        urlopen.assert_not_called()

    def test_model_tersedia_dari_endpoint_models(self):
        tangkapan = {}

        def palsu(permintaan, timeout=None):
            tangkapan["url"] = permintaan.full_url
            tangkapan["auth"] = permintaan.headers["Authorization"]
            return _ResponsHTTP({"data": [
                {"id": "model-lain"},
                {"id": MODEL, "owned_by": "deepseek"},
            ]})

        with mock.patch.object(model_client.urllib.request, "urlopen", palsu):
            hasil = model_client.cek_koneksi(api_key=KUNCI_TIRUAN)

        self.assertTrue(hasil["ok"])
        self.assertTrue(hasil["model_tersedia"])
        self.assertEqual(hasil["model_api"], MODEL)
        self.assertEqual(hasil["penyedia"], "tokenharbor")
        self.assertFalse(hasil["lokal"])
        self.assertEqual(hasil["jumlah_model"], 2)
        self.assertEqual(tangkapan["url"], TOKENHARBOR + "/models")
        self.assertEqual(tangkapan["auth"], "Bearer " + KUNCI_TIRUAN)
        self.assertNotIn(KUNCI_TIRUAN, json.dumps(hasil))

    def test_model_tidak_tersedia_di_penyedia(self):
        with mock.patch.object(
            model_client.urllib.request, "urlopen",
            return_value=_ResponsHTTP({"data": [{"id": "hermes3:8b"}]}),
        ):
            hasil = model_client.cek_koneksi(api_key=KUNCI_TIRUAN)
        self.assertTrue(hasil["ok"])
        self.assertFalse(hasil["model_tersedia"])
        self.assertEqual(hasil["model_api"], "")
        self.assertIn(MODEL, hasil["pesan"])

    def test_model_variant_tanpa_free_diterima(self):
        with mock.patch.object(
            model_client.urllib.request, "urlopen",
            return_value=_ResponsHTTP({"models": ["deepseek-v4.1-flash"]}),
        ):
            hasil = model_client.cek_koneksi(api_key=KUNCI_TIRUAN)
        self.assertTrue(hasil["model_tersedia"])
        self.assertEqual(hasil["model_api"], "deepseek-v4.1-flash")

    def test_id_persis_diutamakan_dari_varian_tanpa_free(self):
        with mock.patch.object(
            model_client.urllib.request, "urlopen",
            return_value=_ResponsHTTP({"data": [
                {"id": "deepseek-v4.1-flash"},
                {"id": MODEL},
            ]}),
        ):
            hasil = model_client.cek_koneksi(api_key=KUNCI_TIRUAN)
        self.assertTrue(hasil["model_tersedia"])
        self.assertEqual(hasil["model_api"], MODEL)

    def test_http_401_tidak_membocorkan_kredensial(self):
        galat = urllib.error.HTTPError(
            TOKENHARBOR + "/models", 401, "Unauthorized", {}, io.BytesIO(b"")
        )
        with mock.patch.object(
            model_client.urllib.request, "urlopen", side_effect=galat
        ):
            hasil = model_client.cek_koneksi(api_key=KUNCI_TIRUAN)
        self.assertFalse(hasil["ok"])
        self.assertFalse(hasil["model_tersedia"])
        self.assertNotIn(KUNCI_TIRUAN, json.dumps(hasil))

    def test_fallback_saldo_tersedia(self):
        galat = urllib.error.HTTPError(
            DEEPSEEK_RESMI + "/models", 404, "Not Found", {}, io.BytesIO(b"")
        )
        dengan = [galat, _ResponsHTTP({"is_available": True})]
        with mock.patch.object(
            model_client.urllib.request, "urlopen", side_effect=dengan
        ):
            hasil = model_client.cek_koneksi(
                base_url=DEEPSEEK_RESMI, api_key=KUNCI_TIRUAN
            )
        self.assertTrue(hasil["ok"])
        self.assertTrue(hasil["saldo_tersedia"])
        self.assertEqual(hasil["penyedia"], "deepseek")


class UjiPemilihanProvider(unittest.TestCase):
    def test_default_provider_deepseek_memakai_tokenharbor(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": KUNCI_TIRUAN}):
            klien = model_client.buat_client()
        self.assertIsInstance(klien, DeepSeekClient)
        self.assertTrue(klien.mode_produksi)
        self.assertEqual(klien.penyedia, "tokenharbor")

    def test_rollback_ollama_lewat_konfigurasi(self):
        with mock.patch.object(model_client, "PROVIDER", "ollama"), \
             mock.patch.object(model_client, "OllamaClient") as ollama:
            klien = model_client.buat_client()
        ollama.assert_called_once_with()
        self.assertIs(klien, ollama.return_value)

    def test_provider_tidak_dikenal_ditolak(self):
        with mock.patch.object(model_client, "PROVIDER", "tidak_ada"):
            with self.assertRaises(model_client.KonfigurasiTidakValid):
                model_client.buat_client()

    def test_orkestrator_memakai_client_terpusat(self):
        from app.orchestrator import Orkestrator

        klien = DeepSeekClient(api_key=KUNCI_TIRUAN)
        with mock.patch("app.orchestrator.buat_client", return_value=klien) as buat:
            ork = Orkestrator()
        buat.assert_called_once_with()
        self.assertIs(ork.client, klien)
        self.assertTrue(ork.verifikasi_wajib)
        self.assertTrue(ork.jalur_cepat)


if __name__ == "__main__":
    unittest.main()
