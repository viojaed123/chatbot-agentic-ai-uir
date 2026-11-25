"""Parser deterministik untuk katalog fakultas dan program studi UIR.

Modul ini HANYA membaca teks yang sudah diekstrak oleh Tool 2
(:mod:`tools.ambil_halaman`). Tidak ada panggilan jaringan, tidak ada model AI,
dan tidak ada tebakan. Semua daftar yang dikembalikan berasal dari baris yang
benar-benar ada di halaman ``https://uir.ac.id/program-pendidikan/``.

Struktur yang dikenali:

* bagian ``FAKULTAS DAN PASCASARJANA`` -> daftar fakultas;
* tabel ``Nama Prodi / Jenjang / Skor Nasional`` -> daftar program studi
  beserta jenjang dan akreditasinya;
* label seperti ``Gen Unggul`` dan ``PMB UIR`` -> label navigasi, bukan prodi.
"""

import re
from functools import lru_cache

# Penanda bagian pada teks hasil Tool 2.
_PENANDA_FAKULTAS = "fakultas dan pascasarjana"
_PENANDA_TABEL = "nama prodi"
_AWAL_FAKULTAS = "fakultas "

# Baris yang menutup bagian fakultas pada halaman UIR.
_AKHIR_FAKULTAS = (
    "fasilitas penunjang",
    "daftar sekarang",
    "lihat akreditasi",
    "tabel akreditasi",
    "skip to content",
)

# Label yang memang muncul di halaman tetapi bukan entitas katalog.
_LABEL_NAVIGASI = (
    "Gen Unggul",
    "PMB UIR",
    "Kontak",
    "Maps",
    "Menu",
    "Program Studi",
    "Mahasiswa Aktif",
    "Alumni",
    "Daftar Sekarang",
    "Lihat Akreditasi",
    "Lihat Sertifikat Akreditasi",
    "Skip to content",
)

_JENJANG_RE = re.compile(r"^(?:S1|S2|S3|D4|D3|PROFESI)$", re.I)

# Nilai akreditasi yang memang digunakan pada tabel sumber.
AKREDITASI = ("BAIK SEKALI", "UNGGUL", "BAIK", "A", "B", "C")
AKREDITASI_RE = re.compile(
    r"\b(?:" + "|".join(AKREDITASI) + r")\b",
    re.I,
)


def _baris(teks):
    """Ubah teks menjadi baris bersih, tanpa baris kosong."""
    if not isinstance(teks, str):
        return []

    hasil = []
    for baris in teks.splitlines():
        bersih = re.sub(r"\s+", " ", baris).strip()
        if bersih:
            hasil.append(bersih)
    return hasil


def _normalisasi_nama(nama):
    """Samakan nama untuk perbandingan.

    ``Pendidikan Islam Anak Usia Dini (PIAUD)`` dan
    ``Pendidikan Islam Anak Usia Dini`` dianggap nama yang sama.
    """
    nilai = str(nama).strip()
    nilai = re.sub(r"\s*\([^)]*\)\s*$", "", nilai)
    nilai = re.sub(r"\s*[—–:-].*$", "", nilai)
    return nilai.strip(" .;")


def _bagian_fakultas(baris):
    """Ambil fakultas dari bagian ``FAKULTAS DAN PASCASARJANA``."""
    indeks = None
    for i, nilai in enumerate(baris):
        if nilai.lower() == _PENANDA_FAKULTAS:
            indeks = i
            break

    if indeks is None:
        return [], []

    fakultas = []
    pascasarjana = []

    for nilai in baris[indeks + 1:]:
        rendah = nilai.lower()

        if any(rendah.startswith(tanda) for tanda in _AKHIR_FAKULTAS):
            break

        if rendah.startswith(_AWAL_FAKULTAS):
            fakultas.append(nilai)
        elif rendah == "pascasarjana":
            pascasarjana.append(nilai)

    return fakultas, pascasarjana


def _tabel_program_studi(baris):
    """Ambil baris tabel ``Nama Prodi / Jenjang / Skor Nasional``."""
    indeks = None
    for i, nilai in enumerate(baris):
        if nilai.lower() == _PENANDA_TABEL:
            indeks = i
            break

    if indeks is None or indeks + 2 >= len(baris):
        return []

    if (
        baris[indeks + 1].lower() != "jenjang"
        or baris[indeks + 2].lower() != "skor nasional"
    ):
        return []

    hasil = []
    terlihat = set()
    i = indeks + 3

    # Setiap baris tabel terdiri dari tiga sel berurutan. Bila ada baris asing,
    # geser satu baris dan cari lagi pola tiga sel tersebut.
    while i + 2 < len(baris):
        nama = baris[i].strip()
        jenjang = baris[i + 1].strip()
        akreditasi = baris[i + 2].strip().upper()

        if (
            _JENJANG_RE.match(jenjang)
            and akreditasi in AKREDITASI
        ):
            nama_norm = _normalisasi_nama(nama)
            kunci = nama_norm.lower()

            if len(nama_norm) >= 3 and kunci not in terlihat:
                hasil.append({
                    "nama": nama,
                    "nama_norm": nama_norm,
                    "jenjang": jenjang.upper(),
                    "akreditasi": akreditasi,
                })
                terlihat.add(kunci)

            i += 3
        else:
            i += 1

    return hasil


@lru_cache(maxsize=32)
def parse_katalog(teks):
    """Kembalikan bagian katalog yang terstruktur dari teks Tool 2.

    Nilai ``label_navigasi`` hanya sebagai informasi klasifikasi. Verifikasi
    klaim tidak boleh memakai label ini sebagai bukti fakultas maupun prodi.
    """
    baris = _baris(teks)
    fakultas, pascasarjana = _bagian_fakultas(baris)
    program = _tabel_program_studi(baris)
    rendah = {nilai.lower() for nilai in baris}

    return {
        "fakultas": fakultas,
        "pascasarjana": pascasarjana,
        "program_studi": program,
        "nama_prodi": [item["nama"] for item in program],
        "nama_prodi_norm": [item["nama_norm"] for item in program],
        "label_navigasi": [
            label for label in _LABEL_NAVIGASI
            if label.lower() in rendah
        ],
        "jenjang": sorted({item["jenjang"] for item in program}),
        "akreditasi": sorted({item["akreditasi"] for item in program}),
    }


def peta_akreditasi(teks):
    """Peta nama program studi (huruf kecil) -> akreditasi dari tabel."""
    hasil = {}
    for item in parse_katalog(teks)["program_studi"]:
        akreditasi = item["akreditasi"]
        hasil[item["nama"].lower()] = akreditasi
        hasil[item["nama_norm"].lower()] = akreditasi
    return hasil
