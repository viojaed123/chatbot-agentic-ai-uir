"""Perencana strategi pencarian untuk pertanyaan informasi UIR.

Modul ini HANYA menentukan strategi pencarian:

* jenis pertanyaan (daftar, prosedur, identitas, waktu, angka, penjelasan),
* topik yang disebut dalam pertanyaan,
* variasi kata kunci yang perlu dicari,
* apakah sitemap perlu dipakai.

Modul ini TIDAK menyimpan jawaban, tidak memuat nama pejabat, daftar
beasiswa, daftar fakultas, atau langkah pendaftaran. Semua fakta tetap
harus berasal dari halaman uir.ac.id yang berhasil dibaca.
"""

import re


# Urutan penting: "Bagaimana cara" lebih spesifik daripada "apa", dan
# "Siapa" lebih spesifik daripada penyebutan topik pimpinan.
_JENIS_PERTANYAAN = (
    ("prosedur", (
        "bagaimana", "cara ", "cara\n", "langkah", "prosedur", "tata cara",
        "proses pendaftaran", "proses daftar",
    )),
    ("identitas", (
        "siapa ", "siapa\n", "nama ", "jabatan", "pimpinan", "rektor",
        "dekan", "wakil rektor",
    )),
    ("waktu", ("kapan ", "kapan\n", "tanggal ", "periode ", "jam ", "waktu ")),
    ("angka", ("berapa ", "berapa\n", "jumlah ", "nominal ", "biaya ")),
    ("daftar", (
        "apa saja", "daftar ", "sebutkan", "seluruh", "semua ",
        "tersedia", "yang ada",
    )),
    ("penjelasan", ("jelaskan", "uraikan", "ceritakan", "informasi ")),
)

# Kata kunci deteksi topik. Bukan jawaban dan bukan daftar entitas.
_TOPIK_PERTANYAAN = (
    (
        "beasiswa",
        (
            "beasiswa", "scholarship", "bantuan pendidikan",
            "bantuan biaya pendidikan", "bantuan pendidikan tinggi",
        ),
    ),
    (
        "pmb",
        (
            "penerimaan mahasiswa baru", "pmb", "pendaftaran mahasiswa",
            "cara daftar", "jalur masuk", "calon mahasiswa",
            "mahasiswa baru", "pendaftaran uir",
        ),
    ),
    ("fakultas", ("fakultas",)),
    ("program studi", ("program studi", "prodi ", "prodi?", "jurusan")),
    ("akreditasi", ("akreditasi", "akreditas")),
    (
        "dosen",
        (
            "dosen", "profesor", "guru besar", "guru-besar",
        ),
    ),
    (
        "pimpinan",
        (
            "pimpinan", "rektor", "dekan", "wakil rektor",
            "struktur organisasi", "pemimpin",
        ),
    ),
    (
        "akademik",
        (
            "akademik", "kalender akademik", "kurikulum", "semester",
            "krs", "ipk",
        ),
    ),
    (
        "biaya",
        (
            "biaya kuliah", "biaya pendidikan", "biaya pendaftaran",
            "uang kuliah", "spp", "tarif",
        ),
    ),
    (
        "fasilitas",
        (
            "fasilitas", "sarana", "prasarana", "laboratorium",
            "perpustakaan", "asrama",
        ),
    ),
    (
        "kemahasiswaan",
        (
            "kemahasiswaan", "organisasi mahasiswa", "ukm",
            "himpunan mahasiswa", "kegiatan mahasiswa",
        ),
    ),
    (
        "sejarah",
        (
            "sejarah", "berdiri", "didirikan", "profil uir",
            "profil universitas islam riau", "tentang uir",
        ),
    ),
    (
        "lokasi",
        (
            "lokasi kampus", "lokasi uir", "lokasi universitas",
            "alamat kampus", "alamat uir", "alamat universitas",
            "kontak uir", "hubungi uir", "telepon uir", "email uir",
            "maps uir", "peta kampus", "letak kampus",
            "di mana kampus", "dimana kampus",
        ),
    ),
)

