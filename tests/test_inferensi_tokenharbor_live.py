"""Uji inferensi LIVE ke TokenHarbor.

Dilewati otomatis bila ``DEEPSEEK_API_KEY`` belum tersedia di environment/.env.
Menjalankan seluruh uji live::

    python -m unittest tests.test_inferensi_tokenharbor_live -v

Bukti model aktif diambil dari metadata ``/models`` dan dari field ``model``
pada respons ``/chat/completions``, bukan dari label frontend. Kredensial tidak
pernah dicetak.
"""

import unittest

from app import model_client

KUNCI_TERSEDIA = model_client.kunci_tersedia()
MODEL_AKTIF = model_client.DEEPSEEK_MODEL


@unittest.skipUnless(
    KUNCI_TERSEDIA,
    "DEEPSEEK_API_KEY belum tersedia; uji inferensi live dilewati.",
)
class UjiTokenHarborLive(unittest.TestCase):
    def test_metadata_models_memverifikasi_model_aktif(self):
        status = model_client.cek_koneksi()
        self.assertTrue(status["ok"], status["pesan"])
        self.assertTrue(status["model_tersedia"], status["pesan"])
        self.assertEqual(status["penyedia"], "tokenharbor")
        # Penyedia dapat mengembalikan ID kanonik tanpa akhiran ``:free``;
        # gunakan normalisasi yang sama dengan jalur produksi.
        self.assertTrue(
            model_client._model_cocok(MODEL_AKTIF, status.get("model_api", "")),
            "Model dari metadata tidak cocok dengan model aktif.",
        )

    def test_inferensi_mengembalikan_model_dari_api(self):
        klien = model_client.DeepSeekClient()
        hasil = klien.chat([
            {
                "role": "system",
                "content": "Jawab sangat singkat dalam Bahasa Indonesia.",
            },
            {"role": "user", "content": "Balas hanya dengan satu kata: siap"},
        ])
        konten = str(hasil["message"]["content"]).strip()
        self.assertTrue(konten, "Penyedia mengembalikan jawaban kosong.")
        self.assertTrue(
            model_client._model_cocok(MODEL_AKTIF, hasil.get("model_dari_api", "")),
            "Model pada respons chat tidak cocok dengan model aktif.",
        )

    def test_payload_tool_diterima_penyedia(self):
        klien = model_client.DeepSeekClient()
        alat = [{
            "type": "function",
            "function": {
                "name": "cari_uir",
                "description": "Cari halaman publik pada situs universitas.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }]
        hasil = klien.chat([
            {
                "role": "system",
                "content": "Gunakan tool yang tersedia bila informasi belum ada.",
            },
            {
                "role": "user",
                "content": "Cari halaman tentang fakultas di universitas.",
            },
        ], tools=alat)
        panggilan = hasil["message"].get("tool_calls") or []
        for item in panggilan:
            nama = (item.get("function") or {}).get("name", "")
            self.assertIn(nama, {"cari_uir"})
        # Respons tetap sah baik model memilih tool maupun menjawab langsung.
        self.assertTrue(
            panggilan or str(hasil["message"]["content"]).strip(),
            "Penyedia tidak mengembalikan tool call maupun teks.",
        )

    def test_retrieval_through_orchestrator(self):
        from app.orchestrator import Orkestrator

        hasil = Orkestrator().tanya("Apa saja fakultas di Universitas Islam Riau?")
        self.assertIsInstance(hasil, dict)
        self.assertIn("status", hasil)
        self.assertIsInstance(hasil.get("sumber_terbaca", []), list)


if __name__ == "__main__":
    unittest.main()
