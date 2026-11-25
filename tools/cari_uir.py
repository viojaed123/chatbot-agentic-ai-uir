
"""Tool cari_uir: mencari halaman publik di https://uir.ac.id/.

Pencarian menggunakan:
1. WordPress REST API.
2. Sitemap UIR.
3. Penggabungan dan pemeringkatan hasil dari kedua sumber.

Keamanan:
- Host harus uir.ac.id secara eksak.
- Hanya HTTPS dan port 443 yang diizinkan.
- Redirect ke luar domain ditolak.
- Robots.txt diperiksa sebelum halaman diakses.
- Timeout dan jeda antarpermintaan diterapkan.
- Tidak mengakses sistem akademik privat.
"""

import html
import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET

from datetime import datetime, timezone

from app import perencana


LOGGER = logging.getLogger(__name__)


# ============================================================
# KONFIGURASI
# ============================================================

ALLOWED_HOST = "uir.ac.id"
BASE_URL = "https://" + ALLOWED_HOST

ROBOTS_URL = BASE_URL + "/robots.txt"
SITEMAP_URL = BASE_URL + "/sitemap.xml"
REST_SEARCH_URL = BASE_URL + "/wp-json/wp/v2/search"

USER_AGENT = "CAPSTONE-UIR-Chatbot/0.1"

REQUEST_TIMEOUT = 10
JEDA_DETAIL = 1.0

# Hasil pencarian yang masih segar dipakai ulang untuk query yang sama.
# Ini hanya menyimpan URL/cuplikan hasil REST/sitemap, BUKAN isi halaman
# dan BUKAN bukti. Setiap jawaban tetap memerlukan baca_cache/ambil_halaman.
HASIL_CACHE_TTL_DETIK = 300
_HASIL_CACHE = {}
_HASIL_CACHE_LOCK = threading.Lock()

# Kandidat akhir dapat lebih banyak karena satu pertanyaan sekarang memakai
# beberapa variasi kata kunci. Permintaan per variasi tetap dibatasi agar
# situs UIR tidak dibebani terlalu banyak sekaligus.
MAX_KANDIDAT = 12
MAX_PER_HALAMAN = 5
MAX_DETAIL = 3
MAX_QUERY = 200
PANJANG_CUPLIKAN = 220

SITEMAP_DIIZINKAN = (
    "post-sitemap",
    "page-sitemap",
    "newsfakultas-sitemap",
    "penelitian-mingguini-sitemap",
    "pengabdian-masy-sitemap",
    "genunggul-sitemap",
)

STATUS_DITEMUKAN = "ditemukan"
STATUS_TIDAK_DITEMUKAN = "tidak_ditemukan"
STATUS_SITUS_TIDAK_TERSEDIA = "situs_tidak_tersedia"
STATUS_DIBLOKIR_ROBOTS = "diblokir_robots"
STATUS_VALIDASI_GAGAL = "validasi_gagal"

REST_KOLEKSI = {
    "post": "posts",
    "page": "pages",
}

_KATA_UMUM = frozenset({
    "apa", "apakah", "yang", "dan", "atau", "dari",
    "untuk", "dengan", "ini", "itu", "ada", "bagaimana",
    "berapa", "kapan", "siapa", "dimana", "mana", "mengapa",
    "kenapa", "sebuah", "para", "saya", "tolong",
    "mohon", "bisa", "dapat", "tentang", "di", "ke",
    "pada", "oleh", "sebagai", "adalah", "yaitu",
    "universitas", "islam", "riau", "uir", "kampus",
    "informasi", "resmi", "halaman", "sebutkan",
    "jelaskan", "berikan", "mengenai", "terkait",
    "tersedia", "terdapat", "dimiliki", "milik", "site",
})

_TOKEN_RELEVAN = re.compile(r"[a-z0-9]{3,}")

_ROBOTS_CACHE = None
_SITEMAP_CACHE = None

# Kunci ini mencegah dua permintaan bersamaan mengambil robots.txt berulang.
# Nilainya sengaja sederhana agar dapat di-mock dalam pengujian unit.
_ROBOTS_SEDANG_DIAMBIL = False
_ROBOTS_LOCK = threading.Lock()

# Penanda katalog resmi dan artikel/berita. Dipakai untuk memeringkat kandidat
# pada pertanyaan daftar; bukan sebagai bukti isi halaman.
_PENANDA_KATALOG = (
    "daftar fakultas", "daftar program studi", "katalog",
    "program-pendidikan", "program studi", "program-studi",
    "nama prodi", "tabel akreditasi", "akademik", "pendidikan",
)
_PENANDA_ARTIKEL = (
    "/berita", "berita-", "/berita/", "newsfakultas", "/post",
    "/tag/", "/category/", "/genunggul", "/penelitian", "/pengabdian",
    "pengumuman", "menggelar", "gelar ", "conference", "konferensi",
    "prosiding", "kunjungan", "launching", "resmikan", "jalin kerja",
    "kerja sama", "sukses", "raih", "resmi", "pendaftaran", "pmb",
    "pkkmb", "seminar", "workshop", "wisuda", "milad", "prestasi",
    "juara", "hadirkan", "keynote", "apel akbar", "mahasiswa",
    "buka ", "dibuka", "jawab kebutuhan", "daftar kuliah",
    "jangan asal", "pilih program", "kantongi izin",
    "kukuhkan", "mengukuhkan", "diukuhkan", "hantarkan", "raih gelar",
)

# Batasi jumlah subpertanyaan agar pencarian tetap ramah situs UIR.
# Beasiswa memakai 8 variasi dan PMB 10 variasi sesuai strategi topik.
MAX_SUBKUERI = 10

