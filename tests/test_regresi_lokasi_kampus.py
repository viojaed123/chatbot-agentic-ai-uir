"""Regresi lokasi/alamat/kontak kampus tanpa internet dan tanpa Ollama.

Kasus yang dijaga: pertanyaan "Di mana lokasi kampus UIR?" dulu dijawab
dari artikel jadwal CBT PMB. Sekarang jawaban hanya boleh berasal dari
halaman kontak/profil resmi yang benar-benar terbaca. Semua nilai pada fixture
adalah data tiruan; tidak ada alamat UIR yang disimpan di source code.
"""

import unittest
from pathlib import Path
from unittest import mock

from app import orchestrator as modul
from app import perencana
from app.system_prompt import SYSTEM_PROMPT
from tools import cari_uir as pencarian


URL_KONTAK = "https://uir.ac.id/kontak/"
URL_CBT = (
    "https://uir.ac.id/pmb-gelombang-1-selesai-berikut-lokasi-"
    "dan-jadwal-cbt-peserta/"
)

# Data tiruan. Bukan alamat, telepon, atau email UIR yang sebenarnya.
TEKS_KONTAK = """Kontak

Universitas Islam Riau

Kontak

Telepon: +62 761 000111

Fax: +62 761 000222

WA: +62 811-000-3333 (Chat Only)

Email : info[at]contoh.ac.id

Alamat

Jl. Contoh Raya No.123, Pekanbaru, Riau, Indonesia Kode Pos 28284
"""

TEKS_CBT = """Lokasi dan Jadwal CBT Peserta

Jl. Kharuddin Tiruan No. 999, Laboratorium Contoh Lantai III Gedung C
Fakultas Contoh UIR.
"""

ALAMAT_TIRUAN = (
    "Jl. Contoh Raya No.123, Pekanbaru, Riau, Indonesia Kode Pos 28284"
)
ALAMAT_CBT = "Jl. Kharuddin Tiruan No. 999"


class KlienPalsu:
    """Klien Ollama tiruan dengan respons antrean."""

    def __init__(self, *respons):
        self.antrean = list(respons)
        self.panggilan = []

    def chat(self, messages, tools=None):
        self.panggilan.append({
            "messages": [dict(item) for item in messages],
            "tools": tools,
        })
        if not self.antrean:
            raise AssertionError("KlienPalsu kehabisan respons.")
        return self.antrean.pop(0)


def _respons(konten="Saya tidak dapat memastikan dari sumber."):
    return {
        "model": "hermes3:8b",
        "message": {
            "role": "assistant",
            "content": konten,
            "tool_calls": [],
        },
    }


def _item_cari(url, judul, cuplikan):
    return {"judul": judul, "url": url, "cuplikan": cuplikan}


def _hasil_cari(*item):
    return {
        "ok": True,
        "status": "ditemukan" if item else "tidak_ditemukan",
        "query": "kontak UIR",
        "count": len(item),
        "sumber": "wp_rest",
        "results": list(item),
        "pesan": "Ditemukan %d halaman." % len(item),
        "waktu": "2026-11-25T00:00:00+00:00",
    }


def _hasil_baca(url, judul, teks):
    return {
        "ok": True,
        "status": "ditemukan",
        "url": url,
        "data": {
            "url": url,
            "judul": judul,
            "teks": teks,
            "panjang": len(teks),
            "waktu": "2026-11-25T00:00:00+00:00",
        },
        "waktu_cache": "2026-11-25T00:00:00+00:00",
        "kedaluwarsa_pada": "2026-11-26T00:00:00+00:00",
        "pesan": "Cache ditemukan dan masih berlaku.",
    }


