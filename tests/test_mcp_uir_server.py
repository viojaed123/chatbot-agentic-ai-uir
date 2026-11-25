"""Uji pembungkus MCP UIR: hanya tiga tool baca, tanpa agent loop."""

import json
import unittest
from unittest import mock

import mcp_uir_server as server


class UjiServerMcpUir(unittest.TestCase):
    def test_hanya_tiga_tool_uir(self):
        nama = {alat.name for alat in server.mcp._tool_manager.list_tools()}
        self.assertEqual(nama, {"cari_uir", "ambil_halaman", "baca_cache"})

    def test_cari_uir_mengutamakan_url(self):
        hasil = {
            "ok": True,
            "status": "sukses",
            "query": "visi",
            "count": 1,
            "results": [
                {
                    "judul": "Profil UIR",
                    "url": "https://uir.ac.id/profil/",
                    "cuplikan": "Visi UIR",
                }
            ],
        }
        with mock.patch.object(server, "cari_uir", return_value=hasil):
            data = json.loads(server.cari_uir_tool("visi"))
        self.assertEqual(next(iter(data)), "url")
        self.assertEqual(data["url"], "https://uir.ac.id/profil/")
        self.assertEqual(data["count"], 1)

    def test_ambil_halaman_memotong_teks_panjang(self):
        hasil = {
            "ok": True,
            "url": "https://uir.ac.id/profil/",
            "status": "sukses",
            "judul": "Profil",
            "teks": "a" * 5000,
            "panjang": 5000,
        }
        with mock.patch.object(server, "ambil_halaman", return_value=hasil):
            data = json.loads(server.ambil_halaman_tool(hasil["url"]))
        self.assertLessEqual(len(data["potongan"]), 3020)
        self.assertTrue(data["potongan"].endswith("… [dipotong]"))

    def test_baca_cache_menolak_url_non_uir_lewat_tool_asli(self):
        hasil = {
            "ok": False,
            "status": "url_ditolak",
            "url": "https://example.com/",
            "data": {},
        }
        with mock.patch.object(server, "baca_cache", return_value=hasil):
            data = json.loads(server.baca_cache_tool("https://example.com/"))
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "url_ditolak")

    def test_respons_tidak_valid_ditangani(self):
        with mock.patch.object(server, "cari_uir", return_value=None):
            data = json.loads(server.cari_uir_tool("visi"))
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "respons_tidak_valid")


if __name__ == "__main__":
    unittest.main()