# Pemetaan ini HANYA strategi pencarian, bukan daftar fakta/jawaban.
# Kata kunci kanonik dipakai untuk mencari halaman; isi jawaban tetap berasal
# dari halaman yang benar-benar dibaca.
_PETA_TOPIK_PENCARIAN = (
    (
        "fakultas",
        ("fakultas",),
        (
            "daftar fakultas",
            "fakultas dan pascasarjana",
            "program pendidikan",
        ),
    ),
    (
        "program studi",
        ("program studi", "program pendidikan", "prodi", "jurusan"),
        (
            "daftar program studi",
            "nama prodi",
            "tabel akreditasi program studi",
        ),
    ),
    (
        "akreditasi",
        ("akreditasi", "akreditas"),
        (
            "akreditasi program studi",
            "tabel akreditasi program studi",
            "nama prodi akreditasi",
        ),
    ),
    (
        "sejarah",
        ("sejarah", "berdiri", "didirikan", "profil", "tentang uir"),
        (
            "profil UIR",
            "sejarah singkat UIR",
            "sejarah universitas islam riau",
        ),
    ),
    (
        "dosen",
        ("dosen", "profesor", "guru besar", "guru-besar"),
        (
            'site:uir.ac.id "Guru Besar" "Teknik"',
            'site:uir.ac.id "Profesor" "Fakultas Teknik"',
            'site:uir.ac.id profesor Universitas Islam Riau',
        ),
    ),
    (
        "pimpinan",
        ("pimpinan", "rektor", "dekan", "struktur organisasi", "pemimpin"),
        (
            "pimpinan UIR",
            "struktur organisasi UIR",
            "profil pimpinan UIR",
        ),
    ),
    (
        "fasilitas",
        ("fasilitas", "sarana", "prasarana", "laboratorium", "perpustakaan"),
        (
            "fasilitas UIR",
            "sarana prasarana UIR",
            "fasilitas kampus UIR",
        ),
    ),
    (
        "lokasi",
        (
            "lokasi kampus", "lokasi uir", "alamat kampus", "alamat uir",
            "kontak uir", "hubungi uir", "telepon uir", "email uir",
            "maps uir", "peta kampus",
        ),
        (
            "kontak UIR",
            "kontak universitas islam riau",
            "alamat kampus UIR",
            "lokasi kampus UIR",
            "maps UIR",
        ),
    ),
)


def _bonus_katalog(item):
    """Bobot katalog: halaman indeks resmi lebih tinggi dari halaman umum."""
    if not isinstance(item, dict):
        return 0

    teks = (
        str(item.get("judul", "")) + " "
        + str(item.get("url", ""))
    ).lower()

    if any(penanda in teks for penanda in (
        "program-pendidikan", "program-studi", "daftar fakultas",
        "daftar program studi", "nama prodi", "tabel akreditasi",
    )):
        return 12

    if any(penanda in teks for penanda in _PENANDA_KATALOG):
        return 4

    return 0


# ============================================================
# EXCEPTION
# ============================================================

class _SitusTidakTersedia(Exception):
    """Kegagalan jaringan, HTTP, atau format data."""


class _DiblokirRobots(Exception):
    """robots.txt melarang akses ke URL."""


# ============================================================
# VALIDASI URL
# ============================================================

def _validasi_url(url):
    """Hanya izinkan HTTPS dengan hostname tepat uir.ac.id."""

    if not isinstance(url, str) or not url.strip():
        return None

    try:
        parts = urllib.parse.urlsplit(url.strip())

        if parts.scheme.lower() != "https":
            return None

        if parts.username is not None or parts.password is not None:
            return None

        if parts.hostname != ALLOWED_HOST:
            return None

        if parts.port not in (None, 443):
            return None

    except ValueError:
        return None

    return urllib.parse.urlunsplit((
        "https",
        ALLOWED_HOST,
        parts.path or "/",
        parts.query,
        "",
    ))


# ============================================================
# PEMBERSIHAN TEKS
# ============================================================

def _bersihkan_teks(teks):
    """Hilangkan HTML dan rapikan teks."""

    if isinstance(teks, dict):
        teks = teks.get("rendered", "")

    if not isinstance(teks, str):
        return ""

    bersih = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>",
        " ",
        teks,
        flags=re.I | re.S,
    )

    bersih = re.sub(r"<[^>]+>", " ", bersih)
    bersih = html.unescape(bersih)

    bersih = "".join(
        karakter
        for karakter in bersih
        if karakter >= " " or karakter == "\t"
    )

    return re.sub(r"\s+", " ", bersih).strip()


def _salin_hasil(hasil):
    """Salinan hasil pencarian agar pemanggil tidak mengubah isi cache."""
    salinan = dict(hasil)
    salinan["results"] = [
        dict(item) for item in (hasil.get("results") or [])
    ]
    return salinan


def _token_topik(teks):
    """Ambil kata-kata penting dari teks."""

    return {
        kata
        for kata in _TOKEN_RELEVAN.findall(str(teks).lower())
        if kata not in _KATA_UMUM
    }


def _adalah_katalog(kata_kunci):
    """Apakah kata kunci meminta daftar/katalog fakultas atau program studi?"""

    # Pertanyaan "Guru Besar Fakultas Teknik" menyebut fakultas sebagai unit
    # kerja, bukan sebagai permintaan katalog daftar fakultas.
    if perencana.pahami_dosen(kata_kunci)["aktif"]:
        return False

    token = _token_topik(_tanpa_site(kata_kunci))
    return bool(
        token.intersection({
            "fakultas", "program", "studi", "prodi", "jurusan",
            "daftar", "katalog", "pendidikan", "akademik",
        })
    )


def _perlu_sitemap(kata_kunci):
    """Apakah topik ini memiliki halaman indeks resmi di sitemap?"""
    if _adalah_katalog(kata_kunci):
        return True

    # Halaman kontak/maps resmi sering tidak muncul di REST untuk kalimat
    # lokasi, sehingga sitemap dipakai bila niat lokasi benar-benar aktif.
    if perencana.pahami_lokasi(kata_kunci)["aktif"]:
        return True

    token = _token_topik(_tanpa_site(kata_kunci))

    # Kata "profil" saja sengaja tidak memaksa sitemap: REST sudah cukup
    # untuk pertanyaan umum, dan ini menjaga perilaku pencarian lama.
    return bool(token.intersection({
        "fasilitas", "sarana", "prasarana", "laboratorium",
        "perpustakaan", "pimpinan", "rektor", "dekan", "direktur",
        "akreditasi", "akreditas", "profesor", "dosen", "guru", "besar",
    }))


def _adalah_artikel(item):
    """Kenali kandidat berita/pengumuman dari judul, URL, dan provenannya."""
    if not isinstance(item, dict):
        return False

    teks = (
        str(item.get("judul", "")) + " "
        + str(item.get("url", ""))
    ).lower()

    if any(penanda in teks for penanda in _PENANDA_ARTIKEL):
        return True

    # Artikel WordPress sering memakai slug bertema akademik. Provenannya
    # lebih dapat dipercaya daripada kata katalog pada judul berita.
    if str(item.get("jenis", "")).lower() == "post":
        return True

    sumber = str(item.get("_sumber_sitemap", "")).lower()
    if sumber.startswith(("post-sitemap", "newsfakultas-sitemap")):
        return True

    return False


def _adalah_katalog_kandidat(item):
    """Kenali halaman katalog/daftar dari judul dan URL saja."""
    if not isinstance(item, dict):
        return False

    teks = (
        str(item.get("judul", "")) + " "
        + str(item.get("url", ""))
    ).lower()

    return any(penanda in teks for penanda in _PENANDA_KATALOG)