# Variasi pencarian dosen memakai prefiks site:uir.ac.id sebagai penanda niat.
# tools/cari_uir.py sudah membatasi domain, jadi prefiks ini dibuang sebelum
# permintaan REST/sitemap dikirim (lihat _tanpa_site di modul tersebut).
# Ini BUKAN daftar nama atau jawaban.
_VARIASI_DOSEN = (
    'site:uir.ac.id "Guru Besar" "Teknik"',
    'site:uir.ac.id "Profesor" "Fakultas Teknik"',
    'site:uir.ac.id "Guru Besar" UIR',
    'site:uir.ac.id "Fakultas Teknik" dosen profesor',
    'site:uir.ac.id profesor Universitas Islam Riau',
    'site:uir.ac.id profil dosen Fakultas Teknik UIR',
    'site:uir.ac.id "Guru Besar" Universitas Islam Riau',
    'site:uir.ac.id dosen profesor Universitas Islam Riau',
)

# Variasi untuk lokasi/alamat/kontak kampus. Ini HANYA kata kunci pencarian;
# alamat, telepon, dan email tetap diambil dari halaman yang dibaca.
_VARIASI_LOKASI = (
    "kontak UIR",
    "kontak universitas islam riau",
    "alamat kampus UIR",
    "lokasi kampus UIR",
    "maps UIR",
)

_VARIASI_PETA = (
    "maps UIR",
    "kontak UIR",
    "kontak universitas islam riau",
    "alamat kampus UIR",
)


# Strategi pencarian. Setiap nilai adalah kata kunci, BUKAN potongan jawaban.
_STRATEGI_TOPIK = {
    "beasiswa": (
        "beasiswa UIR",
        "beasiswa mahasiswa UIR",
        "informasi beasiswa UIR",
        "beasiswa internal UIR",
        "beasiswa eksternal untuk mahasiswa UIR",
        "beasiswa pemerintah",
        "informasi bantuan pendidikan UIR",
        "informasi beasiswa dari unit terkait UIR",
    ),
    "pmb": (
        "penerimaan mahasiswa baru UIR",
        "PMB UIR",
        "pendaftaran mahasiswa baru UIR",
        "jalur masuk UIR",
        "prosedur pendaftaran UIR",
        "persyaratan mahasiswa baru UIR",
        "pembelian akun PMB UIR",
        "seleksi mahasiswa baru UIR",
        "biaya pendaftaran UIR",
        "jadwal penerimaan mahasiswa baru UIR",
    ),
    "dosen": _VARIASI_DOSEN,
    "pimpinan": (
        "pimpinan UIR",
        "profil rektor UIR",
        "pengangkatan rektor UIR",
        "struktur organisasi UIR",
        "rektor universitas islam riau",
    ),
    "fakultas": (
        "daftar fakultas",
        "fakultas dan pascasarjana",
        "program pendidikan",
    ),
    "program studi": (
        "daftar program studi",
        "nama prodi",
        "tabel akreditasi program studi",
    ),
    "akreditasi": (
        "akreditasi program studi",
        "tabel akreditasi program studi",
        "nama prodi akreditasi",
    ),
    "akademik": (
        "akademik UIR",
        "kalender akademik UIR",
        "informasi akademik UIR",
    ),
    "biaya": (
        "biaya kuliah UIR",
        "biaya pendidikan UIR",
        "biaya pendaftaran UIR",
    ),
    "fasilitas": (
        "fasilitas UIR",
        "sarana prasarana UIR",
        "fasilitas kampus UIR",
    ),
    "kemahasiswaan": (
        "kemahasiswaan UIR",
        "organisasi mahasiswa UIR",
        "kegiatan kemahasiswaan UIR",
    ),
    "sejarah": (
        "profil UIR",
        "sejarah singkat UIR",
        "sejarah universitas islam riau",
    ),
    "lokasi": _VARIASI_LOKASI,
}

# Topik yang halaman indeks resminya penting sehingga sitemap dipakai.
_SITEMAP_TOPIK = frozenset({
    "beasiswa", "pmb", "dosen", "pimpinan", "fakultas", "program studi",
    "akreditasi", "akademik", "biaya", "fasilitas", "kemahasiswaan",
    "lokasi",
})

_PENANDA_STRATEGI = re.compile(
    r"\b(apa|apa saja|bagaimana|cara|siapa|kapan|berapa|jelaskan|"
    r"daftar|sebutkan|informasi|prosedur|langkah|status|tersedia|"
    r"proses|senarai|katalog|lokasi|alamat|kontak|hubungi|maps|peta)\b|\?",
    re.I,
)


def _teks(pertanyaan):
    return re.sub(r"\s+", " ", str(pertanyaan or "").strip().lower())


