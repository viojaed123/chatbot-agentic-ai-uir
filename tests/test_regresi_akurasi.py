"""Regresi akurasi: katalog resmi, status penolakan, dan jawaban kanonik."""

import unittest
from unittest import mock

from app import orchestrator as modul


TEKS_KATALOG = """
FAKULTAS DAN PASCASARJANA
Fakultas Hukum
Fakultas Teknik
Tabel Akreditasi Program Studi
Nama Prodi
Ilmu Hukum
S1
A
""" + ("Informasi akreditasi dan program studi UIR. " * 10)

URL_KATALOG = "https://uir.ac.id/program-pendidikan/"
URL_BERITA = (
    "https://uir.ac.id/newsfakultas/fakultas-hukum-gelar-conference/"
)

TEKS_PROFIL = """
Gen Unggul
PMB UIR
Sejarah Singkat
Universitas Islam Riau adalah perguruan tinggi tertua di Provinsi Riau
berdiri pada tanggal 4 September 1962 bertepatan dengan 23 Zulkaidah 1382 H,
dibawah Yayasan Lembaga Pendidikan Islam (YLPI) Riau. Tokoh Pejuang
Universitas Islam Riau yaitu H. Soeman Hasibuan.
""" + ("Informasi sejarah dan visi misi Universitas Islam Riau. " * 10)

URL_PROFIL = "https://uir.ac.id/profil/"
PERTANYAAN_PROFIL = "Halo, apa itu Universitas Islam Riau?"

PERTANYAAN = "Apa saja fakultas yang tersedia di UIR?"

PERTANYAAN_BERDIRI = "Kapan Universitas Islam Riau berdiri?"
PERTANYAAN_LUAR = "Siapa presiden Indonesia saat ini?"
PERTANYAAN_2030 = "Berapa jumlah mahasiswa UIR pada tahun 2030?"


class _KlienPalsu:
    """Klien tanpa jaringan: hanya mengantre respons teks."""

    def __init__(self, *konten):
        self.antrean = list(konten)
        self.panggilan = []

    def chat(self, messages, tools=None):
        self.panggilan.append(1)
        item = self.antrean.pop(0)
        return _respons_teks(item) if isinstance(item, str) else item


def _respons_teks(konten):
    return {"message": {"role": "assistant", "content": konten, "tool_calls": []}}


def _respons_tool(nama, argumen):
    return {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "function": {"name": nama, "arguments": argumen}
            }],
        }
    }


def _hasil_cari(url, judul):
    return {
        "ok": True,
        "status": "ditemukan",
        "count": 1,
        "sumber": "wp_rest",
        "results": [{
            "judul": judul,
            "url": url,
            "cuplikan": "Halaman resmi UIR.",
            "subtype": "page",
            "id": 1,
        }],
    }


def _cache_profil():
    return {
        "ok": True,
        "status": "ditemukan",
        "url": URL_PROFIL,
        "data": {
            "url": URL_PROFIL,
            "judul": "Profil – Sejarah dan Visi Misi UIR - Universitas Islam Riau",
            "teks": TEKS_PROFIL,
            "panjang": len(TEKS_PROFIL),
            "waktu": "2026-11-24T12:00:00+00:00",
        },
        "waktu_cache": "2026-11-24T12:00:00+00:00",
        "kedaluwarsa_pada": "2026-11-25T12:00:00+00:00",
        "pesan": "Cache ditemukan.",
    }


def _rekam_mahasiswa():
    return {
        "url": "https://uir.ac.id/berita/mahasiswa-uir-aktif/",
        "judul": "Mahasiswa UIR aktif",
        "teks": "Mahasiswa UIR mengikuti kegiatan kampus tahun 2024. " * 10,
        "sumber": "baca_cache",
    }


def _rekam():
    return {
        "url": URL_KATALOG,
        "judul": "Program Pendidikan - Universitas Islam Riau",
        "teks": TEKS_KATALOG,
        "sumber": "baca_cache",
    }


def _rekam_profil(url=URL_PROFIL):
    return {
        "url": url,
        "judul": "Profil – Sejarah dan Visi Misi UIR - Universitas Islam Riau",
        "teks": TEKS_PROFIL,
        "sumber": "baca_cache",
    }