def _skor_kandidat(kata_kunci, item):
    """Skor relevansi + penalti berita + bonus katalog resmi."""
    kata_kunci = _tanpa_site(kata_kunci)
    skor = _skor_relevansi(
        kata_kunci,
        item.get("judul", "") if isinstance(item, dict) else "",
        item.get("cuplikan", "") if isinstance(item, dict) else "",
        item.get("url", "") if isinstance(item, dict) else "",
    )

    if _adalah_artikel(item):
        # Berita tidak boleh mendapat bonus katalog hanya karena slug-nya
        # memuat kata "program studi" atau "pendidikan".
        skor -= 8
    elif _adalah_katalog(kata_kunci) and _adalah_katalog_kandidat(item):
        # Bonus indeks katalog hanya untuk pertanyaan daftar fakultas/prodi.
        # Tanpa syarat ini, halaman /program-pendidikan/ selalu mengalahkan
        # halaman fasilitas atau pimpinan walaupun tidak relevan.
        skor += _bonus_katalog(item)

    info_lokasi = perencana.pahami_lokasi(kata_kunci)
    if info_lokasi["aktif"]:
        # Halaman kontak/maps/profil resmi lebih penting daripada artikel yang
        # hanya memuat kata "lokasi" pada judul (misalnya jadwal CBT).
        skor += _bonus_lokasi(item)

    info_dosen = perencana.pahami_dosen(kata_kunci)
    if info_dosen["aktif"]:
        # Profil dosen/Guru Besar lebih penting daripada berita yang hanya
        # menyebut kata profesor pada cuplikan pencarian.
        skor += _bonus_dosen(item)

        # Bila pertanyaan menarget Fakultas Teknik, halaman yang benar-benar
        # menyebut unit itu harus mengalahkan profesor dari fakultas lain.
        if info_dosen["teknik"] and isinstance(item, dict):
            label = " ".join((
                str(item.get("judul", "")),
                str(item.get("cuplikan", "")),
                str(item.get("url", "")),
            )).lower()
            if any(penanda in label for penanda in (
                "fakultas teknik", "fakultas-teknik", "teknik sipil",
                "teknik-sipil", "teknik perminyakan", "prodi teknik",
            )):
                skor += 8
            else:
                skor -= 4

    # Halaman dengan query string (mis. elementor_library) sering merupakan
    # artefak editor, bukan halaman publik yang stabil.
    if isinstance(item, dict):
        query_url = urllib.parse.urlsplit(
            str(item.get("url", ""))
        ).query.lower()

        if query_url:
            # Artefak editor Elementor kerap mengembalikan HTTP 401 dan
            # bukan halaman publik yang stabil.
            skor -= 20 if "elementor_library" in query_url else 6

    return skor


# ============================================================
# NORMALISASI QUERY
# ============================================================

def _tanpa_site(kata_kunci):
    """Buang prefiks ``site:uir.ac.id`` dan tanda kutip dari kata kunci.

    Prefiks dipakai pada variasi pertanyaan sebagai penanda niat pencarian pada
    domain resmi. Karena URL sudah divalidasi secara ketat oleh modul ini,
    prefiks tersebut tidak boleh dikirim ke REST/sitemap UIR.
    """

    nilai = re.sub(
        r"(?i)^\s*site\s*:\s*uir\.ac\.id\s*",
        "",
        str(kata_kunci or ""),
    )

    return nilai.replace('"', "").strip()


def _bonus_dosen(item):
    """Utamakan profil dosen/Guru Besar/Fakultas sebelum berita umum."""

    if not isinstance(item, dict):
        return 0

    label = (
        str(item.get("judul", "")) + " " + str(item.get("url", ""))
    ).lower()

    # Berita yang hanya "memaparkan"/"mengukuhkan" Guru Besar bukan halaman
    # profil. Beri penalti agar tidak mengalahkan halaman profil dosen.
    if any(penanda in label for penanda in (
        "paparkan", "memaparkan", "mengukuhkan", "kukuhkan", "diukuhkan",
        "hantarkan", "raih gelar", "menggelar", "kunjungan", "seminar",
        "workshop", "orasi ilmiah", "wawancara", "dosen tamu",
        "profesor tamu", "visiting lecturer", "visiting professor",
        "dosen-dari-", "kaust", "king abdullah", "arab saudi",
    )):
        return -8

    if any(penanda in label for penanda in (
        "mengenal-prof", "profil-dosen", "profil_guru", "guru-besar-ke-",
        "prof-", "/dosen/", "dosen/",
    )):
        return 12

    if any(penanda in label for penanda in (
        "guru-besar", "guru_besar", "guru besar", "profesor",
        "fakultas-teknik", "/fakultas/", "program-studi",
    )):
        return 6

    return 0


def _bonus_lokasi(item):
    """Utamakan halaman kontak/maps/profil saat menanyakan lokasi kampus."""

    if not isinstance(item, dict):
        return 0

    judul = str(item.get("judul", ""))
    url = str(item.get("url", ""))
    label = (judul + " " + url).lower()
    path = urllib.parse.urlsplit(url).path.lower().rstrip("/")

    if path.endswith("/kontak") or path.endswith("/maps"):
        return 12
    if path.endswith("/profil") or "hubungi" in path:
        return 6
    if _adalah_artikel(item) or any(penanda in label for penanda in (
        "cbt", "jadwal", "pmb", "pendaftaran", "ujian",
    )):
        return -10

    return 0


