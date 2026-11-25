"""Pengujian adapter API FastAPI tanpa Ollama dan tanpa jaringan.

Orkestrator asli sengaja tidak dipanggil di sini. Yang diuji adalah kontrak
adapter: sesi per pengguna, validasi input, filter sumber, dan kerahasiaan
detail internal.
"""

import concurrent.futures
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from web.api import MAX_PERTANYAAN, SESSION_ID_RE, create_app

URL_VALID = "https://uir.ac.id/profil-universitas-islam-riau"
URL_VALID_2 = "https://uir.ac.id/program-studi"


class Orketru:
    """Pengganti Orkestrator yang hanya mencatat pertanyaan."""

    def __init__(self, nomor, hasil=None, galat=None):
        self.nomor = nomor
        self.hasil = hasil
        self.galat = galat
        self.riwayat = []
        self.bukti = []

    def tanya(self, pertanyaan):
        if self.galat is not None:
            raise self.galat
        self.riwayat.append(pertanyaan)
        if self.hasil is not None:
            return dict(self.hasil)
        return {
            "ok": True,
            "status": "berhasil",
            "pesan": "",
            "jawaban": f"ork{self.nomor}:{pertanyaan}",
            "sumber": [],
            "sumber_terbaca": [],
            "langkah": 2,
        }


class PembuatOrketru:
    def __init__(self, hasil=None, galat=None):
        self.daftar = []
        self.hasil = hasil
        self.galat = galat

    def __call__(self):
        ork = Orketru(len(self.daftar), hasil=self.hasil, galat=self.galat)
        self.daftar.append(ork)
        return ork


def klien(pembuat=None, folder=None):
    app = create_app(
        pembuat_orkestrator=pembuat or PembuatOrketru(),
        direktori_frontend=folder,
    )
    return TestClient(app, raise_server_exceptions=False)


def hasil_berhasil(**perubahan):
    data = {
        "ok": True,
        "status": "berhasil",
        "pesan": "",
        "jawaban": "Jawaban uji.",
        "sumber": [],
        "sumber_terbaca": [URL_VALID],
        "langkah": 3,
    }
    data.update(perubahan)
    return data


class TestKesehatanApi(unittest.TestCase):
    def test_health_melaporkan_status_ollama_lokal(self):
        with patch("web.api.cek_ollama", return_value={
            "ok": True,
            "model": "hermes3:8b",
            "model_tersedia": True,
            "pesan": "Ollama lokal terhubung.",
        }):
            respons = klien().get("/api/health")

        self.assertEqual(respons.status_code, 200)
        data = respons.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["lokal"])
        self.assertEqual(data["model"], "hermes3:8b")
        self.assertTrue(data["model_tersedia"])
        self.assertIn("waktu", data)

    def test_health_tidak_membocorkan_exception(self):
        with patch("web.api.cek_ollama", side_effect=RuntimeError("RAHASIA_HEALTH")):
            respons = klien().get("/api/health")

        self.assertEqual(respons.status_code, 200)
        data = respons.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["penyedia"], "tokenharbor")
        self.assertFalse(data["lokal"])
        self.assertNotIn("RAHASIA_HEALTH", respons.text)