class UjiRegresiAkurasi(unittest.TestCase):
    def test_katalog_didahulukan_dari_berita(self):
        self.assertLess(
            modul._prioritas_url(URL_KATALOG),
            modul._prioritas_url(URL_BERITA),
        )

    def test_status_penolakan_memakai_bukti_sesi(self):
        ork = modul.Orkestrator(client=object(), verifikasi_wajib=False)
        ork.bukti = [_rekam()]
        hasil = ork._reject("alasan uji")
        self.assertEqual(hasil["status"], "bukti_tidak_cukup")

        kosong = modul.Orkestrator(client=object(), verifikasi_wajib=False)
        self.assertEqual(
            kosong._reject("alasan uji")["status"],
            "sumber_tidak_tersedia",
        )

    def test_jawaban_fakultas_disusun_dari_parser(self):
        jawaban = modul._katalog_answer(
            "- Fakultas Hukum\n- Fakultas Rekaan",
            [_rekam()],
            PERTANYAAN,
            hanya_prodi=False,
        )

        self.assertIsNotNone(jawaban)
        self.assertIn("Fakultas Hukum", jawaban)
        self.assertIn("Fakultas Teknik", jawaban)
        self.assertNotIn("Fakultas Rekaan", jawaban)
        self.assertIn(URL_KATALOG, jawaban)

    def test_butir_didukung_membedakan_kesalahan_sebagian(self):
        rekam = _rekam()

        self.assertTrue(
            modul._ada_butir_didukung(
                "- Fakultas Hukum\n- Fakultas Rekaan",
                [rekam],
                "fakultas",
            )
        )
        self.assertFalse(
            modul._ada_butir_didukung(
                "- Fakultas Rekaan",
                [rekam],
                "fakultas",
            )
        )

    def test_katalog_relevan_walaupun_skor_teks_rendah(self):
        ork = modul.Orkestrator(client=object(), verifikasi_wajib=False)
        ork.pertanyaan = "Fakultas saja?"
        ork.bukti = [_rekam()]

        self.assertEqual(ork._relevant_records(), [_rekam()])

    def test_pertanyaan_profil_dikenali(self):
        self.assertTrue(modul._adalah_profil_pertanyaan(PERTANYAAN_PROFIL))
        self.assertFalse(modul._adalah_profil_pertanyaan(PERTANYAAN))

    def test_halaman_profil_resmi_dikenali(self):
        self.assertTrue(modul._authoritative_profile(_rekam_profil()))
        self.assertFalse(
            modul._authoritative_profile(
                _rekam_profil("https://uir.ac.id/?elementor_library=profil-uir")
            )
        )
        self.assertFalse(modul._authoritative_profile(_rekam()))

    def test_jawaban_profil_diambil_dari_sumber(self):
        jawaban = modul._profil_answer([_rekam_profil()])

        self.assertIsNotNone(jawaban)
        self.assertIn("4 September 1962", jawaban)
        self.assertIn(URL_PROFIL, jawaban)
        self.assertNotIn("1981", jawaban)
        self.assertNotIn("ATIKOM", jawaban)

    def test_profil_relevan_walaupun_skor_teks_rendah(self):
        ork = modul.Orkestrator(client=object(), verifikasi_wajib=False)
        ork.pertanyaan = PERTANYAAN_PROFIL
        ork.bukti = [_rekam_profil()]

        self.assertEqual(ork._relevant_records(), [_rekam_profil()])

    def test_ekstraksi_butir_mengabaikan_bagian_sumber(self):
        jawaban = (
            "- Fakultas Hukum\n"
            "- Fakultas Teknik\n\n"
            "Sumber:\n- https://uir.ac.id/program-pendidikan/\n"
        )

        self.assertEqual(
            modul._extract_answer_items(jawaban),
            ["Fakultas Hukum", "Fakultas Teknik"],
        )

    def test_permintaan_satu_butir_tidak_diwajibkan_lengkap(self):
        self.assertFalse(modul._butuh_katalog(
            "Dari jawaban sebelumnya, sebutkan satu fakultas saja."
        ))
        self.assertTrue(modul._butuh_katalog(PERTANYAAN))

    def test_klaim_prosa_satu_fakultas_diterima(self):
        jawaban = (
            "Dari jawaban sebelumnya, satu fakultas yang ada di UIR "
            "adalah Fakultas Teknik.\n\nSumber:\n- " + URL_KATALOG
        )

        valid, alasan = modul._claims_supported(
            jawaban,
            [_rekam()],
            "Dari jawaban sebelumnya, sebutkan satu fakultas saja.",
        )

        self.assertTrue(valid, alasan)

    # ------------------------------------------------------------------
    # Regresi temuan uji Ollama nyata: tahun berdiri, luar cakupan, 2030.
    # ------------------------------------------------------------------
    def test_pertanyaan_tahun_berdiri_dikenali(self):
        self.assertTrue(modul._adalah_pertanyaan_tahun_berdiri(PERTANYAAN_BERDIRI))
        self.assertFalse(modul._adalah_pertanyaan_tahun_berdiri(PERTANYAAN))
        self.assertFalse(modul._adalah_profil_pertanyaan(PERTANYAAN_BERDIRI))

    def test_halaman_profil_didahulukan_dari_berita(self):
        profil = "https://uir.ac.id/profil/"
        berita = "https://uir.ac.id/dua-tahun-berdiri-program-studi/"
        artefak = "https://uir.ac.id/?elementor_library=profil-uir"

        self.assertLess(
            modul._prioritas_url_profil(profil),
            modul._prioritas_url_profil(berita),
        )
        self.assertGreater(
            modul._prioritas_url_profil(artefak),
            modul._prioritas_url_profil(profil),
        )

    def test_tahun_berdiri_diambil_dari_profil_resmi(self):
        berita = (
            "https://uir.ac.id/dua-tahun-berdiri-program-studi-doktor/"
        )
        klien = _KlienPalsu(
            _respons_tool(
                "cari_uir",
                {"query": "Kapan Universitas Islam Riau berdiri?"},
            ),
            "UIR berdiri pada tahun 2024 menurut berita tersebut.",
        )

        dengan_cari = mock.patch.object(
            modul, "cari_uir",
            side_effect=[
                _hasil_cari(berita, "Berita pendirian program studi"),
                _hasil_cari(URL_PROFIL, "Profil – Sejarah dan Visi Misi UIR"),
            ],
        )
        dengan_baca = mock.patch.object(
            modul, "baca_cache", return_value=_cache_profil()
        )

        with dengan_cari as cari, dengan_baca:
            ork = modul.Orkestrator(client=klien, verifikasi_wajib=True)
            hasil = ork.tanya(PERTANYAAN_BERDIRI)

        self.assertTrue(hasil["ok"], hasil["pesan"])
        self.assertIn("4 September 1962", hasil["jawaban"])
        self.assertIn(URL_PROFIL, hasil["jawaban"])
        self.assertNotIn(berita, hasil["jawaban"])
        self.assertEqual(cari.call_args_list[0], mock.call(
            query="Kapan Universitas Islam Riau berdiri?"
        ))
        self.assertEqual(cari.call_args_list[-1], mock.call(query="profil UIR"))
        self.assertEqual(len(klien.panggilan), 2)

    def test_profil_relevan_untuk_pertanyaan_tahun_berdiri(self):
        ork = modul.Orkestrator(client=object(), verifikasi_wajib=False)
        ork.pertanyaan = PERTANYAAN_BERDIRI
        ork.bukti = [_rekam_profil()]

        self.assertEqual(ork._relevant_records(), [_rekam_profil()])

    def test_pertanyaan_luar_cakupan_dikenali(self):
        self.assertTrue(modul._adalah_pertanyaan_di_luar_cakupan(PERTANYAAN_LUAR))
        self.assertFalse(modul._adalah_pertanyaan_di_luar_cakupan(PERTANYAAN))
        self.assertFalse(modul._adalah_pertanyaan_di_luar_cakupan(PERTANYAAN_2030))

    def test_luar_cakupan_ditolak_tanpa_memanggil_model(self):
        klien = _KlienPalsu("Joko Widodo adalah presiden Indonesia.")
        ork = modul.Orkestrator(client=klien, verifikasi_wajib=True)

        hasil = ork.tanya(PERTANYAAN_LUAR)

        self.assertFalse(hasil["ok"])
        self.assertEqual(klien.panggilan, [])
        self.assertNotIn("Joko Widodo", hasil["jawaban"])

    def test_tahun_depan_dan_ketersediaannya(self):
        self.assertEqual(modul._tahun_diminta(PERTANYAAN_2030), {"2030"})
        self.assertEqual(modul._tahun_diminta(PERTANYAAN), set())
        self.assertFalse(
            modul._tahun_tersedia_di_bukti({"2030"}, [_rekam_mahasiswa()])
        )
        self.assertTrue(
            modul._tahun_tersedia_di_bukti(
                {"2024"}, [_rekam_mahasiswa()]
            )
        )

    def test_tahun_di_artikel_tidak_terkait_tidak_dianggap_bukti(self):
        rekam = {
            "url": "https://uir.ac.id/minyak/",
            "judul": "Produksi minyak",
            "teks": (
                "SKK Migas mencanangkan satu juta barrel per hari pada "
                "tahun 2030 di beberapa lapangan raksasa."
            ),
            "sumber": "baca_cache",
        }

        self.assertEqual(
            modul._kata_topik_pertanyaan(PERTANYAAN_2030),
            {"jumlah", "mahasiswa"},
        )
        self.assertFalse(
            modul._tahun_tersedia_di_bukti(
                {"2030"}, [rekam], {"jumlah", "mahasiswa"}
            )
        )

    def test_satu_butir_diambil_dari_katalog(self):
        jawaban = modul._satu_butir_katalog(
            [_rekam()], "fakultas"
        )

        self.assertIsNotNone(jawaban)
        self.assertIn("Fakultas Hukum", jawaban)
        self.assertIn(URL_KATALOG, jawaban)
        self.assertNotIn("Fakultas Teknik", jawaban)

    def test_tahun_tanpa_bukti_ditolak_walaupun_model_menolak_halus(self):
        klien = _KlienPalsu(
            "Saya tidak dapat memberikan angka pasti untuk tahun 2030."
        )
        ork = modul.Orkestrator(client=klien, verifikasi_wajib=True)
        ork.bukti = [_rekam_mahasiswa()]

        hasil = ork.tanya(PERTANYAAN_2030)

        self.assertFalse(hasil["ok"])
        self.assertEqual(hasil["status"], "bukti_tidak_cukup")
        self.assertIn("tahun yang diminta", hasil["pesan"])
        self.assertEqual(len(klien.panggilan), 1)


if __name__ == "__main__":
    unittest.main()