def _normalisasi_query(query):
    """
    Mengubah pertanyaan menjadi kata kunci pencarian.

    Contoh:
    - Apa saja fakultas yang tersedia di UIR?
      -> daftar fakultas

    - Daftar fakultas Universitas Islam Riau
      -> daftar fakultas

    - Apa program studi yang ada di UIR?
      -> program studi

    - Informasi beasiswa UIR
      -> beasiswa
    """

    teks = _tanpa_site(query).lower()
    teks = re.sub(r"\s+", " ", teks).strip()

    # Pertanyaan dosen tidak boleh terjebak menjadi "daftar fakultas" hanya
    # karena menyebut "Fakultas Teknik" sebagai unit kerja.
    if perencana.pahami_dosen(teks)["aktif"]:
        variasi = perencana.strategi_pencarian(teks, batas=1)["subkueri"]
        if variasi:
            return _tanpa_site(variasi[0])

    info_lokasi = perencana.pahami_lokasi(teks)
    if info_lokasi["aktif"]:
        return info_lokasi["kanonik"]

    penanda_daftar = any(kata in teks for kata in (
        "daftar",
        "apa saja",
        "tersedia",
        "sebutkan",
        "berapa",
        "seluruh",
        "semua",
    ))

    # Pertanyaan tentang fakultas.
    if "fakultas" in teks:
        return "daftar fakultas" if penanda_daftar else "fakultas"

    # Pertanyaan tentang program studi.
    if any(kata in teks for kata in (
        "program studi",
        "program pendidikan",
        "prodi",
    )):
        return "daftar program studi" if penanda_daftar else "program studi"

    # Pertanyaan tentang beasiswa.
    if any(kata in teks for kata in (
        "beasiswa",
        "scholarship",
    )):
        return "beasiswa"

    # Pertanyaan tentang penerimaan mahasiswa.
    if any(kata in teks for kata in (
        "pendaftaran",
        "pmb",
        "penerimaan mahasiswa",
    )):
        return "penerimaan mahasiswa baru"

    # Pertanyaan tentang profil UIR.
    if any(kata in teks for kata in (
        "sejarah",
        "profil",
        "tentang uir",
        "apa itu",
    )):
        return "profil UIR"

    token = [
        kata
        for kata in _TOKEN_RELEVAN.findall(teks)
        if kata not in _KATA_UMUM
    ]

    if token:
        return " ".join(token[:6])

    return "profil UIR"


def _identifikasi_topik(teks):
    """Kembalikan topik pencarian yang disebut pada pertanyaan.

    Fungsi ini tidak menentukan jawaban. Ia hanya memilih kata kunci mana
    yang perlu dicari ketika satu pertanyaan memiliki beberapa aspek.
    """

    rendah = re.sub(r"\s+", " ", str(teks).lower())
    ditemukan = []

    for nama, penanda, _ in _PETA_TOPIK_PENCARIAN:
        if any(kata in rendah for kata in penanda):
            ditemukan.append(nama)

    return ditemukan


def _urai_subkueri(query):
    """Ubah satu pertanyaan menjadi beberapa kata kunci pencarian.

    Strategi ada di :mod:`app.perencana`. Pertanyaan polos seperti
    ``"beasiswa"`` sengaja tetap satu subkueri. Pertanyaan dengan penanda
    daftar/prosedur/identitas memakai variasi sesuai topik, tetapi batas
    ``MAX_SUBKUERI`` tetap menjaga jumlah permintaan ke situs UIR.
    """

    teks = re.sub(r"\s+", " ", str(query).lower()).strip()
    kanonik = _normalisasi_query(teks)

    return perencana.strategi_pencarian(
        teks,
        kanonik=kanonik,
        batas=MAX_SUBKUERI,
    )["subkueri"]


def _skor_multi(kata_kunci, subkueri, item):
    """Skor terbaik dari semua subkueri yang dipakai."""

    daftar = [kata_kunci] + [
        kata for kata in (subkueri or []) if kata != kata_kunci
    ]

    return max(
        _skor_kandidat(kata, item)
        for kata in daftar
    )


# ============================================================
# PEMERINGKATAN HASIL
# ============================================================

def _skor_relevansi(query, judul="", cuplikan="", url=""):
    """Skor kecocokan query terhadap judul, cuplikan, dan slug."""

    query = _tanpa_site(query)
    token_query = _token_topik(query)

    if not token_query:
        return 0

    token_judul = _token_topik(judul)
    token_cuplikan = _token_topik(cuplikan)

    token_url = _token_topik(
        urllib.parse.urlsplit(url).path.replace("/", " ")
    )

    skor = (
        len(token_query & token_judul) * 4
        + len(token_query & token_cuplikan) * 2
        + len(token_query & token_url)
    )

    if query.lower() in judul.lower():
        skor += 5

    return skor


def _adalah_inggris(url):
    """Periksa apakah URL merupakan halaman berbahasa Inggris."""

    path = urllib.parse.urlsplit(url).path

    return path == "/en" or path.startswith("/en/")


def _prioritaskan(kandidat, kata_kunci):
    """Urutkan kandidat berdasarkan relevansi dan bahasa."""

    berurutan = []

    for posisi, item in enumerate(kandidat):

        skor = _skor_kandidat(kata_kunci, item)

        berurutan.append((
            skor,
            int(_adalah_inggris(item.get("url", ""))),
            posisi,
            item,
        ))

    berurutan.sort(
        key=lambda data: (
            -data[0],
            data[1],
            data[2],
        )
    )

    return [
        item
        for _, _, _, item in berurutan
    ]


def _potong_teks(teks, batas):
    """Potong teks agar panjangnya tidak berlebihan."""

    if not teks or len(teks) <= batas:
        return teks

    potongan = teks[:batas]
    posisi = potongan.rfind(" ")

    if posisi > batas // 2:
        potongan = potongan[:posisi]

    return potongan.rstrip(" ,;:.-") + "…"


def _jeda():
    """Jeda antarpermintaan untuk menghindari akses terlalu cepat."""

    time.sleep(JEDA_DETAIL)


# ============================================================
# HTTP DAN ROBOTS.TXT
# ============================================================

def _ambil_robots():
    """Ambil dan simpan robots.txt dalam cache memori (aman untuk thread)."""

    global _ROBOTS_CACHE, _ROBOTS_SEDANG_DIAMBIL

    if _ROBOTS_CACHE is not None:
        return _ROBOTS_CACHE or None

    with _ROBOTS_LOCK:
        # Cek ulang setelah mengunci: thread lain mungkin baru saja selesai.
        if _ROBOTS_CACHE is not None:
            return _ROBOTS_CACHE or None

        _ROBOTS_SEDANG_DIAMBIL = True
        try:
            isi = _http_get(ROBOTS_URL).decode(
                "utf-8",
                "replace",
            )
        except _SitusTidakTersedia:
            _ROBOTS_CACHE = False
            return None
        finally:
            _ROBOTS_SEDANG_DIAMBIL = False

        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(ROBOTS_URL)
        parser.parse(isi.splitlines())

        _ROBOTS_CACHE = parser

        return parser


def _cek_robots(url):
    """Pastikan robots.txt mengizinkan akses ke URL."""

    alamat = _validasi_url(url)

    if alamat is None:
        raise _SitusTidakTersedia("URL tidak valid.")

    parser = _ambil_robots()

    if parser is None:
        raise _SitusTidakTersedia(
            "robots.txt tidak dapat diambil."
        )

    if not parser.can_fetch(USER_AGENT, alamat):
        raise _DiblokirRobots(
            "robots.txt melarang akses ke " + alamat
        )