class UjiPemahamanLokasi(unittest.TestCase):
    def test_lokasi_kampus_memakai_kueri_kontak(self):
        info = perencana.pahami_lokasi("Di mana lokasi kampus UIR?")

        self.assertTrue(info["aktif"])
        self.assertEqual(info["kind"], "lokasi")
        self.assertEqual(info["kanonik"], "kontak UIR")
        self.assertFalse(info["artikel"])
        self.assertIn("kontak UIR", info["subkueri"])

    def test_variasi_alamat_kontak_dan_peta(self):
        kasus = {
            "Alamat kampus Universitas Islam Riau": ("alamat", "kontak UIR"),
            "kontak UIR": ("kontak", "kontak UIR"),
            "maps UIR": ("peta", "maps UIR"),
        }

        for pertanyaan, (kind, kanonik) in kasus.items():
            with self.subTest(pertanyaan=pertanyaan):
                info = perencana.pahami_lokasi(pertanyaan)
                self.assertTrue(info["aktif"])
                self.assertEqual(info["kind"], kind)
                self.assertEqual(info["kanonik"], kanonik)

    def test_konteks_artikel_dan_topik_lain_ditolak(self):
        for pertanyaan in (
            "Di mana lokasi pelaksanaan CBT UIR?",
            "lokasi jadwal pendaftaran PMB UIR",
            "lokasi Fakultas Teknik UIR",
        ):
            with self.subTest(pertanyaan=pertanyaan):
                self.assertFalse(
                    perencana.pahami_lokasi(pertanyaan)["aktif"]
                )

    def test_strategi_lokasi_tidak_memakai_kata_tanya_mentah(self):
        strategi = perencana.strategi_pencarian(
            "Di mana lokasi kampus UIR?", batas=3
        )

        self.assertEqual(strategi["topik"], ["lokasi"])
        self.assertEqual(strategi["subkueri"][0], "kontak UIR")
        self.assertTrue(strategi["butuh_sitemap"])


class UjiNormalisasiPencarian(unittest.TestCase):
    def test_normalisasi_tidak_menghasilkan_mana_lokasi(self):
        hasil = pencarian._normalisasi_query("Di mana lokasi kampus UIR?")

        self.assertEqual(hasil, "kontak UIR")
        self.assertNotIn("mana lokasi", hasil)

    def test_normalisasi_peta_memakai_maps(self):
        hasil = pencarian._normalisasi_query("peta kampus UIR")

        self.assertEqual(hasil, "maps UIR")


class UjiEkstraksiDanVerifikasi(unittest.TestCase):
    def setUp(self):
        self.rekam = {
            "url": URL_KONTAK,
            "judul": "Kontak - Universitas Islam Riau",
            "teks": TEKS_KONTAK,
            "sumber": "baca_cache",
            "waktu": "2026-11-25T00:00:00+00:00",
        }

    def test_ekstrak_alamat_verbatim(self):
        self.assertEqual(modul._ekstrak_alamat(TEKS_KONTAK), ALAMAT_TIRUAN)

    def test_authoritative_menolak_artikel_cbt(self):
        artikel = {
            "url": URL_CBT,
            "judul": "Lokasi dan Jadwal CBT Peserta",
            "teks": TEKS_CBT,
            "sumber": "baca_cache",
        }

        self.assertTrue(modul._authoritative_lokasi(self.rekam))
        self.assertFalse(modul._authoritative_lokasi(artikel))

    def test_verifikasi_menolak_alamat_rekaan(self):
        jawaban = modul._lokasi_answer(
            [self.rekam], "Di mana lokasi kampus UIR?"
        )

        self.assertIsNotNone(jawaban)
        self.assertEqual(
            modul._klaim_lokasi_didukung(
                jawaban, [self.rekam], "Di mana lokasi kampus UIR?"
            ),
            (True, ""),
        )

        rekaan = jawaban.replace(
            ALAMAT_TIRUAN, "Jl. Rekaan Salah No.1, Pekanbaru"
        )
        self.assertFalse(
            modul._klaim_lokasi_didukung(
                rekaan, [self.rekam], "Di mana lokasi kampus UIR?"
            )[0]
        )