# Kata kunci untuk memahami pertanyaan dosen/profesor. Ini BUKAN daftar nama
# atau jawaban; hanya penanda topik dan atribut yang boleh dicari.
_PENANDA_DOSEN = ("dosen", "profesor", "guru besar", "guru-besar")
_PENANDA_GURU_BESAR = ("guru besar", "guru-besar")
_PENANDA_PROFESOR = ("profesor",)
_PENANDA_UIR = (
    "uir", "universitas islam riau", "universitas", "kampus",
    "lingkungan uir", "lingkungan universitas",
)

# Penanda niat lokasi/kontak. Bukan fakta: tidak ada alamat, telepon, atau
# email yang disimpan di sini.
_PENANDA_LOKASI = (
    "lokasi", "alamat", "letak", "kontak", "hubungi", "telepon",
    "telp", "email", "maps", "peta", "di mana", "dimana",
)
_PENANDA_KONTAK = ("kontak", "hubungi", "telepon", "telp", "email")
_PENANDA_PETA = ("maps", "peta")
_PENANDA_ALAMAT = ("alamat", "letak")

# Pertanyaan dengan konteks kegiatan/ujian bukan permintaan alamat kampus.
_PENANDA_KONTEKS_ARTIKEL = (
    "cbt", "ujian", "jadwal", "acara", "seminar", "workshop",
    "kegiatan", "pendaftaran", "pmb", "wisuda", "konferensi",
)

# Topik lain yang lebih spesifik tidak boleh direbut oleh kata "lokasi".
_PENANDA_TOPIK_LAIN = (
    "fakultas", "program studi", "prodi", "jurusan", "beasiswa",
    "akreditasi", "dosen", "profesor", "guru besar", "guru-besar",
    "rektor", "dekan", "fasilitas", "sarana", "perpustakaan",
    "laboratorium",
)


def pahami_lokasi(pertanyaan):
    """Pahami niat lokasi/alamat/kontak kampus tanpa menyimpan fakta UIR.

    Keluaran hanya petunjuk pencarian: jenis niat, kata kunci kanonik, dan
    variasi. Nilai alamat/telepon/email tetap harus diambil dari halaman
    uir.ac.id yang berhasil dibaca.
    """

    teks = _teks(pertanyaan)
    hasil = {
        "aktif": False,
        "kind": "",
        "uir": False,
        "artikel": False,
        "kanonik": "",
        "subkueri": [],
    }

    if not teks:
        return hasil

    hasil["uir"] = any(kata in teks for kata in _PENANDA_UIR)
    penanda_lokasi = any(kata in teks for kata in _PENANDA_LOKASI)
    di_mana_kampus = (
        ("di mana" in teks or "dimana" in teks)
        and ("kampus" in teks or "uir" in teks)
    )

    if not hasil["uir"] or not (penanda_lokasi or di_mana_kampus):
        return hasil

    hasil["artikel"] = any(
        kata in teks for kata in _PENANDA_KONTEKS_ARTIKEL
    )
    if hasil["artikel"]:
        return hasil

    if not any(kata in teks for kata in _PENANDA_KONTAK + _PENANDA_ALAMAT + _PENANDA_PETA):
        if any(kata in teks for kata in _PENANDA_TOPIK_LAIN):
            return hasil

    if any(kata in teks for kata in _PENANDA_PETA):
        hasil["kind"] = "peta"
        hasil["kanonik"] = "maps UIR"
        hasil["subkueri"] = list(_VARIASI_PETA)
    elif any(kata in teks for kata in _PENANDA_KONTAK):
        hasil["kind"] = "kontak"
        hasil["kanonik"] = "kontak UIR"
        hasil["subkueri"] = list(_VARIASI_LOKASI)
    elif any(kata in teks for kata in _PENANDA_ALAMAT):
        hasil["kind"] = "alamat"
        hasil["kanonik"] = "kontak UIR"
        hasil["subkueri"] = list(_VARIASI_LOKASI)
    else:
        hasil["kind"] = "lokasi"
        hasil["kanonik"] = "kontak UIR"
        hasil["subkueri"] = list(_VARIASI_LOKASI)

    hasil["aktif"] = True
    return hasil


def _variasi_lokasi(info):
    """Susun variasi kata kunci sesuai niat lokasi/kontak."""

    daftar = list(info.get("subkueri") or _VARIASI_LOKASI)
    for kata in _VARIASI_LOKASI:
        if kata not in daftar:
            daftar.append(kata)
    return daftar