class _RedirectAman(urllib.request.HTTPRedirectHandler):
    """Tolak redirect ke host, skema, atau port yang tidak diizinkan."""

    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        alamat = _validasi_url(newurl)

        if alamat is None:
            raise _SitusTidakTersedia(
                "Redirect ditolak karena tujuan bukan HTTPS uir.ac.id."
            )

        # Redirect robots.txt tidak boleh memicu pemeriksaan
        # robots secara rekursif.
        if req.full_url != ROBOTS_URL:
            _cek_robots(alamat)

        return super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            alamat,
        )


_OPENER = urllib.request.build_opener(
    _RedirectAman()
)


def _http_get(url):
    """Ambil data HTTP melalui opener dengan redirect aman."""

    alamat = _validasi_url(url)

    if alamat is None:
        raise _SitusTidakTersedia(
            "URL tidak valid: " + str(url)
        )

    permintaan = urllib.request.Request(
        alamat,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/json, application/xml, text/xml, "
                "text/html;q=0.9, */*;q=0.8"
            ),
        },
        method="GET",
    )

    try:
        with _OPENER.open(
            permintaan,
            timeout=REQUEST_TIMEOUT,
        ) as respons:

            kode = respons.getcode()

            if kode in (429, 503):
                raise _SitusTidakTersedia(
                    "Server UIR sedang sibuk (HTTP %s)." % kode
                )

            # Respons urllib normalnya memiliki geturl().
            # Jika objek respons pengujian tidak memilikinya,
            # gunakan URL awal yang sudah divalidasi.
            try:
                url_akhir = respons.geturl()
            except (AttributeError, TypeError):
                url_akhir = alamat

            if not isinstance(url_akhir, str):
                url_akhir = alamat

            akhir = _validasi_url(url_akhir)

            if akhir is None:
                raise _SitusTidakTersedia(
                    "URL akhir respons tidak diizinkan."
                )

            if akhir != alamat and alamat != ROBOTS_URL:
                _cek_robots(akhir)

            return respons.read()

    except (_DiblokirRobots, _SitusTidakTersedia):
        raise

    except urllib.error.HTTPError as exc:
        raise _SitusTidakTersedia(
            "HTTP %s saat mengambil %s."
            % (exc.code, alamat)
        ) from exc

    except (
        urllib.error.URLError,
        socket.timeout,
        TimeoutError,
        OSError,
    ) as exc:
        raise _SitusTidakTersedia(
            "Gagal menghubungi situs UIR: %s" % exc
        ) from exc


# ============================================================
# SITEMAP
# ============================================================

def _lokal(tag):
    """Ambil nama tag XML tanpa namespace."""

    return tag.rsplit("}", 1)[-1]


def _parse_indeks(xml_teks):
    """Ambil daftar sitemap anak dari sitemap index."""

    hasil = []

    for anak in ET.fromstring(xml_teks):

        if _lokal(anak.tag) != "sitemap":
            continue

        for turunan in anak:

            if (
                _lokal(turunan.tag) == "loc"
                and turunan.text
            ):
                alamat = _validasi_url(turunan.text)

                if alamat:
                    hasil.append(alamat)

    return hasil


def _judul_dari_slug(slug):
    """Ubah slug URL menjadi judul sementara."""

    if not slug:
        return "Beranda"

    teks = re.sub(
        r"[-_]+",
        " ",
        urllib.parse.unquote(slug),
    ).strip()

    teks = re.sub(r"\s+", " ", teks)

    return (
        teks[:1].upper() + teks[1:]
        if teks
        else "Beranda"
    )


def _parse_urlset(xml_teks):
    """Ambil URL dan tanggal perubahan dari sitemap."""

    entri = []

    for anak in ET.fromstring(xml_teks):

        if _lokal(anak.tag) != "url":
            continue

        lokasi = None
        lastmod = ""

        for turunan in anak:

            nama = _lokal(turunan.tag)

            if (
                nama == "loc"
                and turunan.text
                and lokasi is None
            ):
                lokasi = _validasi_url(turunan.text)

            elif nama == "lastmod" and turunan.text:
                lastmod = turunan.text.strip()

        if not lokasi:
            continue

        slug = (
            urllib.parse.urlsplit(lokasi)
            .path.strip("/")
            .split("/")[-1]
        )

        entri.append({
            "url": lokasi,
            "lastmod": lastmod,
            "slug": slug,
            "judul": _judul_dari_slug(slug),
        })

    return entri


def _nama_sitemap_diizinkan(url):
    """Batasi sitemap anak yang boleh dibaca."""

    nama = urllib.parse.urlsplit(url).path.rsplit(
        "/",
        1,
    )[-1]

    return any(
        nama.startswith(awalan)
        for awalan in SITEMAP_DIIZINKAN
    )


def _muat_sitemap():
    """Muat sitemap dan simpan hasilnya dalam cache memori."""

    global _SITEMAP_CACHE

    if _SITEMAP_CACHE is not None:
        return _SITEMAP_CACHE

    _cek_robots(SITEMAP_URL)

    try:
        indeks = _http_get(SITEMAP_URL).decode(
            "utf-8",
            "replace",
        )

        anak = [
            url
            for url in _parse_indeks(indeks)
            if _nama_sitemap_diizinkan(url)
        ]

        if not anak:
            entri = _parse_urlset(indeks)
            for item in entri:
                item["_sumber_sitemap"] = "langsung"

        else:
            entri = []

            for posisi, url_anak in enumerate(anak):

                if posisi:
                    _jeda()

                _cek_robots(url_anak)

                isi = _http_get(url_anak).decode(
                    "utf-8",
                    "replace",
                )

                nama_anak = urllib.parse.urlsplit(url_anak).path.rsplit("/", 1)[-1]
                for item in _parse_urlset(isi):
                    item["_sumber_sitemap"] = nama_anak
                    entri.append(item)

    except ET.ParseError as exc:
        raise _SitusTidakTersedia(
            "Sitemap UIR tidak dapat diurai."
        ) from exc

    _SITEMAP_CACHE = entri

    return entri


def _url_di_sitemap(url):
    """Periksa apakah URL tercantum di sitemap yang sudah dimuat."""

    if _SITEMAP_CACHE is None:
        return False

    return any(
        item["url"] == url
        for item in _SITEMAP_CACHE
    )


