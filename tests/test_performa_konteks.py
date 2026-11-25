"""Regresi untuk perbaikan latensi: konteks ringkas tanpa melemahkan bukti."""

import json
import unittest
from unittest import mock

from app import ollama_client
from app import orchestrator as modul


class _Klien:
    def __init__(self, *respons):
        self.antrean = list(respons)
        self.panggilan = []

    def chat(self, messages, tools=None):
        self.panggilan.append({
            "messages": [dict(item) for item in messages],
            "tools": tools,
        })
        return self.antrean.pop(0)


def _respons(konten="", tool_calls=None):
    return {
        "model": "hermes3:8b",
        "message": {
            "role": "assistant",
            "content": konten,
            "tool_calls": tool_calls or [],
        },
    }


def _panggil(nama, argumen):
    return {"function": {"name": nama, "arguments": argumen}}


class UjiKonteksModel(unittest.TestCase):
    def test_teks_panjang_diringkas_tanpa_mengubah_asli(self):
        asli = "Beasiswa UIR untuk mahasiswa aktif. " * 400
        hasil = modul._ringkas_untuk_model({"teks": asli}, "beasiswa")
        self.assertTrue(hasil["teks_diringkas"])
        self.assertEqual(hasil["panjang_asli"], len(asli))
        self.assertLessEqual(len(hasil["teks"]), modul.MAX_EVIDEN_MODEL)
        self.assertEqual(asli, "Beasiswa UIR untuk mahasiswa aktif. " * 400)

    def test_hasil_pencarian_dibatasi(self):
        hasil = modul._ringkas_untuk_model(
            {"results": [
                {"url": "https://uir.ac.id/%d/" % i, "cuplikan": "x" * 500}
                for i in range(20)
            ]},
            "uir",
        )
        self.assertEqual(len(hasil["results"]), modul.MAX_HASIL_MODEL)
        self.assertTrue(hasil["results_diringkas"])
        self.assertLessEqual(
            len(hasil["results"][0]["cuplikan"]),
            modul.MAX_CUPLIKAN_MODEL,
        )

    def test_bukti_lengkap_tetap_dipakai_verifikasi(self):
        teks_panjang = (
            "Beasiswa UIR terbuka untuk mahasiswa aktif. " * 200
            + "Alamat resmi: Jl. Kaharuddin Nasution No.113, Pekanbaru."
        )
        halaman = {
            "ok": True,
            "status": "berhasil",
            "url": "https://uir.ac.id/kontak/",
            "judul": "Kontak UIR",
            "teks": teks_panjang,
            "panjang": len(teks_panjang),
            "pesan": "",
            "waktu": "2026-11-25T00:00:00+00:00",
        }
        klien = _Klien(
            _respons(tool_calls=[_panggil(
                "baca_cache", {"url": "https://uir.ac.id/kontak/"}
            )]),
            _respons("Alamat UIR ada di halaman kontak."),
        )
        ork = modul.Orkestrator(client=klien, verifikasi_wajib=False)
        with mock.patch.object(modul, "baca_cache", return_value=halaman):
            ork.tanya("Apa alamat UIR?")

        self.assertEqual(ork.bukti[0]["teks"], teks_panjang)
        pesan_tool = [
            item for item in klien.panggilan[1]["messages"]
            if item.get("role") == "tool"
        ]
        self.assertEqual(len(pesan_tool), 1)
        isi = json.loads(pesan_tool[0]["content"])
        self.assertTrue(isi["teks_diringkas"])
        self.assertLessEqual(len(isi["teks"]), modul.MAX_EVIDEN_MODEL)

    def test_payload_tool_giliran_lama_tidak_dikirim(self):
        klien = _Klien(_respons("Jawaban pertama."), _respons("Jawaban kedua."))
        ork = modul.Orkestrator(client=klien, verifikasi_wajib=False)
        ork.tanya("Pertanyaan pertama")
        ork.tanya("Pertanyaan kedua")

        pesan = klien.panggilan[1]["messages"]
        self.assertNotIn("tool", [item.get("role") for item in pesan])
        self.assertIn("Pertanyaan pertama", json.dumps(pesan, ensure_ascii=False))
        self.assertIn("Jawaban pertama", json.dumps(pesan, ensure_ascii=False))
        self.assertIn("Pertanyaan kedua", json.dumps(pesan, ensure_ascii=False))
        # Riwayat asli tetap lengkap walaupun payload model sudah ringkas.
        self.assertEqual(ork.riwayat[0]["role"], "system")

    def test_client_mengirim_batas_token_output(self):
        with mock.patch("urllib.request.urlopen") as buka:
            buka.return_value.__enter__.return_value.read.return_value = json.dumps({
                "message": {"role": "assistant", "content": "Halo."},
            }).encode("utf-8")
            klien = ollama_client.OllamaClient()
            klien.chat([{"role": "user", "content": "Halo"}])

        permintaan = buka.call_args.args[0]
        payload = json.loads(permintaan.data.decode("utf-8"))
        self.assertIn("num_predict", payload["options"])
        self.assertEqual(
            payload["options"]["num_predict"],
            ollama_client.OLLAMA_NUM_PREDICT,
        )

    def test_client_membatasi_token_saat_memilih_tool(self):
        with mock.patch("urllib.request.urlopen") as buka:
            buka.return_value.__enter__.return_value.read.return_value = json.dumps({
                "message": {"role": "assistant", "content": ""},
            }).encode("utf-8")
            klien = ollama_client.OllamaClient()
            klien.chat(
                [{"role": "user", "content": "Halo"}],
                tools=[{"type": "function", "function": {"name": "cari_uir"}}],
            )

        permintaan = buka.call_args.args[0]
        payload = json.loads(permintaan.data.decode("utf-8"))
        self.assertEqual(
            payload["options"]["num_predict"],
            ollama_client.OLLAMA_NUM_PREDICT_TOOL,
        )
        self.assertLess(
            ollama_client.OLLAMA_NUM_PREDICT_TOOL,
            ollama_client.OLLAMA_NUM_PREDICT,
        )


if __name__ == "__main__":
    unittest.main()