def pahami_dosen(pertanyaan):
    """Pahami topik dosen/Profesor/Guru Besar tanpa menyimpan fakta UIR.

    Fungsi ini hanya menghasilkan petunjuk pencarian: kategori yang diminta,
    fakultas/unit yang dirujuk, atribut yang dicari, dan mode pertanyaan.
    Nama, jabatan, angka, dan tautan tetap harus berasal dari halaman yang
    dibaca.
    """

    teks = _teks(pertanyaan)
    aktif = any(kata in teks for kata in _PENANDA_DOSEN)

    hasil = {
        "aktif": aktif,
        "kategori": "",
        "mode": "",
        "unit": "",
        "teknik": False,
        "uir": False,
        "guru_besar": False,
        "profesor": False,
        "dosen": False,
        "lingkungan": False,
        "atribut": [],
    }

    if not aktif:
        return hasil

    hasil["guru_besar"] = any(
        kata in teks for kata in _PENANDA_GURU_BESAR
    )
    hasil["profesor"] = any(
        kata in teks for kata in _PENANDA_PROFESOR
    ) or "prof." in teks
    hasil["dosen"] = "dosen" in teks
    hasil["teknik"] = bool(re.search(r"\bteknik\b", teks))
    hasil["uir"] = any(kata in teks for kata in _PENANDA_UIR)
    hasil["lingkungan"] = (
        "lingkungan" in teks
        or "di uir" in teks
        or "di universitas islam riau" in teks
    )

    if hasil["teknik"]:
        hasil["unit"] = "Fakultas Teknik"
    elif hasil["uir"] or hasil["lingkungan"]:
        hasil["unit"] = "UIR"

    if hasil["guru_besar"] and hasil["teknik"]:
        hasil["kategori"] = "guru_besar"
        hasil["mode"] = "guru_besar_fakultas_teknik"
    elif hasil["guru_besar"]:
        hasil["kategori"] = "guru_besar"
        hasil["mode"] = "guru_besar_uir"
    elif hasil["dosen"] and hasil["profesor"] and hasil["teknik"]:
        hasil["kategori"] = "profesor"
        hasil["mode"] = "dosen_profesor_fakultas_teknik"
    elif hasil["lingkungan"] and hasil["teknik"]:
        hasil["kategori"] = "profesor"
        hasil["mode"] = "profesor_lingkungan_fakultas_teknik"
    elif hasil["lingkungan"]:
        hasil["kategori"] = "profesor"
        hasil["mode"] = "profesor_lingkungan_uir"
    elif hasil["teknik"]:
        hasil["kategori"] = "profesor"
        hasil["mode"] = "profesor_fakultas_teknik"
    else:
        hasil["kategori"] = (
            "dosen" if hasil["dosen"] and not hasil["profesor"] else "profesor"
        )
        hasil["mode"] = "profesor_uir"

    atribut = []
    if any(kata in teks for kata in (
        "siapa", "nama", "sebutkan", "siapa saja",
    )):
        atribut.append("nama")
    if any(kata in teks for kata in (
        "jabatan", "profesor", "guru besar", "guru-besar",
    )):
        atribut.append("jabatan")
    if any(kata in teks for kata in (
        "berapa", "jumlah", "ada berapa", "banyak",
    )):
        atribut.append("jumlah")
    if any(kata in teks for kata in (
        "program studi", "prodi", "jurusan",
    )):
        atribut.append("program studi")

    hasil["atribut"] = list(dict.fromkeys(atribut)) or ["nama", "jabatan"]
    return hasil


def _variasi_dosen(info):
    """Susun variasi kata kunci sesuai maksud pertanyaan dosen/profesor."""

    daftar = []
    pilihan = []

    if info.get("guru_besar"):
        pilihan.extend((0, 2, 6))
    if info.get("teknik"):
        pilihan.extend((1, 3, 5))
    pilihan.extend((4, 7))

    for indeks in dict.fromkeys(pilihan):
        if _VARIASI_DOSEN[indeks] not in daftar:
            daftar.append(_VARIASI_DOSEN[indeks])

    for kata in _VARIASI_DOSEN:
        if kata not in daftar:
            daftar.append(kata)

    return daftar


def jenis_pertanyaan(pertanyaan):
    """Klasifikasikan maksud pertanyaan untuk memilih strategi pencarian."""

    teks = _teks(pertanyaan)

    for nama, penanda in _JENIS_PERTANYAAN:
        if any(kata in teks for kata in penanda):
            return nama

    if teks.endswith("?"):
        return "penjelasan"

    return "umum"