def _cari_di_sitemap(kata_kunci):
    """Cari kandidat halaman berdasarkan isi sitemap."""

    kata_kunci = _tanpa_site(kata_kunci)
    entri = _muat_sitemap()
    token = _token_topik(kata_kunci)

    if not token:
        return []

    katalog = _adalah_katalog(kata_kunci)
    cocok = []

    for item in entri:

        # Pada pertanyaan daftar, sitemap berita bukan katalog resmi.
        sumber = str(item.get("_sumber_sitemap", ""))
        if katalog and (
            sumber.startswith("post-sitemap")
            or _adalah_artikel(item)
        ):
            continue

        skor = _skor_kandidat(kata_kunci, item)

        if skor > 0:
            cocok.append((skor, item))

    cocok.sort(
        key=lambda pasangan: (
            -pasangan[0],
            int(_adalah_inggris(pasangan[1]["url"])),
            pasangan[1]["judul"].lower(),
        )
    )

    return [
        item
        for _, item in cocok[:MAX_KANDIDAT]
    ]


# ============================================================
# WORDPRESS REST API
# ============================================================

def _cari_via_rest(kata_kunci):
    """Cari halaman publik melalui WordPress REST API."""

    kata_kunci = _tanpa_site(kata_kunci)
    parameter = urllib.parse.urlencode({
        "search": kata_kunci,
        "per_page": MAX_PER_HALAMAN,
    })

    url = REST_SEARCH_URL + "?" + parameter

    _cek_robots(url)

    try:
        data = json.loads(
            _http_get(url).decode(
                "utf-8",
                "replace",
            )
        )

    except json.JSONDecodeError as exc:
        raise _SitusTidakTersedia(
            "Balasan REST UIR bukan JSON valid."
        ) from exc

    if not isinstance(data, list):
        raise _SitusTidakTersedia(
            "Bentuk balasan REST UIR tidak dikenal."
        )

    hasil = []

    for item in data:

        if not isinstance(item, dict):
            continue

        judul = _bersihkan_teks(
            item.get("title")
        )

        lokasi = _validasi_url(
            item.get("url")
        )

        if not judul or not lokasi:
            continue

        jenis = _bersihkan_teks(
            item.get("subtype")
            or item.get("type")
            or "halaman"
        )

        hasil.append({
            "judul": judul,
            "url": lokasi,
            "jenis": jenis or "halaman",
            "id": item.get("id"),
            "cuplikan": "",
            "_sumber_kandidat": "wp_rest",
        })

    return _prioritaskan(
        hasil,
        kata_kunci,
    )[:MAX_PER_HALAMAN]


def _ambil_detail(kandidat):
    """
    Ambil detail excerpt dari WordPress REST API.

    Excerpt dan meta description hanya digunakan sebagai
    cuplikan pencarian, bukan sebagai bukti isi halaman lengkap.
    """

    jenis = kandidat.get("jenis", "")
    identitas = str(kandidat.get("id", ""))

    if not re.fullmatch(
        r"[a-z0-9_-]{1,40}",
        jenis,
    ):
        return {}

    if not re.fullmatch(
        r"\d{1,12}",
        identitas,
    ):
        return {}

    koleksi = REST_KOLEKSI.get(
        jenis,
        jenis,
    )

    url = (
        BASE_URL
        + "/wp-json/wp/v2/"
        + koleksi
        + "/"
        + identitas
    )

    if _validasi_url(url) is None:
        return {}

    _cek_robots(url)

    try:
        data = json.loads(
            _http_get(url).decode(
                "utf-8",
                "replace",
            )
        )

    except json.JSONDecodeError:
        return {}

    if not isinstance(data, dict):
        return {}

    excerpt = data.get("excerpt")

    cuplikan_excerpt = (
        _bersihkan_teks(
            excerpt.get("rendered")
        )
        if isinstance(excerpt, dict)
        else ""
    )

    deskripsi = ""

    meta = data.get("aioseo_head_json")

    if isinstance(meta, dict):
        deskripsi = _bersihkan_teks(
            meta.get("description")
        )

    if cuplikan_excerpt:
        cuplikan = cuplikan_excerpt
        sumber = "excerpt"

    elif deskripsi:
        cuplikan = deskripsi
        sumber = "meta_description"

    else:
        cuplikan = kandidat.get("judul", "")
        sumber = "judul"

    return {
        "cuplikan": _potong_teks(
            cuplikan,
            PANJANG_CUPLIKAN,
        ),
        "sumber_cuplikan": sumber,
        "tanggal": _bersihkan_teks(
            data.get("modified")
            or data.get("date")
            or ""
        ),
    }


# ============================================================
# PENGGABUNGAN KANDIDAT
# ============================================================

def _gabungkan_kandidat(rest, sitemap, kata_kunci, subkueri=None):
    """
    Gabungkan kandidat REST API dan sitemap dari beberapa subkueri.

    URL yang sama hanya dimasukkan satu kali. Bila URL muncul pada beberapa
    subkueri atau sumber, provenannya digabungkan sehingga peringkat tetap
    memakai skor relevansi terbaik dan duplikat tidak menghilangkan informasi.
    """

    gabungan = {}
    daftar_sub = list(subkueri or [kata_kunci])

    def _tambah_provenan(salinan, item):
        """Satukan jejak subkueri tanpa mengganti data utama kandidat."""
        lama = salinan.get("_subkueri") or []
        baru = item.get("_subkueri") or []

        for kata in lama + baru:
            if kata and kata not in salinan.setdefault("_subkueri", []):
                salinan["_subkueri"].append(kata)

    # Masukkan hasil REST API.
    for item in rest:

        url = _validasi_url(
            item.get("url", "")
        )

        if not url:
            continue

        salinan = dict(item)
        salinan["url"] = url
        salinan["_sumber_kandidat"] = "wp_rest"

        gabungan[url] = salinan

    # Masukkan hasil sitemap.
    for item in sitemap:

        url = _validasi_url(
            item.get("url", "")
        )

        if not url:
            continue

        # URL sudah ditemukan melalui REST API atau subkueri lain.
        if url in gabungan:
            gabungan[url]["terverifikasi_sitemap"] = True
            _tambah_provenan(gabungan[url], item)
            continue

        salinan = {
            "judul": item.get("judul") or "",
            "url": url,
            "jenis": "sitemap",
            "id": None,
            "cuplikan": "",
            "lastmod": item.get("lastmod", ""),
            "_sumber_kandidat": "sitemap",
            "_subkueri": list(item.get("_subkueri") or []),
        }

        gabungan[url] = salinan

    hasil = list(gabungan.values())

    hasil.sort(
        key=lambda item: (
            -_skor_multi(kata_kunci, daftar_sub, item),
            int(_adalah_inggris(item.get("url", ""))),
            item.get("judul", "").lower(),
        )
    )

    return hasil[:MAX_KANDIDAT]


