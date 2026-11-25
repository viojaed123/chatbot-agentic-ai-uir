"""Uji pengaman stabilisasi: argumen tool, retry, dan status galat Hermes."""

import socket
import unittest
import urllib.error
from unittest import mock

from fastapi.testclient import TestClient

from app import hermes_runtime
from app.hermes_runtime import HermesRuntime, HermesTidakTersedia, HermesTimeout
from app.system_prompt import SYSTEM_PROMPT
import mcp_uir_server as server
from web.api import create_app


class _RuntimePalsu:
    def __init__(self, kode):
        self.kode = kode

    def tanya(self, pertanyaan, session_id):
        return {
            "ok": False,
            "status": self.kode,
            "pesan": "galat uji",
            "jawaban": "",
            "sumber": [],
            "status_sumber": {},
            "langkah": 0,
            "runtime": {"agent": "hermes", "run_id": ""},
        }


def _http_error(kode, retry_after=None):
    kepala = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError(
        "http://127.0.0.1:8642/v1/runs", kode, "galat uji", kepala, None
    )


def _respons_json(isi=b'{"status":"ok"}'):
    respons = mock.MagicMock()
    respons.__enter__.return_value.read.return_value = isi
    return respons


class UjiPromptStabilisasi(unittest.TestCase):
    def test_prompt_melarang_tool_describe_dan_argumen_kosong(self):
        self.assertIn("Jangan memanggil tool_describe", SYSTEM_PROMPT)
        self.assertIn("Argumen WAJIB", SYSTEM_PROMPT)
        self.assertIn("missing required argument", SYSTEM_PROMPT)
        self.assertIn("Maksimal SATU cari_uir", SYSTEM_PROMPT)
        self.assertIn('"query":"kata kunci singkat"', SYSTEM_PROMPT)

    def test_deskripsi_mcp_menyebut_argumen_wajib(self):
        alat = {x.name: x for x in server.mcp._tool_manager.list_tools()}
        for nama in ("cari_uir", "ambil_halaman", "baca_cache"):
            self.assertIn("Argumen WAJIB", alat[nama].description)
        self.assertIn("query", alat["cari_uir"].description)
        self.assertIn("url", alat["baca_cache"].description)


class UjiRetryHermes(unittest.TestCase):
    def setUp(self):
        self.runtime = HermesRuntime(api_key="kunci-uji", timeout=2, poll=0.1)

    def test_detik_tunggu_dibatasi_dan_aman(self):
        self.assertEqual(HermesRuntime._detik_tunggu("120"), 60)
        self.assertEqual(HermesRuntime._detik_tunggu("abc"), 0)
        self.assertEqual(HermesRuntime._detik_tunggu(None), 0)

    def test_get_429_diulang_then_berhasil(self):
        with mock.patch.object(
            hermes_runtime.urllib.request, "urlopen",
            side_effect=[_http_error(429, 1), _respons_json()],
        ) as urlopen, mock.patch.object(hermes_runtime.time, "sleep") as tidur:
            hasil = self.runtime._permintaan("/health")
        self.assertEqual(hasil, {"status": "ok"})
        self.assertEqual(urlopen.call_count, 2)
        tidur.assert_called_once()
        self.assertLessEqual(tidur.call_args.args[0], 5)

    def test_post_429_tidak_diulang(self):
        with mock.patch.object(
            hermes_runtime.urllib.request, "urlopen", side_effect=_http_error(429)
        ) as urlopen, mock.patch.object(hermes_runtime.time, "sleep") as tidur:
            with self.assertRaises(HermesTidakTersedia) as tangkap:
                self.runtime._permintaan("/v1/runs", data={"input": "x"}, metode="POST")
        self.assertEqual(urlopen.call_count, 1)
        tidur.assert_not_called()
        self.assertEqual(tangkap.exception.kode, "rate_limit")

    def test_get_5xx_habis_retry_dengan_kode_jelas(self):
        with mock.patch.object(
            hermes_runtime.urllib.request, "urlopen",
            side_effect=[_http_error(500), _http_error(502), _http_error(503)],
        ) as urlopen, mock.patch.object(hermes_runtime.time, "sleep"):
            with self.assertRaises(HermesTidakTersedia) as tangkap:
                self.runtime._permintaan("/health")
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(tangkap.exception.kode, "hermes_5xx")

    def test_get_timeout_diulang_lalu_gagal_jujur(self):
        with mock.patch.object(
            hermes_runtime.urllib.request, "urlopen", side_effect=socket.timeout()
        ) as urlopen, mock.patch.object(hermes_runtime.time, "sleep"):
            with self.assertRaises(HermesTimeout):
                self.runtime._permintaan("/health")
        self.assertEqual(urlopen.call_count, 3)

    def test_401_dan_403_tanpa_retry(self):
        for kode in (401, 403):
            with self.subTest(kode=kode):
                with mock.patch.object(
                    hermes_runtime.urllib.request, "urlopen", side_effect=_http_error(kode)
                ) as urlopen:
                    with self.assertRaises(HermesTidakTersedia) as tangkap:
                        self.runtime._permintaan("/health")
                self.assertEqual(urlopen.call_count, 1)
                self.assertEqual(tangkap.exception.kode, "kunci_tidak_tersedia")

    def test_tanya_mempertahankan_status_rate_limit_dan_5xx(self):
        for kode in ("rate_limit", "hermes_5xx"):
            with self.subTest(kode=kode):
                with mock.patch.object(
                    self.runtime, "_permintaan",
                    side_effect=HermesTidakTersedia("galat uji", kode),
                ):
                    hasil = self.runtime.tanya("Apa visi UIR?", "sesi-uji")
                self.assertFalse(hasil["ok"])
                self.assertEqual(hasil["status"], kode)


class UjiStatusApiHermes(unittest.TestCase):
    def test_api_mempertahankan_status_galat_hermes(self):
        for kode in ("rate_limit", "hermes_5xx", "kunci_tidak_tersedia", "timeout"):
            with self.subTest(kode=kode):
                aplikasi = create_app(
                    pembuat_runtime=lambda k=kode: _RuntimePalsu(k),
                    runtime_aktif="hermes",
                )
                with TestClient(aplikasi) as klien:
                    respons = klien.post("/api/chat", json={"question": "Apa visi UIR?"})
                self.assertEqual(respons.status_code, 200)
                isi = respons.json()
                self.assertFalse(isi["ok"])
                self.assertEqual(isi["status"], kode)
                self.assertEqual(isi["agent_runtime"], "hermes")
                self.assertNotIn("api_key", str(isi).lower())


if __name__ == "__main__":
    unittest.main()