def topik_pertanyaan(pertanyaan):
    """Kembalikan semua topik yang disebut, tanpa mengurutkan ulang fakta."""

    teks = _teks(pertanyaan)
    hasil = []

    for nama, penanda in _TOPIK_PERTANYAAN:
        for kata in penanda:
            pola = r"\b" + re.escape(kata.strip()) + r"\b"
            if re.search(pola, teks):
                if nama not in hasil:
                    hasil.append(nama)
                break

    # "program studi" lebih spesifik daripada "fakultas" bila keduanya ada;
    # urutan ini hanya memengaruhi prioritas kata kunci, bukan isi jawaban.
    if "program studi" in hasil and "fakultas" in hasil:
        hasil.remove("fakultas")
        hasil.insert(1, "fakultas")

    # Untuk pertanyaan dosen, kategori informasi lebih spesifik daripada
    # fakultas yang disebut sebagai unit kerja.
    if "dosen" in hasil:
        hasil.remove("dosen")
        hasil.insert(0, "dosen")

    # Kata "lokasi" pada berita CBT/PMB bukan permintaan alamat kampus.
    if "lokasi" in hasil and not pahami_lokasi(pertanyaan)["aktif"]:
        hasil.remove("lokasi")

    return hasil


def _perlu_variasi(pertanyaan):
    """Query polos seperti 'beasiswa' sengaja tetap satu kata kunci."""

    teks = _teks(pertanyaan)
    return bool(_PENANDA_STRATEGI.search(teks))


def _sitemap_utama(topik):
    """Satu kata kunci sitemap yang paling mewakili topik."""

    if not topik:
        return []

    nama = topik[0]
    kata = _STRATEGI_TOPIK.get(nama, (nama,))
    return [kata[0]]


def strategi_pencarian(pertanyaan, kanonik=None, batas=10):
    """Susun strategi pencarian untuk satu pertanyaan.

    ``kanonik`` dipakai bila pertanyaan berupa kata kunci polos agar perilaku
    lama tetap terjaga. ``batas`` membatasi jumlah subkueri agar situs UIR
    tidak diakses terlalu banyak.
    """

    teks = _teks(pertanyaan)
    jenis = jenis_pertanyaan(teks)
    topik = topik_pertanyaan(teks)
    dasar = str(kanonik or teks).strip()
    dosen = pahami_dosen(teks)
    lokasi = pahami_lokasi(teks)

    # Pertanyaan lokasi/alamat/kontak memakai halaman kontak/profil resmi,
    # bukan berita yang kebetulan memuat kata "lokasi" (misalnya jadwal CBT).
    if lokasi["aktif"]:
        subkueri = _variasi_lokasi(lokasi)[:max(1, int(batas))]
        return {
            "jenis": jenis,
            "topik": ["lokasi"],
            "subkueri": subkueri,
            "sitemap_subkueri": subkueri[:1],
            "butuh_sitemap": True,
            "pemahaman_lokasi": lokasi,
        }

    # Pertanyaan dosen/Profesor/Guru Besar memakai beberapa variasi kata kunci
    # resmi, bukan kalimat mentah dan bukan variasi daftar fakultas.
    if dosen["aktif"]:
        subkueri = _variasi_dosen(dosen)[:max(1, int(batas))]
        return {
            "jenis": jenis,
            "topik": topik,
            "subkueri": subkueri,
            "sitemap_subkueri": subkueri[:1],
            "butuh_sitemap": True,
            "pemahaman_dosen": dosen,
        }

    if not _perlu_variasi(teks):
        return {
            "jenis": jenis,
            "topik": topik,
            "subkueri": [dasar] if dasar else [],
            "sitemap_subkueri": [],
            "butuh_sitemap": False,
        }

    subkueri = []
    for nama in topik:
        for kata in _STRATEGI_TOPIK.get(nama, ()):
            if kata.lower() not in [item.lower() for item in subkueri]:
                subkueri.append(kata)

    if not subkueri:
        subkueri = [dasar] if dasar else []

    subkueri = subkueri[:max(1, int(batas))]

    return {
        "jenis": jenis,
        "topik": topik,
        "subkueri": subkueri,
        "sitemap_subkueri": _sitemap_utama(topik),
        "butuh_sitemap": bool(set(topik) & _SITEMAP_TOPIK),
    }