# ============================================================
# PENYUSUNAN HASIL
# ============================================================

def _hasil(
    status,
    ok,
    query,
    results,
    pesan,
    sumber,
    subkueri=None,
    per_subkueri=None,
):
    """Bentuk respons terstruktur untuk orchestrator."""

    return {
        "ok": ok,
        "status": status,
        "query": query,
        "count": len(results),
        "sumber": sumber,
        "results": results,
        "pesan": pesan,
        "subkueri": list(subkueri or [query]),
        "per_subkueri": list(per_subkueri or []),
        "waktu": datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds"),
    }


def _susun_hasil(kandidat, kata_kunci, sumber, subkueri=None):
    """Susun kandidat menjadi hasil pencarian yang konsisten."""

    hasil = []
    daftar_sub = [kata_kunci] + [
        kata for kata in (subkueri or []) if kata != kata_kunci
    ]

    def _skor_item(item):
        return _skor_multi(kata_kunci, daftar_sub, item)

    kandidat = sorted(
        kandidat,
        key=lambda item: (
            -_skor_item(item),
            int(_adalah_inggris(item.get("url", ""))),
        ),
    )

    jumlah_detail = 0

    for item in kandidat:

        detail = {}

        sumber_kandidat = item.get(
            "_sumber_kandidat",
            sumber,
        )

        # Ambil detail untuk kandidat dari REST API.
        if (
            sumber_kandidat == "wp_rest"
            and item.get("id") is not None
            and jumlah_detail < MAX_DETAIL
        ):

            if jumlah_detail:
                _jeda()

            jumlah_detail += 1

            try:
                detail = _ambil_detail(item)

            except _DiblokirRobots:
                LOGGER.warning(
                    "Detail diblokir robots.txt: %s",
                    item["url"],
                )

            except _SitusTidakTersedia as exc:
                LOGGER.warning(
                    "Detail gagal untuk %s: %s",
                    item["url"],
                    exc,
                )

        # Kandidat sitemap hanya memiliki judul dan lastmod.
        elif sumber_kandidat == "sitemap":

            detail = {
                "cuplikan": item.get("judul", ""),
                "sumber_cuplikan": "judul",
                "tanggal": item.get("lastmod", ""),
            }

        cuplikan = (
            detail.get("cuplikan")
            or item.get("cuplikan")
            or item.get("judul", "")
        )

        hasil.append({
            "judul": item.get("judul", ""),
            "url": item["url"],
            "cuplikan": cuplikan,
            "sumber_cuplikan": (
                detail.get("sumber_cuplikan")
                or "judul"
            ),
            "tanggal": (
                detail.get("tanggal")
                or item.get("lastmod", "")
            ),
            "jenis": item.get("jenis", "halaman"),
            "subkueri": list(item.get("_subkueri") or []),
            "terverifikasi_sitemap": (
                item.get("terverifikasi_sitemap", False)
                or _url_di_sitemap(item["url"])
            ),
        })

    hasil.sort(
        key=lambda item: (
            -_skor_item(item),
            int(_adalah_inggris(item["url"])),
        )
    )

    return hasil[:MAX_KANDIDAT]


# ============================================================
# PENCARIAN UTAMA
# ============================================================

def _cari_utama(
    kata_kunci,
    subkueri=None,
    sitemap_subkueri=None,
    butuh_sitemap=False,
):
    """
    Jalankan pencarian REST API dan sitemap untuk setiap subkueri.

    Pertanyaan sederhana tetap satu subkueri. Pertanyaan dengan beberapa
    aspek dicari dengan beberapa kata kunci, lalu kandidatnya digabung,
    dihilangkan duplikatnya, dan diperingkat ulang. Sitemap dipakai untuk
    permintaan katalog dan sebagai fallback bila REST gagal.
    """

    daftar_sub = list(subkueri or [kata_kunci])
    # Sitemap cukup sekali untuk kata kunci utama sebuah topik. REST tetap
    # memakai semua variasi agar halaman yang tidak ada di sitemap tidak luput.
    daftar_sitemap = [
        sub for sub in (sitemap_subkueri or daftar_sub)
        if sub in daftar_sub
    ] or list(daftar_sub)
    kandidat_rest = []
    kandidat_sitemap = []

    gagal_rest = None
    gagal_sitemap = None

    ringkasan = {}
    for sub in daftar_sub:
        ringkasan[sub] = {
            "query": sub,
            "wp_rest": 0,
            "sitemap": 0,
            "gagal_rest": False,
            "gagal_sitemap": False,
        }

    # --------------------------------------------------------
    # Tahap 1: WordPress REST API untuk setiap subkueri.
    # --------------------------------------------------------

    for posisi, sub in enumerate(daftar_sub):
        if posisi:
            _jeda()

        potongan = []

        try:
            potongan = _cari_via_rest(sub)

        except _DiblokirRobots as exc:
            gagal_rest = exc
            ringkasan[sub]["gagal_rest"] = True

        except _SitusTidakTersedia as exc:
            gagal_rest = exc
            ringkasan[sub]["gagal_rest"] = True

            LOGGER.warning(
                "Pencarian REST gagal untuk '%s': %s",
                sub,
                exc,
            )

        ringkasan[sub]["wp_rest"] = len(potongan)

        for item in potongan:
            if not isinstance(item, dict):
                continue

            salinan = dict(item)
            salinan["_subkueri"] = [sub]
            kandidat_rest.append(salinan)

    # --------------------------------------------------------
    # Tahap 2: tentukan kebutuhan pencarian sitemap.
    # --------------------------------------------------------

    # Sitemap dipakai untuk topik yang memiliki halaman indeks resmi
    # (katalog, fasilitas, pimpinan, profil), bukan hanya saat REST gagal.
    perlu_sitemap = (
        bool(butuh_sitemap)
        or any(_perlu_sitemap(sub) for sub in daftar_sub)
        or gagal_rest is not None
    )

    # --------------------------------------------------------
    # Tahap 3: sitemap digunakan satu kali per subkueri.
    # --------------------------------------------------------

    if perlu_sitemap:

        for posisi, sub in enumerate(daftar_sitemap):
            if posisi:
                _jeda()

            potongan = []

            try:
                potongan = _cari_di_sitemap(sub)

            except _DiblokirRobots as exc:
                gagal_sitemap = exc
                ringkasan[sub]["gagal_sitemap"] = True

                LOGGER.warning(
                    "Sitemap diblokir robots.txt untuk '%s': %s",
                    sub,
                    exc,
                )

            except _SitusTidakTersedia as exc:
                gagal_sitemap = exc
                ringkasan[sub]["gagal_sitemap"] = True

                LOGGER.warning(
                    "Pencarian sitemap gagal untuk '%s': %s",
                    sub,
                    exc,
                )

            ringkasan[sub]["sitemap"] = len(potongan)

            for item in potongan:
                if not isinstance(item, dict):
                    continue

                salinan = dict(item)
                salinan["_subkueri"] = [sub]
                kandidat_sitemap.append(salinan)

    per_subkueri = list(ringkasan.values())

    # --------------------------------------------------------
    # Tahap 4: gabungkan kandidat dari semua sumber dan subkueri.
    # --------------------------------------------------------

    kandidat = _gabungkan_kandidat(
        kandidat_rest,
        kandidat_sitemap,
        kata_kunci,
        daftar_sub,
    )

    # --------------------------------------------------------
    # Tahap 5: tangani jika tidak ada kandidat.
    # --------------------------------------------------------

    if not kandidat:

        if (
            isinstance(gagal_rest, _DiblokirRobots)
            or isinstance(gagal_sitemap, _DiblokirRobots)
        ):
            return _hasil(
                STATUS_DIBLOKIR_ROBOTS,
                False,
                kata_kunci,
                [],
                "robots.txt situs UIR melarang akses ke sumber pencarian.",
                "wp_rest+sitemap",
                daftar_sub,
                per_subkueri,
            )

        if gagal_rest and gagal_sitemap:
            return _hasil(
                STATUS_SITUS_TIDAK_TERSEDIA,
                False,
                kata_kunci,
                [],
                "Situs UIR tidak dapat dihubungi atau sumber pencarian tidak tersedia.",
                "wp_rest+sitemap",
                daftar_sub,
                per_subkueri,
            )

        if gagal_rest and perlu_sitemap and not kandidat_sitemap:
            return _hasil(
                STATUS_SITUS_TIDAK_TERSEDIA,
                False,
                kata_kunci,
                [],
                "Pencarian informasi di situs UIR tidak berhasil dilakukan.",
                "wp_rest+sitemap",
                daftar_sub,
                per_subkueri,
            )

        return _hasil(
            STATUS_TIDAK_DITEMUKAN,
            True,
            kata_kunci,
            [],
            (
                "Tidak ditemukan halaman publik di uir.ac.id "
                "untuk kata kunci '%s'."
            ) % kata_kunci,
            "wp_rest+sitemap",
            daftar_sub,
            per_subkueri,
        )

    # --------------------------------------------------------
    # Tahap 6: susun hasil akhir yang sudah deduplikasi.
    # --------------------------------------------------------

    hasil = _susun_hasil(
        kandidat,
        kata_kunci,
        "gabungan",
        daftar_sub,
    )

    if kandidat_rest and kandidat_sitemap:
        sumber = "wp_rest+sitemap"

    elif kandidat_rest:
        sumber = "wp_rest"

    else:
        sumber = "sitemap"

    return _hasil(
        STATUS_DITEMUKAN,
        True,
        kata_kunci,
        hasil,
        (
            "Ditemukan %d halaman publik di uir.ac.id "
            "untuk kata kunci '%s'."
        ) % (len(hasil), kata_kunci),
        sumber,
        daftar_sub,
        per_subkueri,
    )