class TestChatApi(unittest.TestCase):
    def test_chat_mengembalikan_kontrak_dan_menyimpan_sesi(self):
        pembuat = PembuatOrketru()
        client = klien(pembuat)

        respons = client.post("/api/chat", json={"question": "Apa itu UIR?"})

        self.assertEqual(respons.status_code, 200)
        data = respons.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "berhasil")
        self.assertEqual(data["jawaban"], "ork0:Apa itu UIR?")
        self.assertEqual(data["langkah"], 2)
        self.assertRegex(data["session_id"], SESSION_ID_RE)
        self.assertEqual(len(pembuat.daftar), 1)
        self.assertEqual(pembuat.daftar[0].riwayat, ["Apa itu UIR?"])

    def test_sesi_sama_memakai_orkestrator_yang_sama(self):
        pembuat = PembuatOrketru()
        client = klien(pembuat)

        pertama = client.post("/api/chat", json={"question": "Pertanyaan satu"}).json()
        sid = pertama["session_id"]
        kedua = client.post("/api/chat", json={
            "session_id": sid,
            "question": "Pertanyaan dua",
        }).json()

        self.assertEqual(kedua["session_id"], sid)
        self.assertEqual(kedua["jawaban"], "ork0:Pertanyaan dua")
        self.assertEqual(len(pembuat.daftar), 1)
        self.assertEqual(
            pembuat.daftar[0].riwayat,
            ["Pertanyaan satu", "Pertanyaan dua"],
        )

    def test_dua_pengguna_mendapat_sesi_terpisah(self):
        pembuat = PembuatOrketru()
        client = klien(pembuat)

        a = client.post("/api/chat", json={"question": "Dari pengguna A"}).json()
        b = client.post("/api/chat", json={"question": "Dari pengguna B"}).json()

        self.assertNotEqual(a["session_id"], b["session_id"])
        self.assertEqual(a["jawaban"], "ork0:Dari pengguna A")
        self.assertEqual(b["jawaban"], "ork1:Dari pengguna B")
        self.assertEqual(len(pembuat.daftar), 2)
        self.assertEqual(pembuat.daftar[0].riwayat, ["Dari pengguna A"])
        self.assertEqual(pembuat.daftar[1].riwayat, ["Dari pengguna B"])

        # Pertanyaan lanjutan A tidak boleh masuk ke riwayat B.
        lanjutan = client.post("/api/chat", json={
            "session_id": a["session_id"],
            "question": "Lanjutan A",
        }).json()
        self.assertEqual(lanjutan["jawaban"], "ork0:Lanjutan A")
        self.assertEqual(pembuat.daftar[1].riwayat, ["Dari pengguna B"])

    def test_session_id_tidak_dikenal_diganti_dengan_id_baru(self):
        pembuat = PembuatOrketru()
        client = klien(pembuat)

        respons = client.post("/api/chat", json={
            "session_id": "aaaaaaaaaaaaaaaaaaaaaaaa",
            "question": "Pertanyaan",
        })

        data = respons.json()
        self.assertEqual(respons.status_code, 200)
        self.assertRegex(data["session_id"], SESSION_ID_RE)
        self.assertNotEqual(data["session_id"], "aaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(len(pembuat.daftar), 1)

    def test_reset_membuat_sesi_dan_orkestrator_baru(self):
        pembuat = PembuatOrketru()
        client = klien(pembuat)

        awal = client.post("/api/chat", json={"question": "Sebelum reset"}).json()
        sid_lama = awal["session_id"]
        reset = client.post("/api/session/reset", json={"session_id": sid_lama}).json()

        self.assertTrue(reset["ok"])
        self.assertRegex(reset["session_id"], SESSION_ID_RE)
        self.assertNotEqual(reset["session_id"], sid_lama)

        sesudah = client.post("/api/chat", json={
            "session_id": reset["session_id"],
            "question": "Sesudah reset",
        }).json()

        self.assertEqual(sesudah["jawaban"], "ork1:Sesudah reset")
        self.assertEqual(pembuat.daftar[0].riwayat, ["Sebelum reset"])
        self.assertEqual(pembuat.daftar[1].riwayat, ["Sesudah reset"])

    def test_reset_tanpa_session_id_juga_membuat_id_baru(self):
        client = klien(PembuatOrketru())
        respons = client.post("/api/session/reset", json={})

        self.assertEqual(respons.status_code, 200)
        self.assertRegex(respons.json()["session_id"], SESSION_ID_RE)


class TestValidasiInput(unittest.TestCase):
    def test_pertanyaan_kosong_ditolak(self):
        for nilai in ("", "   ", "\n\t"):
            with self.subTest(nilai=repr(nilai)):
                respons = klien().post("/api/chat", json={"question": nilai})
                self.assertEqual(respons.status_code, 400)
                data = respons.json()
                self.assertFalse(data["ok"])
                self.assertEqual(data["status"], "validasi_gagal")
                self.assertIn("kosong", data["pesan"])

    def test_pertanyaan_terlalu_panjang_ditolak(self):
        respons = klien().post("/api/chat", json={
            "question": "a" * (MAX_PERTANYAAN + 1),
        })

        self.assertEqual(respons.status_code, 400)
        data = respons.json()
        self.assertEqual(data["status"], "validasi_gagal")
        self.assertIn(str(MAX_PERTANYAAN), data["pesan"])

    def test_pertanyaan_mengandung_null_byte_ditolak(self):
        respons = klien().post("/api/chat", json={"question": "abc\x00def"})

        self.assertEqual(respons.status_code, 400)
        self.assertEqual(respons.json()["status"], "validasi_gagal")

    def test_pertanyaan_non_teks_ditolak_tanpa_detail(self):
        for nilai in (123, None, ["pertanyaan"]):
            with self.subTest(nilai=nilai):
                respons = klien().post("/api/chat", json={"question": nilai})
                self.assertEqual(respons.status_code, 400)
                self.assertEqual(respons.json()["status"], "validasi_gagal")
                self.assertNotIn("Traceback", respons.text)

    def test_body_tanpa_question_ditolak(self):
        respons = klien().post("/api/chat", json={})

        self.assertEqual(respons.status_code, 400)
        self.assertEqual(respons.json()["status"], "validasi_gagal")


class TestSumberApi(unittest.TestCase):
    def test_hanya_sumber_terbaca_yang_valid_yang_dikirim(self):
        pembuat = PembuatOrketru(hasil=hasil_berhasil(
            sumber=[
                "https://www.uir.ac.id/kandidat-belum-dibaca",
                "http://uir.ac.id/tidak-aman",
            ],
            sumber_terbaca=[
                URL_VALID,
                URL_VALID,
                URL_VALID_2,
                "http://uir.ac.id/http",
                "https://uir.ac.id.evil.com/phishing",
                "https://www.uir.ac.id/www",
                123,
            ],
        ))
        pembuat.bukti_judul = None
        client = klien(pembuat)

        respons = client.post("/api/chat", json={"question": "Sumber"})
        sumber = respons.json()["sumber"]

        self.assertEqual(
            sumber,
            [
                {"url": URL_VALID, "judul": ""},
                {"url": URL_VALID_2, "judul": ""},
            ],
        )

    def test_judul_sumber_diambil_dari_bukti_orkestrator(self):
        pembuat = PembuatOrketru(hasil=hasil_berhasil(
            sumber_terbaca=[URL_VALID, URL_VALID_2],
        ))

        # Judul disuntikkan setelah instance dibuat, meniru self.bukti asli.
        def dengan_bukti():
            ork = pembuat()
            ork.bukti = [
                {"url": URL_VALID, "judul": "Profil Universitas Islam Riau"},
                {"url": "https://uir.ac.id.evil.com/x", "judul": "Judul palsu"},
            ]
            return ork

        client = TestClient(
            create_app(pembuat_orkestrator=dengan_bukti),
            raise_server_exceptions=False,
        )
        respons = client.post("/api/chat", json={"question": "Judul"})
        sumber = respons.json()["sumber"]

        self.assertEqual(sumber[0]["judul"], "Profil Universitas Islam Riau")
        self.assertEqual(sumber[1]["judul"], "")
        self.assertNotIn("Judul palsu", respons.text)


class TestStatusKhusus(unittest.TestCase):
    def test_bukti_tidak_cukup_diteruskan_apa_adanya(self):
        hasil = hasil_berhasil(
            ok=False,
            status="bukti_tidak_cukup",
            pesan="Halaman dibaca, tetapi butir tidak ditemukan.",
            jawaban="Saya tidak akan menebak.",
            sumber_terbaca=["https://uir.ac.id/halaman-yang-dibaca"],
            langkah=5,
        )
        respons = klien(PembuatOrketru(hasil=hasil)).post(
            "/api/chat", json={"question": "Tahun berdiri fakultas?"}
        )

        self.assertEqual(respons.status_code, 200)
        data = respons.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "bukti_tidak_cukup")
        self.assertEqual(data["jawaban"], "Saya tidak akan menebak.")
        self.assertEqual(len(data["sumber"]), 1)

    def test_sumber_tidak_tersedia_tanpa_sumber_dan_tanpa_rekaan(self):
        hasil = hasil_berhasil(
            ok=False,
            status="sumber_tidak_tersedia",
            pesan="Informasi belum terverifikasi.",
            jawaban="",
            sumber_terbaca=[],
            langkah=5,
        )
        respons = klien(PembuatOrketru(hasil=hasil)).post(
            "/api/chat", json={"question": "Kabar terbaru?"}
        )

        data = respons.json()
        self.assertEqual(respons.status_code, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "sumber_tidak_tersedia")
        self.assertEqual(data["jawaban"], "")
        self.assertEqual(data["sumber"], [])

    def test_galat_internal_tidak_menampilkan_traceback(self):
        pembuat = PembuatOrketru(galat=RuntimeError("RAHASIA_INTERNAL_DETAIL"))
        respons = klien(pembuat).post("/api/chat", json={"question": "Apa kabar?"})

        self.assertEqual(respons.status_code, 500)
        data = respons.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "kesalahan_internal")
        self.assertRegex(data["session_id"], SESSION_ID_RE)
        self.assertEqual(data["jawaban"], "")
        self.assertEqual(data["sumber"], [])
        self.assertNotIn("RAHASIA_INTERNAL_DETAIL", respons.text)
        self.assertNotIn("Traceback", respons.text)