class UjiAlurLokasi(unittest.TestCase):
    def setUp(self):
        self.palsu = {}

    def _pasang_tool(self, hasil_cari, peta_baca=None):
        patchers = {
            "cari": mock.patch.object(
                modul, "cari_uir", return_value=hasil_cari
            ),
            "baca": mock.patch.object(
                modul,
                "baca_cache",
                side_effect=lambda url: (
                    peta_baca or {}
                ).get(url, {
                    "ok": False,
                    "status": "tidak_ditemukan",
                    "url": url,
                    "data": None,
                    "pesan": "Tidak ada cache.",
                }),
            ),
            "ambil": mock.patch.object(
                modul,
                "ambil_halaman",
                return_value={
                    "ok": False,
                    "status": "situs_tidak_tersedia",
                    "url": "",
                    "judul": "",
                    "teks": "",
                    "panjang": 0,
                    "pesan": "Tidak mengambil halaman pada uji ini.",
                },
            ),
            "simpan": mock.patch.object(
                modul,
                "simpan_cache",
                return_value={"ok": True, "status": "tersimpan"},
            ),
        }
        for nama, patcher in patchers.items():
            self.palsu[nama] = patcher.start()
            self.addCleanup(patcher.stop)

    def _orkestrator(self, *respons):
        return modul.Orkestrator(
            client=KlienPalsu(*respons), verifikasi_wajib=True
        )

    def test_lokasi_dijawab_dari_halaman_kontak_bukan_cbt(self):
        self._pasang_tool(
            _hasil_cari(
                _item_cari(
                    URL_CBT,
                    "Lokasi dan Jadwal CBT Peserta",
                    "Lokasi pelaksanaan CBT peserta PMB",
                ),
                _item_cari(
                    URL_KONTAK,
                    "Kontak - Universitas Islam Riau",
                    "Alamat dan kontak resmi UIR",
                ),
            ),
            {
                URL_CBT: _hasil_baca(
                    URL_CBT, "Lokasi dan Jadwal CBT Peserta", TEKS_CBT
                ),
                URL_KONTAK: _hasil_baca(
                    URL_KONTAK, "Kontak - Universitas Islam Riau", TEKS_KONTAK
                ),
            },
        )
        ork = self._orkestrator(_respons())

        hasil = ork.tanya("Di mana lokasi kampus UIR?")

        self.assertTrue(hasil["ok"])
        self.assertGreaterEqual(hasil["langkah"], 1)
        self.assertIn(ALAMAT_TIRUAN, hasil["jawaban"])
        self.assertNotIn(ALAMAT_CBT, hasil["jawaban"])
        self.assertIn(URL_KONTAK, hasil["sumber"])
        self.assertNotIn(URL_CBT, hasil["sumber"])
        self.assertNotIn(URL_CBT, hasil["jawaban"])
        self.assertTrue(hasil["status_sumber"][URL_KONTAK]["cited"])
        self.assertTrue(hasil["status_sumber"][URL_KONTAK]["evidence"])

    def test_hanya_artikel_cbt_harus_ditolak(self):
        self._pasang_tool(
            _hasil_cari(
                _item_cari(
                    URL_CBT,
                    "Lokasi dan Jadwal CBT Peserta",
                    "Lokasi pelaksanaan CBT peserta PMB",
                )
            ),
            {
                URL_CBT: _hasil_baca(
                    URL_CBT, "Lokasi dan Jadwal CBT Peserta", TEKS_CBT
                )
            },
        )
        ork = self._orkestrator(_respons(), _respons())

        hasil = ork.tanya("Di mana lokasi kampus UIR?")

        self.assertFalse(hasil["ok"])
        self.assertEqual(hasil["status"], "bukti_tidak_cukup")
        self.assertNotIn(ALAMAT_CBT, hasil["jawaban"])
        self.assertIn("belum", hasil["jawaban"].lower())

    def test_pertanyaan_kontak_memuat_telepon_dan_email(self):
        self._pasang_tool(
            _hasil_cari(
                _item_cari(
                    URL_KONTAK,
                    "Kontak - Universitas Islam Riau",
                    "Telepon dan email resmi UIR",
                )
            ),
            {
                URL_KONTAK: _hasil_baca(
                    URL_KONTAK, "Kontak - Universitas Islam Riau", TEKS_KONTAK
                )
            },
        )
        ork = self._orkestrator(_respons())

        hasil = ork.tanya("Apa kontak UIR?")

        self.assertTrue(hasil["ok"])
        self.assertIn("Telepon: +62 761 000111", hasil["jawaban"])
        self.assertIn("Email : info[at]contoh.ac.id", hasil["jawaban"])
        self.assertEqual(hasil["sumber"], [URL_KONTAK])


class UjiAturanPromptLokasi(unittest.TestCase):
    def test_prompt_melarang_artikel_cbt_sebagai_alamat(self):
        for penanda in (
            "Kontak/Profil/Maps resmi",
            "CBT",
            "bukan alamat resmi universitas",
            "belum dapat diverifikasi",
        ):
            with self.subTest(penanda=penanda):
                self.assertIn(penanda, SYSTEM_PROMPT)


class UjiTidakAdaAlamatDiSource(unittest.TestCase):
    def test_alamat_asli_tidak_disimpan_di_kode(self):
        akar = Path(__file__).resolve().parents[1]

        for relatif in (
            "app/perencana.py",
            "app/orchestrator.py",
            "tools/cari_uir.py",
            "app/system_prompt.py",
        ):
            with self.subTest(file=relatif):
                isi = (akar / relatif).read_text(encoding="utf-8")
                self.assertNotIn("Kaharuddin Nasution", isi)


if __name__ == "__main__":
    unittest.main()