# ============================================================
# API PUBLIK
# ============================================================

def cari_uir(query):
    """Cari halaman publik UIR berdasarkan pertanyaan atau kata kunci."""

    # Validasi tipe input.
    if not isinstance(query, str):

        return _hasil(
            STATUS_VALIDASI_GAGAL,
            False,
            "",
            [],
            "Kata kunci harus berupa teks.",
            "",
        )

    mentah = query.strip()

    # Validasi panjang dan karakter input.
    if (
        not mentah
        or len(mentah) > MAX_QUERY
        or any(
            ord(kar) < 32 or ord(kar) == 127
            for kar in mentah
        )
    ):

        return _hasil(
            STATUS_VALIDASI_GAGAL,
            False,
            mentah[:MAX_QUERY],
            [],
            (
                "Kata kunci kosong, terlalu panjang, "
                "atau mengandung karakter kontrol."
            ),
            "",
        )

    # Bersihkan, normalisasi, lalu uraikan menjadi beberapa subkueri.
    bersih = _bersihkan_teks(mentah)
    subkueri = _urai_subkueri(bersih)
    kata_kunci = subkueri[0]
    strategi = perencana.strategi_pencarian(
        bersih.lower(),
        kanonik=kata_kunci,
        batas=MAX_SUBKUERI,
    )

    if not kata_kunci:

        return _hasil(
            STATUS_VALIDASI_GAGAL,
            False,
            mentah[:MAX_QUERY],
            [],
            "Kata kunci tidak valid.",
            "",
            subkueri,
        )

    # Pakai ulang hasil yang masih segar untuk kumpulan subkueri yang sama.
    # Cache HANYA berisi URL/cuplikan hasil REST/sitemap, bukan bukti halaman.
    kunci_cache = tuple(subkueri)
    sekarang = time.monotonic()
    with _HASIL_CACHE_LOCK:
        tersimpan = _HASIL_CACHE.get(kunci_cache)

    if tersimpan is not None:
        waktu, hasil_cache = tersimpan
        if sekarang - waktu < HASIL_CACHE_TTL_DETIK:
            salinan = _salin_hasil(hasil_cache)
            salinan["dari_cache"] = True
            return salinan

    # Jalankan pencarian utama.
    try:
        hasil = _cari_utama(
            kata_kunci,
            subkueri,
            sitemap_subkueri=strategi.get("sitemap_subkueri"),
            butuh_sitemap=strategi.get("butuh_sitemap", False),
        )

        if hasil.get("ok") and hasil.get("status") in (
            STATUS_DITEMUKAN,
            STATUS_TIDAK_DITEMUKAN,
        ):
            with _HASIL_CACHE_LOCK:
                _HASIL_CACHE[kunci_cache] = (
                    sekarang,
                    _salin_hasil(hasil),
                )

        return hasil

    except _DiblokirRobots:

        return _hasil(
            STATUS_DIBLOKIR_ROBOTS,
            False,
            kata_kunci,
            [],
            "robots.txt melarang akses ke halaman yang diminta.",
            "",
            subkueri,
        )

    except Exception:

        LOGGER.exception(
            "Kesalahan tak terduga di cari_uir"
        )

        return _hasil(
            STATUS_SITUS_TIDAK_TERSEDIA,
            False,
            kata_kunci,
            [],
            (
                "Terjadi kesalahan saat mencari "
                "informasi di situs UIR."
            ),
            "",
            subkueri,
        )