class TestKonkurensi(unittest.TestCase):
    def test_permintaan_bersamaan_pada_sesi_sama_terserialisasi(self):
        class OrkKonkuren(Orketru):
            def __init__(self, nomor):
                super().__init__(nomor)
                self.aktif = 0
                self.maks_aktif = 0
                self.pengunci = threading.Lock()

            def tanya(self, pertanyaan):
                with self.pengunci:
                    self.aktif += 1
                    self.maks_aktif = max(self.maks_aktif, self.aktif)
                try:
                    time.sleep(0.02)
                    return super().tanya(pertanyaan)
                finally:
                    with self.pengunci:
                        self.aktif -= 1

        created = []

        def pembuat():
            ork = OrkKonkuren(len(created))
            created.append(ork)
            return ork

        client = klien(pembuat)
        sid = client.post("/api/chat", json={"question": "Pemanasan"}).json()["session_id"]

        def kirim(nomor):
            return client.post("/api/chat", json={
                "session_id": sid,
                "question": f"Pertanyaan {nomor}",
            })

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            respons = list(executor.map(kirim, range(8)))

        self.assertTrue(all(item.status_code == 200 for item in respons))
        self.assertEqual(len({item.json()["session_id"] for item in respons}), 1)
        self.assertEqual(created[0].maks_aktif, 1)
        self.assertEqual(len(created[0].riwayat), 9)


class TestFrontendStatis(unittest.TestCase):
    def test_folder_frontend_dilayani_dari_origin_sama(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "index.html").write_text(
                "<!doctype html><title>UIR AI Assistant</title>",
                encoding="utf-8",
            )
            respons = klien(folder=Path(folder)).get("/")

        self.assertEqual(respons.status_code, 200)
        self.assertIn("UIR AI Assistant", respons.text)


if __name__ == "__main__":
    unittest.main()
