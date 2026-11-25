"""Jalur cepat deterministik: bukti cukup tanpa memanggil model.

Jalur cepat HANYA dipakai pada mode produksi dengan klien Ollama lokal nyata,
atau bila eksplisit diaktifkan pada pengujian. Verifikasi klaim dan sitasi
tetap dijalankan oleh ``_finalize``.
"""

import unittest
from unittest import mock

from app import orchestrator as modul
from app import telemetri


URL_KONTAK = "https://uir.ac.id/kontak/"

# Data tiruan, bukan alamat UIR yang sebenarnya.
TEKS_KONTAK = """Kontak

Universitas Islam Riau

Telepon: +62 761 000111

Email : info[at]contoh.ac.id

Alamat

Jl. Contoh Raya No.123, Pekanbaru, Riau, Indonesia Kode Pos 28284
"""

ALAMAT_TIRUAN = (
    "Jl. Contoh Raya No.123, Pekanbaru, Riau, Indonesia Kode Pos 28284"
)


class KlienPalsu:
    """Klien yang gagal bila dipanggil, untuk membuktikan jalur cepat."""

    def __init__(self):
        self.panggilan = []

    def chat(self, messages, tools=None):
        self.panggilan.append({
            "messages": [dict(item) for item in messages],
            "tools": tools,
        })
        raise AssertionError("Jalur cepat tidak boleh memanggil model.")


def _hasil_cari():
    return {
        "ok": True,
        "status": "ditemukan",
        "query": "kontak UIR",
        "count": 1,
        "sumber": "wp_rest",
        "results": [{
            "url": URL_KONTAK,
            "judul": "Kontak Universitas Islam Riau",
            "cuplikan": "Alamat dan kontak resmi UIR",
        }],
        "pesan": "Ditemukan 1 halaman.",
        "waktu": "2026-11-25T00:00:00+00:00",
    }


def _hasil_baca():
    return {
        "ok": True,
        "status": "ditemukan",
        "url": URL_KONTAK,
        "data": {
            "url": URL_KONTAK,
            "judul": "Kontak Universitas Islam Riau",
            "teks": TEKS_KONTAK,
            "panjang": len(TEKS_KONTAK),
            "waktu": "2026-11-25T00:00:00+00:00",
        },
        "waktu_cache": "2026-11-25T00:00:00+00:00",
        "kedaluwarsa_pada": "2026-11-26T00:00:00+00:00",
        "pesan": "Cache ditemukan dan masih berlaku.",
    }


class UjiJalurCepat(unittest.TestCase):
    def _pasang_tool(self):
        patchers = {
            "cari": mock.patch.object(
                modul, "cari_uir", return_value=_hasil_cari()
            ),
            "baca": mock.patch.object(
                modul, "baca_cache", return_value=_hasil_baca()
            ),
            "simpan": mock.patch.object(
                modul,
                "simpan_cache",
                return_value={"ok": True, "status": "tersimpan"},
            ),
        }
        for patcher in patchers.values():
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_klien_tiruan_tidak_mengaktifkan_jalur_cepat(self):
        ork = modul.Orkestrator(
            client=KlienPalsu(), verifikasi_wajib=True
        )
        self.assertFalse(ork.jalur_cepat)

    def test_bukti_lokasi_cukup_tanpa_panggilan_model(self):
        self._pasang_tool()
        klien = KlienPalsu()
        ork = modul.Orkestrator(
            client=klien,
            verifikasi_wajib=True,
            jalur_cepat=True,
        )

        telemetri.mulai()
        try:
            hasil = ork.tanya("Di mana lokasi kampus UIR?")
            ringkasan = telemetri.selesai()
        finally:
            telemetri.selesai()

        self.assertTrue(hasil["ok"], hasil.get("detail", ""))
        self.assertIn(ALAMAT_TIRUAN, hasil["jawaban"])
        self.assertEqual(hasil["sumber"], [URL_KONTAK])
        self.assertEqual(klien.panggilan, [])
        self.assertEqual(ringkasan["panggilan_model"], 0)
        self.assertEqual(ringkasan["jalur_cepat"], 1)
        self.assertIn("jalur_cepat", ringkasan["penanda"])

    def test_tanpa_bukti_jalur_cepat_tidak_dipakai(self):
        with mock.patch.object(
            modul,
            "cari_uir",
            return_value={
                "ok": True,
                "status": "tidak_ditemukan",
                "results": [],
            },
        ):
            ork = modul.Orkestrator(
                client=object(),
                verifikasi_wajib=True,
                jalur_cepat=True,
            )
            ork.pertanyaan = "Di mana lokasi kampus UIR?"
            self.assertIsNone(ork._jawaban_awal_terverifikasi())


if __name__ == "__main__":
    unittest.main()
