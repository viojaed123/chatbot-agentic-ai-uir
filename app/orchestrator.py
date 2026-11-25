"""Orkestrator agen dengan verifikasi bukti konservatif.

Provider model dipilih terpusat oleh :mod:`app.model_client`. DeepSeek API
adalah provider utama; Ollama/Hermes lokal tetap tersedia sebagai rollback.
"""
import json
import logging
import re
import urllib.parse

from app import perencana, telemetri
from app.katalog import AKREDITASI_RE, parse_katalog, peta_akreditasi
from app.ollama_client import (
    KonfigurasiTidakValid,
    ModelTidakTersedia,
    OllamaClient,
    OllamaError,
    OllamaTidakTersedia,
    OllamaTimeout,
    ResponsTidakValid,
)
from app.model_client import (
    KonfigurasiTidakValid as KonfigurasiTidakValidAPI,
    KunciTidakTersedia,
    ModelError,
    ModelTidakTersedia as ModelTidakTersediaAPI,
    ModelTimeout,
    PenyediaTidakTersedia,
    ResponsTidakValid as ResponsModelTidakValid,
    buat_client,
)
from app.system_prompt import SYSTEM_PROMPT
from tools.ambil_halaman import ambil_halaman
from tools.baca_cache import baca_cache, simpan_cache
from tools.cari_uir import MAX_QUERY, _validasi_url, cari_uir

LOGGER = logging.getLogger(__name__)

MAX_LANGKAH = 5
MAX_RIWAYAT = 40

# Konteks yang dikirim ke model sengaja dibatasi. Teks halaman LENGKAP tetap
# disimpan pada self.bukti untuk verifikasi klaim; yang dikirim ke model hanya
# cuplikan relevan. Ini mencegah prompt melebihi context window penyedia saat
# inference berjalan di CPU (prompt besar = waktu eval sangat lama).
MAX_EVIDEN_MODEL = 1600
MAX_HASIL_MODEL = 6
MAX_CUPLIKAN_MODEL = 180

_STATUS_BACA = {"ditemukan", "berhasil"}
_STATUS_CACHE_MISS = {"tidak_ditemukan", "kedaluwarsa"}

# Status sumber dipisahkan secara eksplisit. "retrieved" hanya berarti URL
# muncul pada hasil pencarian; "cited" hanya dipakai bila halaman benar-benar
# mendukung klaim yang ditampilkan pada jawaban akhir.
_STATUS_SUMBER = ("retrieved", "read", "evidence", "used", "cited")

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")

_STOP = set("""
apa apakah yang dan atau dari untuk dengan ini itu ada bagaimana berapa kapan siapa
dimana mana mengapa kenapa sebuah para saya tolong mohon bisa dapat tentang di ke pada
oleh sebagai adalah yaitu serta tersebut informasi resmi universitas islam riau
uir kampus halaman sebutkan jelaskan berikan mengenai terdapat tersedia terkait
saja seluruh semua daftar
""".split())

# Hanya bukti yang benar-benar berasal dari halaman yang dibaca yang boleh
# dijadikan katalog. Nilai ini diisi oleh _record(), bukan oleh model.
_SUMBER_BACA = ("baca_cache", "ambil_halaman")

# Penanda halaman berita/artikel. Dipakai supaya penyebutan "fakultas" dalam
# berita tidak membuatnya dianggap sebagai katalog resmi.
_BERITA_PATH = (
    "newsfakultas", "/berita", "berita-", "/post", "/tag/",
    "/category/", "/genunggul", "/penelitian", "/pengabdian",
)

_BERITA_JUDUL = (
    "berita", "pengumuman", "menggelar", "kunjungan", "launching",
    "resmikan", "seminar", "workshop", "wisuda", "milad",
    "apel akbar", "buka pendaftaran", "membuka pendaftaran",
    "pendaftaran dibuka", "hadirkan", "keynote", "juara", "prestasi",
    "kukuhkan", "mengukuhkan", "diukuhkan", "hantarkan", "raih gelar",
)

# Penanda struktural pada isi halaman katalog UIR yang sebenarnya.
# "Program Pendidikan" memuat "FAKULTAS DAN PASCASARJANA" dan
# "Tabel Akreditasi Program Studi" meski judulnya tidak memuat kata fakultas.
_KATALOG_TEKS = (
    "daftar fakultas", "fakultas dan pascasarjana",
    "daftar program studi", "tabel akreditasi program studi",
    "nama prodi", "program pendidikan",
)



TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "cari_uir",
            "description": "Cari halaman publik UIR untuk menemukan URL sumber.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "baca_cache",
            "description": "Baca halaman UIR dari cache lokal sebelum mengambil halaman baru.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ambil_halaman",
            "description": "Ambil isi halaman publik UIR jika cache kosong/kedaluwarsa.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"}
                },
                "required": ["url"],
            },
        },
    },
]

_ARG_KEYS = {
    "cari_uir": {"query"},
    "baca_cache": {"url"},
    "ambil_halaman": {"url"},
}


def _hasil(
    ok,
    status,
    jawaban="",
    sumber=None,
    terbaca=None,
    langkah=0,
    pesan="",
    status_sumber=None,
):
    return {
        "ok": bool(ok),
        "status": status,
        "jawaban": jawaban,
        "sumber": list(sumber or []),
        "sumber_terbaca": list(terbaca or []),
        "langkah": langkah,
        "pesan": pesan,
        "status_sumber": status_sumber or {},
    }


def _catat_pencarian(result):
    """Hitung bila hasil pencarian berasal dari cache (tanpa mencatat query)."""
    if isinstance(result, dict) and result.get("dari_cache"):
        telemetri.tambah("pencarian_cache_hit")


def _tokens(text):
    return {
        x for x in _TOKEN_RE.findall(str(text).lower())
        if x not in _STOP
    }


def _skor(pertanyaan, judul, teks):
    q = _tokens(pertanyaan)
    if not q:
        return 0

    title = _tokens(judul)
    body = _tokens(teks)

    return (2 * len(q & title) + len(q & body)) / (3 * len(q))


def _bersihkan_jawaban(answer):
    """Buang sisa markup tool_call yang kadang ikut kirim model."""

    if not isinstance(answer, str):
        return ""

    bersih = re.sub(
        r"<tool_call\b[^>]*>.*?</tool_call>",
        "",
        answer,
        flags=re.I | re.S,
    )
    bersih = re.sub(
        r"^\s*<tool_call\b[^>]*>.*$",
        "",
        bersih,
        flags=re.I | re.M,
    )

    return bersih.strip()


def _extract_data(result):
    if not isinstance(result, dict):
        return {}

    data = result.get("data")
    return data if isinstance(data, dict) else result


def _parse_call(call):
    if not isinstance(call, dict):
        return "", None

    function = call.get("function")
    if not isinstance(function, dict):
        return "", None

    name = function.get("name", "")
    args = function.get("arguments", {})

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return name, None

    return (
        name if isinstance(name, str) else "",
        args if isinstance(args, dict) else None,
    )


def _valid_args(name, args):
    return (
        name in _ARG_KEYS
        and isinstance(args, dict)
        and set(args) == _ARG_KEYS[name]
        and all(
            isinstance(value, str) and bool(value.strip())
            for value in args.values()
        )
    )


def _is_list_question(question):
    q = question.lower()
    return any(
        word in q
        for word in (
            "apa saja",
            "daftar",
            "sebutkan",
            "seluruh",
            "semua",
            "berapa saja",
        )
    )


def _topic(question):
    q = question.lower()

    # Pertanyaan dosen/Profesor/Guru Besar memakai jalur bukti tersendiri.
    # Kata "Fakultas Teknik" pada pertanyaan tersebut adalah unit kerja,
    # bukan permintaan daftar/katalog fakultas.
    if perencana.pahami_dosen(question)["aktif"]:
        return ""

    # "Sebutkan program studi Fakultas Teknik UIR" bertopic program studi,
    # bukan fakultas. Karena itu kata program studi diperiksa lebih dulu.
    if "program studi" in q or "prodi" in q:
        return "program studi"

    if "fakultas" in q:
        return "fakultas"

    return ""


# Topik untuk pencarian, relevansi, dan sitasi per bagian. Daftar ini hanya
# berisi kata kunci deteksi, BUKAN fakultas/prodi/jawaban yang di-hardcode.
_TOPIK_PENCARIAN = {
    "dosen": (
        "dosen", "profesor", "guru besar", "guru-besar",
    ),
    "fakultas": ("fakultas",),
    "program studi": ("program studi", "prodi", "jurusan"),
    "akreditasi": ("akreditasi", "akreditas"),
    "sejarah": (
        "sejarah", "berdiri", "didirikan", "profil", "tentang uir",
    ),
    "pimpinan": (
        "pimpinan", "rektor", "dekan", "struktur organisasi", "pemimpin",
    ),
    "fasilitas": (
        "fasilitas", "sarana", "prasarana", "laboratorium", "perpustakaan",
    ),
    "beasiswa": ("beasiswa", "scholarship", "bantuan pendidikan"),
    "pmb": (
        "penerimaan mahasiswa baru", "pmb", "pendaftaran mahasiswa",
        "mahasiswa baru", "jalur masuk", "calon mahasiswa",
    ),
    "akademik": (
        "akademik", "kalender akademik", "kurikulum", "semester", "ipk",
    ),
    "biaya": (
        "biaya kuliah", "biaya pendidikan", "biaya pendaftaran",
        "uang kuliah", "spp",
    ),
    "kemahasiswaan": (
        "kemahasiswaan", "organisasi mahasiswa", "ukm",
        "himpunan mahasiswa", "kegiatan mahasiswa",
    ),
}

_LABEL_TOPIK = {
    "lokasi": "lokasi/kontak UIR",
    "dosen": "dosen/Guru Besar",
    "fakultas": "fakultas",
    "program studi": "program studi",
    "akreditasi": "akreditasi",
    "sejarah": "sejarah/profil",
    "pimpinan": "pimpinan",
    "fasilitas": "fasilitas",
    "beasiswa": "beasiswa",
    "pmb": "penerimaan mahasiswa baru",
    "akademik": "akademik",
    "biaya": "biaya kuliah",
    "kemahasiswaan": "kemahasiswaan",
}

_TANDA_LANJUTAN = (
    "sebelumnya", "jawaban sebelumnya", "di atas", "tadi", "tersebut",
    "dari situ", "lanjutannya", "masih tentang", "informasi itu",
)

_TANDA_AMBIGU = (
    "salah satunya", "salah satu dari", "tanpa topik", "itu saja",
    "hal itu", "tersebut", "informasi itu",
)

_KATA_JAWABAN_UMUM = {
    "berdasarkan", "berikut", "halaman", "informasi", "resmi", "uir",
    "universitas", "islam", "riau", "tersebut", "sumber", "di atas",
    "anda", "lihat", "silakan", "mohon", "maaf", "jika", "namun",
    "termasuk", "selain", "antara", "berikutnya", "selanjutnya",
    "dimana", "yang", "dengan", "untuk", "pada", "dari", "ini", "itu",
    # Label umum pada halaman profil/fasilitas. Bukan nama entitas rekaan;
    # nilai faktual (nama orang dan angka) tetap diperiksa terpisah.
    "telepon", "telp", "email", "alamat", "kontak", "fax", "website",
    "fakultas", "program", "studi", "pendidikan", "prodi", "kampus",
    "laboratorium", "perpustakaan", "fasilitas", "sarana", "prasarana",
    "olahraga", "internet", "layanan", "gedung", "rektor", "dekan",
    "direktur", "mahasiswa", "civitas", "akademika", "penelitian",
    "pimpinan", "organisasi", "struktur", "jabatan", "nama",
    # Label prosedur/navigasi. Bukan nama orang atau entitas faktual; angka
    # dan nama orang tetap diverifikasi terpisah dengan aturan yang sama.
    "proses", "pmb", "gen", "unggul", "jalur", "khusus", "rapor",
    "based", "test", "computer", "gelombang", "akun", "username",
    "password", "syarat", "dokumen", "biaya", "pendaftaran", "seleksi",
    "baru", "khusus", "reguler", "beasiswa", "bantuan", "pendidikan",
    "program", "studi", "akreditasi", "tabel", "daftar", "katalog",
    "kampus", "unit", "informasi", "halaman", "sumber", "catatan",
}


def _topik_pertanyaan(question):
    """Kembalikan semua topik yang disebut pada pertanyaan."""

    rendah = re.sub(r"\s+", " ", str(question).lower())
    hasil = []

    for nama, penanda in _TOPIK_PENCARIAN.items():
        if any(kata in rendah for kata in penanda):
            hasil.append(nama)

    # Niat lokasi memakai helper yang menolak konteks CBT/PMB/acara, sehingga
    # pertanyaan "lokasi jadwal CBT" tidak ikut menjadi topik alamat kampus.
    if perencana.pahami_lokasi(question)["aktif"] and "lokasi" not in hasil:
        hasil.insert(0, "lokasi")

    return hasil


def _topik_teks(teks):
    """Deteksi topik dari judul/URL/isi halaman yang sudah terbaca."""

    rendah = re.sub(r"\s+", " ", str(teks or "").lower())
    hasil = set()

    for nama, penanda in _TOPIK_PENCARIAN.items():
        if any(kata in rendah for kata in penanda):
            hasil.add(nama)

    return hasil


def _topik_record(record):
    if not isinstance(record, dict):
        return set()

    teks = " ".join((
        str(record.get("judul", "")),
        str(record.get("url", "")),
        str(record.get("teks", ""))[:1200],
        " ".join(str(x) for x in (record.get("bagian") or [])[:3]),
    ))

    return _topik_teks(teks)


# ------------------------------------------------------------------
# Strategi khusus per topik: prioritas halaman, ekstraksi bukti, dan
# penyusunan jawaban terbatas. Fungsi-fungsi ini tidak memuat fakta UIR;
# nama/angka/langkah selalu diambil dari teks halaman yang terbaca.
# ------------------------------------------------------------------

def _path_url(url):
    bagian = urllib.parse.urlsplit(str(url or ""))
    return bagian.path.lower().rstrip("/")


def _prioritas_url_identitas(url):
    """Utamakan halaman pimpinan resmi, bukan laporan/rektor historis."""

    alamat = str(url or "").lower()
    if "elementor_library" in urllib.parse.urlsplit(alamat).query:
        return 4

    path = _path_url(alamat)

    if "/pimpinan" in path:
        return 0
    if "pengangkatan-rektor" in path:
        return 1
    if "/profil" in path:
        return 2
    if _adalah_artikel("", alamat):
        return 4

    return 3


def _prioritas_url_pmb(url):
    """Utamakan halaman proses penerimaan mahasiswa baru resmi."""

    alamat = str(url or "").lower()
    path = _path_url(alamat)

    awal = path.strip("/")
    if awal.startswith((
        "penerimaan-mahasiswa-baru",
        "pendaftaran-mahasiswa-baru",
        "mekanisme-pembelian-akun",
        "pmb",
        "jalur-masuk",
    )):
        return 0

    if any(penanda in path for penanda in (
        "penerimaan-mahasiswa", "pendaftaran-mahasiswa",
        "pembelian-akun", "/pmb", "jalur-masuk",
    )):
        return 1
    if "elementor_library" in urllib.parse.urlsplit(alamat).query:
        return 4
    if _adalah_artikel("", alamat):
        return 3

    # Berita dengan slug panjang yang hanya kebetulan menyebut pendaftaran
    # tidak boleh mengalahkan halaman proses resmi.
    if len(awal) > 70:
        return 2

    return 2


def _prioritas_url_beasiswa(url):
    """Utamakan halaman indeks beasiswa resmi, bukan berita penerima."""

    alamat = str(url or "").lower()
    path = _path_url(alamat)

    if path.endswith("/beasiswa") or "informasi-beasiswa" in path:
        return 0
    if "beasiswa" in path and not _adalah_artikel("", alamat):
        return 1
    if _adalah_artikel("", alamat):
        return 3

    return 2


def _prioritas_url_dosen(url):
    """Utamakan profil dosen/Guru Besar dan halaman unit resmi."""

    alamat = str(url or "").lower()
    bagian = urllib.parse.urlsplit(alamat)
    path = bagian.path.lower().rstrip("/")

    if "elementor_library" in bagian.query:
        return 5

    # Berita pengukuhan/orasi dan kunjungan dosen tamu sering memakai kata
    # "Guru Besar" atau awalan "/dosen-" pada slug. Keduanya bukan halaman
    # profil yang memuat daftar nama dan jabatan dosen UIR.
    if any(penanda in path for penanda in (
        "paparkan", "mengukuhkan", "pengukuhan", "orasi", "kunjungan",
        "seminar", "workshop", "menggelar", "wawancara", "dosen-dari-",
        "dosen-tamu", "profesor-tamu", "visiting", "kaust",
    )) or _adalah_artikel("", alamat):
        return 4

    if any(penanda in path for penanda in (
        "mengenal-prof", "profil-dosen", "profil_guru", "guru-besar-ke-",
        "prof-", "/dosen/",
    )):
        return 0
    if any(penanda in path for penanda in (
        "fakultas-teknik", "fakultas/", "/fakultas", "program-studi",
    )):
        return 1
    if any(penanda in path for penanda in (
        "guru-besar", "guru_besar", "profesor", "/profil",
    )):
        return 2

    return 3


def _prioritas_url_lokasi(url):
    """Utamakan halaman kontak/maps resmi, bukan berita jadwal/CBT."""

    alamat = str(url or "").lower()
    bagian = urllib.parse.urlsplit(alamat)
    path = bagian.path.lower().rstrip("/")

    if "elementor_library" in bagian.query:
        return 4

    if path.endswith("/kontak") or path.endswith("/maps"):
        return 0
    if path.endswith("/profil") or "hubungi" in path:
        return 1

    if any(penanda in path for penanda in (
        "cbt", "jadwal", "pmb", "pendaftaran", "ujian",
    )) or _adalah_artikel("", alamat):
        return 3

    return 2


def _prioritas_url_topik(url, daftar_topik):
    topik = set(daftar_topik or ())

    if "lokasi" in topik:
        return _prioritas_url_lokasi(url)
    if "dosen" in topik:
        return _prioritas_url_dosen(url)
    if "pimpinan" in topik:
        return _prioritas_url_identitas(url)
    if "pmb" in topik:
        return _prioritas_url_pmb(url)
    if "beasiswa" in topik:
        return _prioritas_url_beasiswa(url)

    return _prioritas_url(url)


def _url_kanonik_topik(url, daftar_topik):
    """Apakah URL ini halaman indeks resmi untuk topik yang diminta?"""

    return _prioritas_url_topik(url, daftar_topik) == 0


# ------------------------------------------------------------------
# Bukti dosen/Profesor/Guru Besar. Semua nama dan jabatan diambil dari teks
# halaman yang terbaca; daftar di bawah hanya berisi pola bahasa, bukan fakta.
# ------------------------------------------------------------------

_GELAR_PROFESOR_RE = re.compile(
    r"\b(?:guru\s*[_-]?besar|prof(?:esor)?\.?|professor)\b",
    re.I,
)
_GURU_BESAR_RE = re.compile(r"\b(?:guru\s*[_-]?besar)\b", re.I)
_LEKTOR_KEPALA_RE = re.compile(r"\blektor\s+kepala\b", re.I)
_NAMI_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'’.-]*")

_GELAR_DIABAIKAN = {
    "prof", "profesor", "professor", "guru", "besar", "dr", "drs",
    "ir", "ph", "phd", "ph.d", "eng", "doktor", "doktoranda", "lecturer",
    "lektor", "kepala", "m", "s", "st", "s.t", "mt", "m.t", "ipu",
    "msc", "ma", "mba", "mhum", "m.hum", "mph", "med", "rer", "pol",
    "mcl", "sh", "s.h", "m.si", "m.cl", "m.agr", "m.m",
    "m.e.sy", "cand", "candida", "asoc", "assoc", "associate",
    "associated",
}
_HORMATAN_DIPERTAHANKAN = {"h", "hj"}
_KATA_TAMBAHAN = {
    "fakultas", "program", "studi", "prodi", "jurusan", "universitas",
    "universias", "islam", "riau", "uir", "jabatan", "nama", "dosen",
    "status", "tanggal", "sejak", "pada", "merupakan", "adalah", "yaitu",
    "bidang", "paparkan", "pemanfaatan", "gas", "karbon", "dioksida",
    "industri", "provinsi", "propinsi", "selain", "berdasarkan",
    "perguruan", "tinggi", "merujuk", "dengan", "untuk", "dalam", "dari",
    "yang", "ini", "itu", "juga", "saat", "target", "pemerintah", "tahun",
    "minyak", "bumi", "barrel", "kebutuhan", "cekungan", "sumatera",
    "tengah", "menggunakan", "teknologi", "carbon", "capture",
    "utilization", "storage", "tri", "dharma", "pascasarjana", "kampus",
    "mahasiswa", "laboratorium", "perpustakaan", "rektor", "dekan",
    "pimpinan", "ilmu", "pengembangan", "khususnya", "capaian",
}
_NARASI_PROFESOR_RE = re.compile(
    r"\b(paparkan|memaparkan|mengukuhkan|kukuhkan|diukuhkan|berhasil|"
    r"menjadi|jadi|mengatakan|menyampaikan|menjelaskan|jelasnya|papar|"
    r"mendorong|mendapat|dimana|disimpan|mengenal|menghadiri|mengikuti|"
    r"menggelar|kunjungan|seminar|workshop|mengumumkan|membuka|menerima|"
    r"mengarahkan|datang|kembali|terima|berikan|hantarkan|raih|"
    r"menambahkan|tampil|webinar|pembicara|moderator|tanya\s+jawab|"
    r"menandatangani|mou|jalin|kerjasama|kerja\s+sama|bersama|"
    r"perwakilan|diundang|mengajar|pengajar|membawa|tour|"
    r"deputy\s+vice\s+chancellor|head\s+of\s+school)\b",
    re.I,
)
_LUAR_UIR_RE = re.compile(
    r"\b(?:UGM|Universitas\s+Gadjah\s+Mada|ITB|Institut\s+Teknologi\s+"
    r"Bandung|Universitas\s+Indonesia|UI|KAUST|King\s+Abdullah|Arab\s+Saudi|"
    r"APU|Asia\s+Pacific\s+University|Universitas\s+Udayana|Unuja|"
    r"Universitas\s+Nuru\s+Jadid|Universiti\s+Utara\s+Malaysia|UUM|"
    r"Universiti\s+Teknologi\s+Malaysia|UITM|Chiang\s+Mai)\b",
    re.I,
)
_PROFESOR_TAMU_RE = re.compile(
    r"\b(?:dosen\s+tamu|profesor\s+tamu|visiting\s+(?:professor|lecturer)|"
    r"king\s+abdullah|kaust|arab\s+saudi|dari\s+luar\s+negeri|"
    r"universitas\s+luar|mengundang\s+(?:dosen|profesor)|"
    r"kuliah\s+\d+\s+hari|tampil\s+(?:di|sebagai)|webinar|pembicara|"
    r"menandatangani\s+mou|jalin\s+kerjasama|jalin\s+kerja\s+sama|"
    r"deputy\s+vice\s+chancellor|asoc\.?\s+prof|assoc\.?\s+prof|"
    r"associated\s+prof)\b",
    re.I,
)


def _adalah_profesor_tamu(record):
    """Apakah halaman itu berbicara profesor/dosen tamu luar UIR?"""

    if not isinstance(record, dict):
        return False

    teks = " ".join((
        str(record.get("judul", "")),
        str(record.get("url", "")),
        str(record.get("teks", ""))[:1200],
    ))

    return bool(_PROFESOR_TAMU_RE.search(teks))


def _potongan_nama_profesor(potongan):
    """Ambil calon nama dari potongan teks yang menyebut jabatan profesor."""

    nilai = re.sub(r"\s+", " ", str(potongan or ""))
    nilai = re.split(
        r"(?:\bFakultas\b|\bProgram\s+Studi\b|\bJabatan\b|\bPendidikan\b|"
        r"\bBeliau\b|\bTelah\b|\bMerupakan\b|\bAdalah\b|\bStatus\b|"
        r"\bTanggal\b|"
        r"\b(?:menceritakan|menggambarkan|mengungkapkan|ungkapnya|meraih|"
        r"mendapatkan|pun|juga|kerap|menyampaikan|menjelaskan|"
        r"diceritakan|ditambahkan|tambahnya|jelasnya|menyebut|"
        r"menuturkan|mengaku|mengajak|mengundang|memberikan|"
        r"memberi|membantu|mengumpulkan|bekerja|melanjutkan|"
        r"menamatkan|menjadi|lahir|menggambar|memilih|membawa|"
        r"berkomitmen|mengindahkan|mengenal|menghadiri|mengikuti|"
        r"membahas|mencapai|mendapat|selama|jadi|raih|hantarkan)\b|"
        r"[,;|]|\s{2,})",
        nilai,
        flags=re.I,
    )[0].strip(" \t.,;:-|()[]")

    if _NARASI_PROFESOR_RE.search(nilai):
        return ""

    suku = []
    for token_cocok in _NAMI_TOKEN_RE.finditer(nilai):
        token = token_cocok.group(0)
        rendah = token.lower().strip(".")

        if rendah in _GELAR_DIABAIKAN or rendah in _KATA_TAMBAHAN:
            continue
        if rendah in _HORMATAN_DIPERTAHANKAN and not suku:
            suku.append(token.rstrip(".") + ".")
            continue
        if not re.match(r"^[A-Z]", token):
            continue

        suku.append(token.rstrip(".,"))
        if len(suku) >= 6:
            break

    nama = " ".join(suku).strip(" ,;:.-")
    if len(nama) < 4 or len(nama) > 120:
        return ""
    if not re.match(r"^[A-Z]", nama):
        return ""

    return nama


def _jabatan_profesor(baris):
    """Jabatan akademik hanya Profesor/Guru Besar, bukan Doktor/Lektor."""

    teks = str(baris or "")
    if _GURU_BESAR_RE.search(teks):
        return "Guru Besar"
    if _GELAR_PROFESOR_RE.search(teks):
        return "Profesor"

    return ""


def _nama_dari_baris_jabatan(baris):
    """Ambil nama dari satu baris yang memuat atau berdampingan dengan jabatan.

    ``Prof.`` didahulukan karena pada halaman berita sering muncul lebih awal
    dalam kalimat daripada label "Guru Besar" yang mengikuti nama.
    """

    teks = str(baris or "")
    if re.search(r"\bprof(?:esor)?\.?\b", teks, re.I):
        pola = r"\b(?:Prof(?:esor)?\.?|Professor)\b"
    else:
        pola = r"\b(?:Guru\s*[_-]?Besar)\b"

    potongan = re.split(pola, teks, maxsplit=1, flags=re.I)
    calon = potongan[1] if len(potongan) > 1 else baris

    return (
        _potongan_nama_profesor(calon)
        or _potongan_nama_profesor(potongan[0])
    )


def _adalah_jabatan_murni(baris):
    """Apakah baris hanya berisi label jabatan tanpa nama/unit lain?"""

    sisa = re.sub(
        r"\b(?:Guru\s*[_-]?Besar|Prof(?:esor)?\.?|Professor)\b",
        " ",
        str(baris or ""),
        flags=re.I,
    )
    return not re.search(r"[A-Za-z]", sisa)


def _unit_profesor(teks):
    """Unit kerja dari teks halaman, tanpa menebak bila tidak dinyatakan."""

    for baris in _baris_bersih(teks):
        cocok = re.search(
            r"\bFakultas\s+([A-Z][A-Za-z&'’\-]+(?:\s+[A-Z][A-Za-z&'’\-]+){0,3})",
            baris,
        )
        if cocok:
            nilai = re.split(r"\s+Universitas\b", cocok.group(1))[0]
            return "Fakultas " + nilai.strip(" ,;:.-")

        cocok = re.search(
            r"\bProgram\s+Studi\s+([A-Z][A-Za-z0-9&'’\-]+"
            r"(?:\s+[A-Z][A-Za-z0-9&'’\-]+){0,3})",
            baris,
        )
        if cocok:
            nilai = re.split(r"\s+Universitas\b", cocok.group(1))[0]
            return "Program Studi " + nilai.strip(" ,;:.-")

    return "Unit kerja tidak dinyatakan pada halaman"


def _waktu_profesor(record, teks):
    """Ambil keterangan waktu bila memang tertulis pada halaman."""

    for kunci in ("waktu", "tanggal", "waktu_cache"):
        nilai = str((record or {}).get(kunci, "") or "").strip()
        if re.search(r"\d", nilai) and len(nilai) <= 80:
            return nilai

    cocok = re.search(
        r"\b(?:\d{1,2}\s+"
        r"(?:Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|"
        r"September|Oktober|November|Desember)\s+(?:19|20)\d{2}"
        r"|(?:Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|"
        r"September|Oktober|November|Desember)\s+(?:19|20)\d{2}"
        r"|(?:19|20)\d{2})\b",
        str(teks or ""),
        re.I,
    )

    return cocok.group(0) if cocok else ""


def _profesor_evidence(records, question):
    """Ekstrak bukti nama+jabatan profesor dari halaman yang terbaca."""

    info = perencana.pahami_dosen(question)
    if not info["aktif"]:
        return []

    hasil = []
    terlihat = {}

    for record in records or []:
        if not isinstance(record, dict):
            continue
        if record.get("sumber") not in _SUMBER_BACA:
            continue
        if _adalah_profesor_tamu(record):
            continue

        teks = str(record.get("teks", "") or "")
        if not teks.strip():
            continue

        unit = _unit_profesor(teks)
        waktu = _waktu_profesor(record, teks)
        url = str(record.get("url", "") or "")

        baris_semua = _baris_bersih(teks)

        for indeks, baris in enumerate(baris_semua):
            berikut = baris_semua[indeks + 1:indeks + 3]

            # Nama harus berdampingan dengan gelar Profesor. Label "Guru Besar"
            # saja terlalu sering menjadi judul berita, bukan nama orang.
            if not re.search(r"\bprof(?:esor)?\.?\b", baris, re.I):
                continue

            # Judul berita pendek ("UIR Kukuhkan Prof ...", "Guru Besar Baru
            # di UIR, Prof ... Raih Gelar ...") sering menyerupai baris profil.
            # Nama dari judul tersebut tidak dipakai sebagai bukti jabatan.
            if len(baris) < 180 and _NARASI_PROFESOR_RE.search(baris):
                continue

            # Narasi yang menyebut profesor sebagai narasumber, rektor, atau
            # tamu bukan bukti jabatan orang itu. Kecuali baris yang memuat
            # "Guru Besar" dan unit teknik secara eksplisit.
            bagian = re.split(
                r"\bprof(?:esor)?\.?\b", baris, maxsplit=1, flags=re.I
            )
            naskah = " ".join(bagian)
            kuat = bool(
                _GURU_BESAR_RE.search(baris)
                and "teknik" in baris.lower()
            )
            if _NARASI_PROFESOR_RE.search(naskah) and not kuat:
                continue

            jabatan = _jabatan_profesor(baris)
            if not jabatan:
                continue

            nama = _nama_dari_baris_jabatan(baris)
            if not nama:
                continue

            # "Prof. Nama" yang diikuti "Guru Besar ..." adalah Guru Besar.
            # Label pada baris sebelumnya sengaja tidak dipakai agar jabatan
            # entri sebelumnya tidak tertular ke entri berikutnya.
            if jabatan == "Profesor" and any(
                _GURU_BESAR_RE.search(item) for item in berikut
            ):
                jabatan = "Guru Besar"

            konteks = " ".join([baris] + berikut).lower()
            if (
                _LEKTOR_KEPALA_RE.search(konteks)
                and not _GURU_BESAR_RE.search(konteks)
                and not re.search(r"\bprof(?:esor)?\.?\b", konteks, re.I)
            ):
                continue
            if _LUAR_UIR_RE.search(konteks):
                continue

            if info["guru_besar"] and jabatan != "Guru Besar":
                continue
            if info["teknik"] and (
                "teknik" not in baris.lower()
                and "teknik" not in unit.lower()
            ):
                continue

            # Satu orang cukup satu kali per halaman. Bila jabatan Guru Besar
            # ditemukan pada baris lain, gunakan jabatan yang lebih spesifik.
            kunci = (nama.lower(), url)
            if kunci in terlihat:
                if jabatan == "Guru Besar":
                    hasil[terlihat[kunci]]["jabatan"] = "Guru Besar"
                continue

            terlihat[kunci] = len(hasil)
            hasil.append({
                "nama": nama,
                "jabatan": jabatan,
                "unit": unit,
                "waktu": waktu,
                "url": url,
                "judul": str(record.get("judul", "") or ""),
                "sumber": record.get("sumber"),
                "baris": baris[:300],
            })

    return hasil[:12]


def _adalah_laporan_rektor(record):
    """Laporan tahunan rektor bukan bukti jabatan rektor terkini."""

    url = str(record.get("url", "")).lower()
    judul = str(record.get("judul", "")).lower()
    teks = str(record.get("teks", "")).lower()

    return (
        "laporan-rektor" in url
        or "laporan rektor" in judul
        or bool(re.search(r"laporan\s+(?:tahun\s+)?(?:19|20)\d{2}", teks))
    )


def _baris_bersih(teks):
    return [
        re.sub(r"\s+", " ", baris).strip(" \t•-")
        for baris in re.split(r"\n+", str(teks or ""))
        if re.sub(r"\s+", " ", baris).strip(" \t•-")
    ]


def _bersihkan_nama(calon):
    nilai = re.sub(r"\s+", " ", str(calon or "")).strip(" ,;:.-")
    nilai = re.split(
        r"\s+(?:Merupakan|Gelar|Pendidikan|Beliau|Untuk|Telah)\b",
        nilai,
        flags=re.I,
    )[0].strip(" ,;:.-")

    if not nilai or nilai.lower().startswith("wakil"):
        return ""
    if len(nilai) < 8 or not re.search(r"[A-Z]", nilai):
        return ""

    return nilai


def _nama_peran(teks, peran):
    """Ambil nama dari baris peran pada halaman pimpinan yang terbaca."""

    baris = _baris_bersih(teks)
    target = {
        "rektor": {"rektor", "rektor uir", "rektor universitas islam riau"},
        "wakil rektor": {
            "wakil rektor", "wakil rektor uir",
            "wakil rektor universitas islam riau",
        },
        "dekan": {"dekan", "dekan uir"},
    }.get(peran, set())

    for indeks, nilai in enumerate(baris):
        if nilai.lower() not in target:
            continue

        for berikut in baris[indeks + 1:indeks + 3]:
            nama = _bersihkan_nama(berikut)
            if nama:
                return nama

    return ""


def _pasangan_peran_nama(teks):
    """Daftar (peran, nama) dari halaman pimpinan yang terbaca."""

    baris = _baris_bersih(teks)
    hasil = []

    for indeks, nilai in enumerate(baris):
        rendah = nilai.lower()
        peran = ""

        if rendah in {"rektor", "rektor uir", "rektor universitas islam riau"}:
            peran = "Rektor"
        elif rendah.startswith("wakil rektor") and len(nilai) < 120:
            peran = nilai
        elif rendah in {"dekan", "dekan uir"}:
            peran = "Dekan"

        if not peran:
            continue

        for berikut in baris[indeks + 1:indeks + 3]:
            nama = _bersihkan_nama(berikut)
            if nama:
                hasil.append((peran, nama))
                break

        if len(hasil) >= 8:
            break

    return hasil


def _record_identitas(records):
    """Halaman pimpinan resmi yang boleh dipakai sebagai bukti jabatan."""

    sah = [
        record for record in records
        if isinstance(record, dict)
        and record.get("sumber") in _SUMBER_BACA
        and not _adalah_laporan_rektor(record)
    ]

    sah.sort(key=lambda record: (
        _prioritas_url_identitas(record.get("url", "")),
        -len(str(record.get("teks", ""))),
    ))

    return sah


def _identitas_answer(records, question):
    """Susun jawaban identitas dari halaman pimpinan resmi yang terbaca."""

    rendah = str(question).lower()
    if not any(tanda in rendah for tanda in (
        "siapa", "rektor", "pimpinan", "dekan", "jabatan",
    )):
        return None

    sah = _record_identitas(records)
    if not sah:
        return None

    if "wakil rektor" in rendah:
        peran = "wakil rektor"
    elif "dekan" in rendah:
        peran = "dekan"
    elif "rektor" in rendah:
        peran = "rektor"
    else:
        peran = "pimpinan"

    dipakai = []

    if peran == "pimpinan":
        butir = []
        for record in sah:
            pasangan = _pasangan_peran_nama(record.get("teks", ""))
            if not pasangan:
                continue
            dipakai.append(record)
            for jabatan, nama in pasangan[:6]:
                butir.append("- %s: %s" % (jabatan, nama))
            break

        if not butir:
            return None

        isi = (
            "Berdasarkan halaman pimpinan resmi UIR yang berhasil dibaca, "
            "jabatan pimpinan UIR meliputi:\n\n" + "\n".join(butir)
        )
    else:
        for record in sah:
            nama = _nama_peran(record.get("teks", ""), peran)
            if not nama:
                continue
            dipakai.append(record)
            isi = (
                "Berdasarkan halaman pimpinan resmi UIR yang berhasil dibaca, "
                "%s Universitas Islam Riau adalah %s." % (
                    "Wakil Rektor" if peran == "wakil rektor" else peran.title(),
                    nama,
                )
            )
            break

        if not dipakai:
            return None

    sumber = []
    for record in dipakai:
        record["digunakan_untuk"] = ["identitas pimpinan"]
        url = str(record.get("url", ""))
        if url and url not in sumber:
            sumber.append(url)

    return (
        isi + "\n\nCatatan: halaman laporan tahunan rektor tidak dipakai "
        "sebagai bukti jabatan terkini.\n\nSumber:\n"
        + "\n".join("- " + url for url in sumber)
    )


def _nama_dari_identitas_jawaban(answer):
    cocok = re.search(
        r"(?i)(?:rektor(?:\s+uir|\s+universitas\s+islam\s+riau)?\s+"
        r"(?:adalah|merupakan|yaitu)\s+)([A-Z][^\n;]{4,160})",
        str(answer or ""),
    )
    if not cocok:
        return ""

    return _bersihkan_nama(cocok.group(1))


def _klaim_identitas_didukung(answer, records, question):
    """Verifikasi klaim 'Rektor UIR adalah X' per halaman yang menyatakannya."""

    rendah = str(question).lower()
    if not any(tanda in rendah for tanda in (
        "siapa", "rektor", "pimpinan", "dekan", "jabatan",
    )):
        return None

    nama = _nama_dari_identitas_jawaban(answer)
    if not nama:
        return None

    for record in _record_identitas(records):
        if _prioritas_url_identitas(record.get("url", "")) > 2:
            continue

        baris = _baris_bersih(record.get("teks", ""))

        for posisi, nilai in enumerate(baris):
            if nama.lower() not in nilai.lower():
                continue

            # Peran dilihat dari baris nama dan dua baris sebelumnya.
            # "Wakil Rektor" pada baris sesudahnya tidak boleh membatalkan
            # nama Rektor yang telah dinyatakan pada baris tersendiri.
            sekitar = baris[max(0, posisi - 2):posisi + 1]
            konteks = " ".join(sekitar).lower()

            if (
                "rektor" in konteks
                and "wakil rektor" not in nilai.lower()
                and not any(
                    item.lower().startswith("wakil rektor")
                    for item in sekitar[:-1]
                )
            ):
                return True, ""

    return False, (
        "Nama '%s' tidak ditemukan pada halaman pimpinan resmi yang "
        "menyatakan jabatan rektor." % nama
    )


def _query_profesor_kanonik(question):
    """Satu variasi kata kunci kanonik untuk perbaikan pencarian profesor."""

    strategi = perencana.strategi_pencarian(
        str(question or "").lower(),
        batas=10,
    )
    daftar = list(strategi.get("subkueri") or ())

    return daftar[0] if daftar else "site:uir.ac.id profesor Universitas Islam Riau"


def _profesor_answer(records, question):
    """Susun jawaban profesor/Guru Besar hanya dari bukti halaman terbaca."""

    info = perencana.pahami_dosen(question)
    if not info["aktif"]:
        return None

    bukti = _profesor_evidence(records, question)

    # Pertanyaan Guru Besar tidak boleh dijawab dengan daftar Profesor biasa.
    if info["kategori"] == "guru_besar":
        bukti = [item for item in bukti if item["jabatan"] == "Guru Besar"]

    if not bukti:
        return None

    dipakai = []
    untuk = set()
    for item in bukti:
        kunci = (item["nama"].lower(), item["jabatan"].lower())
        if kunci in untuk:
            continue
        untuk.add(kunci)
        dipakai.append(item)

    for record in records or []:
        if not isinstance(record, dict):
            continue
        if any(str(record.get("url", "")) == item["url"] for item in dipakai):
            label = list(record.get("digunakan_untuk") or [])
            if "profesor dan guru besar" not in label:
                label.append("profesor dan guru besar")
            record["digunakan_untuk"] = label

    if info["unit"] == "Fakultas Teknik":
        judul = "**Profesor dan Guru Besar Fakultas Teknik UIR**"
    elif info["kategori"] == "guru_besar":
        judul = "**Guru Besar Universitas Islam Riau**"
    elif info["teknik"]:
        judul = "**Profesor dan Guru Besar Fakultas Teknik UIR**"
    else:
        judul = "**Profesor dan Guru Besar Universitas Islam Riau**"

    bagian = [
        judul,
        "",
        "Berdasarkan halaman resmi Universitas Islam Riau yang berhasil "
        "dibaca, berikut nama-nama yang dapat diverifikasi:",
        "",
    ]

    for indeks, item in enumerate(dipakai, 1):
        bagian.append("%d. %s" % (indeks, item["nama"]))
        bagian.append("   - Jabatan akademik: %s" % item["jabatan"])
        bagian.append("   - Fakultas/unit: %s" % item["unit"])
        if item["waktu"]:
            bagian.append("   - Keterangan waktu: %s" % item["waktu"])
        bagian.append("   - Sumber: %s" % item["url"])

    if "jumlah" in info["atribut"]:
        bagian.extend((
            "",
            "Ditemukan %d nama dengan jabatan Profesor/Guru Besar yang "
            "dapat diverifikasi pada halaman yang berhasil dibaca. Angka "
            "ini bukan jumlah resmi seluruh profesor/Guru Besar UIR."
            % len(dipakai),
        ))

    bagian.extend((
        "",
        "Catatan:",
        "Informasi yang belum dapat dibuktikan tidak dimasukkan. Daftar "
        "ini hanya memuat nama yang ditemukan pada halaman resmi UIR yang "
        "berhasil dibaca dan tidak dinyatakan lengkap.",
    ))

    return "\n".join(bagian)


def _klaim_profesor_didukung(answer, records, question):
    """Validasi setiap nama/jabatan profesor terhadap bukti halaman."""

    info = perencana.pahami_dosen(question)
    if not info["aktif"]:
        return None

    bukti = _profesor_evidence(records, question)
    if info["kategori"] == "guru_besar":
        bukti = [item for item in bukti if item["jabatan"] == "Guru Besar"]

    peta = {item["nama"].lower(): item for item in bukti}
    if not peta:
        return None

    # Hanya baris bernomor yang berisi nama. Baris "- Jabatan akademik: ..."
    # dan "- Sumber: ..." adalah detail, bukan nama yang perlu diverifikasi.
    nama_answer = []
    for baris in str(answer or "").splitlines():
        cocok = re.match(r"^\s*\d+[.)]\s*(.+)$", baris)
        if not cocok:
            continue
        nilai = cocok.group(1).strip(" .;")
        if nilai:
            nama_answer.append(nilai)

    if not nama_answer:
        return None

    for nama in nama_answer:
        if re.match(r"(?:https?://|www\.)", nama, re.I):
            continue
        kunci = re.sub(r"\s+", " ", nama).strip().lower()
        if kunci not in peta:
            return False, (
                "Nama '%s' tidak ditemukan pada halaman resmi UIR yang "
                "menyebut jabatan Profesor/Guru Besar." % nama
            )

    for baris in str(answer or "").splitlines():
        cocok = re.search(
            r"Jabatan\s+akademik\s*:\s*(.+)$",
            baris,
            re.I,
        )
        if cocok and not _jabatan_profesor(cocok.group(1)):
            return False, (
                "Jabatan akademik pada jawaban tidak dibuktikan sebagai "
                "Profesor atau Guru Besar."
            )

    url_bukti = {item["url"] for item in bukti}
    for url in re.findall(r"https://uir\.ac\.id/[^\s)]+", str(answer or "")):
        if url.rstrip(".,;") not in url_bukti:
            return False, (
                "Tautan '%s' tidak mendukung nama/jabatan yang ditampilkan."
                % url
            )

    return True, ""


def _ekstrak_nama_beasiswa(teks):
    """Ambil nama program beasiswa dari teks halaman, bukan dari daftar baku."""

    hasil = []
    henti = {
        "dibuka", "dibukakan", "untuk", "bagi", "mahasiswa", "uir",
        "tersedia", "pendaftaran", "dapat", "mendaftar", "berkesempatan",
        "mengajukan", "program", "periode", "tahun", "aktif", "baru",
        "resmi", "informasi", "peluang", "kesempatan", "ditutup",
        "silahkan", "silakan", "hubungi", "kunjungi", "selengkapnya",
        "seperti", "yang", "sudah", "dibaca", "tersebut", "lihat",
        "detail", "menyediakan", "dapat", "dipakai", "bahan",
        "pertimbangan",
    }

    # Setiap baris diproses terpisah supaya pola tidak menyeberang dari
    # "Informasi Beasiswa UIR" ke "Beasiswa KIP" pada baris berikutnya.
    for baris in _baris_bersih(teks):
        rendah = baris.lower()

        # "Penerima Beasiswa ..." adalah berita penerima, bukan nama program.
        if "penerima beasiswa" in rendah:
            continue

        for potongan in re.split(r"(?i)\bBeasiswa\s+", baris)[1:]:
            nilai = re.sub(r"\s+", " ", potongan).strip(" ,;:.-")
            kata = nilai.split()

            while kata and kata[-1].lower().strip(".,") in henti:
                kata.pop()

            nama = " ".join(kata).strip(" ,;:.-")[:140]

            # Baris "Beasiswa UIR" adalah nama kategori resmi. Kata "UIR"
            # sengaja dipertahankan hanya bila memang seluruh potongan.
            if not nama and nilai.lower() == "uir":
                nama = "UIR"

            if not nama or len(nama) < 3 or len(nama) > 60:
                continue
            if not re.match(r"[A-Z0-9]", nama):
                continue

            # "Cendekia Baznas" dan "Cendekia BAZNAS 2026" adalah program
            # yang sama bila tahun hanya merupakan keterangan periode.
            kunci = re.sub(
                r"\b(?:19|20)\d{2}\b", "", nama
            ).strip().lower()
            if kunci not in [
                re.sub(r"\b(?:19|20)\d{2}\b", "", item).strip().lower()
                for item in hasil
            ]:
                hasil.append(nama)

    return hasil[:12]


def _adalah_berita_penerima(record):
    judul = str(record.get("judul", ""))
    url = str(record.get("url", ""))
    teks = str(record.get("teks", "")).lower()

    if not _adalah_artikel(judul, url):
        return False

    return any(kata in teks for kata in (
        "raih beasiswa", "meraih beasiswa", "penerima beasiswa",
        "mendapat beasiswa", "berprestasi hingga",
    ))


def _kategori_beasiswa(record):
    teks = str(record.get("teks", "")).lower()

    if _adalah_berita_penerima(record):
        return {"berita_penerima"}

    kategori = set()

    if any(kata in teks for kata in (
        "ditutup", "tertutup", "berakhir", "tidak lagi", "sudah tidak",
        "kedaluwarsa",
    )):
        kategori.add("ditutup")

    if any(kata in teks for kata in (
        "dibuka", "pendaftaran dibuka", "tersedia", "aktif", "mendaftar",
        "mengajukan", "berkesempatan mendaftar", "proses seleksinya",
        "dapat kamu ikuti", "kesempatan melalui", "program & jenis beasiswa",
        "program dan jenis beasiswa",
    )):
        kategori.add("aktif")

    if not kategori:
        kategori.add("status_belum_diketahui")

    return kategori


def _authoritative_beasiswa(record):
    if record.get("sumber") not in _SUMBER_BACA:
        return False
    if _adalah_artikel(
        str(record.get("judul", "")), str(record.get("url", ""))
    ):
        return False

    path = _path_url(record.get("url", ""))
    teks = str(record.get("teks", "")).lower()

    return (
        path.endswith("/beasiswa")
        or "informasi beasiswa" in teks
        or "daftar beasiswa" in teks
    )


def _potongan_nama(record, nama):
    for baris in _baris_bersih(record.get("teks", "")):
        if nama.lower() in baris.lower():
            return baris[:260]

    return str(record.get("cuplikan", ""))[:260]


def _beasiswa_answer(records, question):
    """Klasifikasikan beasiswa menurut bukti, tanpa klaim kelengkapan."""

    rendah = str(question).lower()
    if "beasiswa" not in rendah and "bantuan pendidikan" not in rendah:
        return None
    if not _adalah_pertanyaan_luas(question) and "status" not in rendah:
        return None

    indeks = {}
    for record in records:
        if record.get("sumber") not in _SUMBER_BACA:
            continue

        for nama in _ekstrak_nama_beasiswa(
            str(record.get("judul", "")) + "\n" + str(record.get("teks", ""))
        ):
            info = indeks.setdefault(nama, {})
            for kategori in _kategori_beasiswa(record):
                daftar = info.setdefault(kategori, [])
                url = str(record.get("url", ""))
                if url and url not in daftar:
                    daftar.append(url)

    if not indeks:
        return None

    bagian = []
    dipakai = {}

    def _tambah(judul, kategori, keterangan):
        butir = []
        for nama, peta in sorted(indeks.items(), key=lambda pasangan: pasangan[0].lower()):
            if kategori not in peta:
                continue

            urls = peta[kategori]
            rekam = next(
                (
                    record for record in records
                    if str(record.get("url", "")) in urls
                    and nama.lower() in (
                        str(record.get("judul", "")) + " " + str(record.get("teks", ""))
                    ).lower()
                ),
                None,
            )
            potongan = _potongan_nama(rekam, nama) if rekam else ""
            baris = "- %s" % nama
            if potongan:
                baris += " — %s" % potongan
            baris += " (Sumber: %s)" % ", ".join(urls)
            butir.append(baris)
            dipakai.setdefault(kategori, []).extend(urls)

        if butir:
            bagian.append("%s\n%s" % (judul, "\n".join(butir[:6])))

    _tambah(
        "1. Program dengan bukti masih aktif:",
        "aktif",
        "halaman menyatakan pendaftaran/tersedia",
    )
    _tambah(
        "2. Program yang pernah tersedia tetapi status saat ini belum diketahui:",
        "status_belum_diketahui",
        "status tidak dinyatakan secara eksplisit",
    )
    _tambah(
        "3. Program yang sudah ditutup atau kedaluwarsa:",
        "ditutup",
        "halaman menyatakan penutupan",
    )
    _tambah(
        "4. Berita historis tentang penerima beasiswa (bukan bukti program tersedia):",
        "berita_penerima",
        "berita penerima tidak membuktikan program masih dibuka",
    )

    if not bagian:
        return None

    if not any(_authoritative_beasiswa(record) for record in records):
        bagian.append(
            "Catatan: daftar ini hanya berdasarkan halaman resmi UIR yang "
            "berhasil dibaca. Kelengkapan seluruh program beasiswa UIR belum "
            "dapat diverifikasi dari sumber tersebut."
        )

    sumber = []
    for urls in dipakai.values():
        for url in urls:
            if url not in sumber:
                sumber.append(url)

    for record in records:
        if str(record.get("url", "")) in sumber:
            kategori = [
                nama for nama, urls in dipakai.items()
                if str(record.get("url", "")) in urls
            ]
            if kategori:
                record["digunakan_untuk"] = kategori

    return (
        "Berdasarkan halaman resmi UIR yang berhasil dibaca, informasi "
        "beasiswa dikelompokkan sebagai berikut:\n\n"
        + "\n\n".join(bagian)
        + "\n\nSumber:\n"
        + "\n".join("- " + url for url in sumber)
    )


def _klaim_beasiswa_didukung(answer, records, question):
    """Setiap nama program pada jawaban harus didukung halaman tersendiri."""

    rendah = str(question).lower()
    if "beasiswa" not in rendah and "bantuan pendidikan" not in rendah:
        return None

    nama = _ekstrak_nama_beasiswa(answer)
    if not nama:
        return None

    teks = "\n".join(
        str(record.get("judul", "")) + " " + str(record.get("teks", ""))
        for record in records
    ).lower()

    for tahun in re.findall(r"\b(?:19|20)\d{2}\b", str(answer)):
        if tahun not in teks:
            return False, (
                "Angka tahun %s tidak ditemukan pada halaman resmi yang terbaca."
                % tahun
            )

    tidak_didukung = [item for item in nama if item.lower() not in teks]

    if tidak_didukung:
        return False, (
            "Nama beasiswa '%s' tidak ditemukan pada halaman resmi yang terbaca."
            % tidak_didukung[0]
        )

    if re.search(r"\b(seluruh|lengkap|semua)\s+beasiswa\b", str(answer).lower()):
        if not any(_authoritative_beasiswa(record) for record in records):
            return False, (
                "Sumber yang terbaca tidak membuktikan kelengkapan daftar beasiswa."
            )

    return True, ""


_KATEGORI_PMB = (
    (
        "Pembuatan atau pembelian akun PMB",
        (
            "akun pmb", "pembelian akun", "membuat akun", "username",
            "password", "pendaftaran mahasiswa",
        ),
    ),
    (
        "Jalur masuk",
        (
            "jalur khusus", "jalur rapor", "rapor based test",
            "computer based test", "jalur masuk",
        ),
    ),
    (
        "Gelombang dan jadwal",
        ("gelombang", "jadwal", "tahun akademik", "periode pendaftaran"),
    ),
    (
        "Pembayaran atau biaya pendaftaran",
        ("nomor va", "biaya pendaftaran", "pembayaran", "virtual account"),
    ),
    (
        "Seleksi dan pengumuman",
        ("seleksi", "pengumuman hasil", "tes ", "ujian"),
    ),
)


def _tahun_terbesar(teks):
    tahun = [
        int(nilai) for nilai in
        re.findall(r"\b(?:19|20)\d{2}\b", str(teks or ""))
    ]
    return max(tahun) if tahun else 0


def _record_pmb(records):
    sah = [
        record for record in records
        if isinstance(record, dict)
        and record.get("sumber") in _SUMBER_BACA
        and not _adalah_artikel(
            str(record.get("judul", "")), str(record.get("url", ""))
        )
        and len(str(record.get("teks", ""))) > 250
        and _prioritas_url_pmb(record.get("url", "")) <= 1
    ]

    sah.sort(key=lambda record: (
        _prioritas_url_pmb(record.get("url", "")),
        -_tahun_terbesar(record.get("teks", "")),
    ))
    return sah


def _prosedur_answer(records, question):
    """Susun langkah PMB hanya dari potongan halaman resmi yang terbaca."""

    rendah = str(question).lower()
    if not any(tanda in rendah for tanda in (
        "pmb", "pendaftaran mahasiswa", "penerimaan mahasiswa",
        "cara daftar", "jalur masuk",
    )):
        return None

    sah = _record_pmb(records)
    if not sah:
        return None

    bagian = []
    belum = []
    dipakai = []

    for label, penanda in _KATEGORI_PMB:
        langkah = []

        for record in sah:
            for baris in _baris_bersih(record.get("teks", "")):
                if len(baris) < 40 or len(baris) > 500:
                    continue
                if not any(kata in baris.lower() for kata in penanda):
                    continue

                pasangan = (baris[:320], str(record.get("url", "")))
                if pasangan not in langkah:
                    langkah.append(pasangan)

            if len(langkah) >= 2:
                break

        if langkah:
            butir = []
            for teks, url in langkah:
                tahun = sorted(set(re.findall(r"\b(?:19|20)\d{2}\b", teks)))
                keterangan = (
                    " (tahun yang disebut: " + ", ".join(tahun) + ")"
                    if tahun else ""
                )
                butir.append(
                    "- %s%s (Sumber: %s)" % (teks, keterangan, url)
                )
                if url not in dipakai:
                    dipakai.append(url)
                for record in sah:
                    if str(record.get("url", "")) == url:
                        record.setdefault("digunakan_untuk", []).append(label)
            bagian.append("%s:\n%s" % (label, "\n".join(butir[:2])))
        else:
            belum.append(label)

    if not bagian:
        return None

    isi = (
        "Berdasarkan halaman resmi UIR yang berhasil dibaca, berikut langkah "
        "penerimaan mahasiswa baru yang dapat dipastikan:\n\n"
        + "\n\n".join(bagian)
    )

    if belum:
        isi += (
            "\n\nBagian yang belum ditemukan pada halaman resmi yang berhasil "
            "dibaca: " + ", ".join(belum) + "."
        )

    isi += (
        "\n\nCatatan: jawaban ini hanya memuat langkah yang didukung bukti; "
        "kelengkapan seluruh prosedur belum dapat dipastikan."
    )

    return isi + "\n\nSumber:\n" + "\n".join("- " + url for url in dipakai)


def _klaim_pmb_didukung(answer, records, question):
    """Prosedur PMB butuh halaman proses resmi, bukan hanya berita."""

    rendah = str(question).lower()
    if not any(tanda in rendah for tanda in (
        "pmb", "pendaftaran mahasiswa", "penerimaan mahasiswa",
        "cara daftar", "jalur masuk",
    )):
        return None

    if not _record_pmb(records):
        return False, (
            "Halaman resmi yang menjelaskan proses penerimaan mahasiswa baru "
            "belum berhasil dibaca."
        )

    return _klaim_umum_didukung(answer, records, question)


def _adalah_pertanyaan_lanjutan(question):
    rendah = str(question).lower()

    return any(tanda in rendah for tanda in _TANDA_LANJUTAN)


def _adalah_pertanyaan_luas(question):
    """Pertanyaan yang meminta daftar/kumpulan informasi, bukan satu fakta."""

    rendah = re.sub(r"\s+", " ", str(question).lower())

    if _is_list_question(question) or _butuh_katalog(question):
        return True

    return any(
        kata in rendah
        for kata in (
            "apa saja", "sebutkan", "daftar", "seluruh", "semua",
            "banyak", "beberapa",
        )
    )


def _adalah_pertanyaan_ambigu(question):
    """Pertanyaan deiktik tanpa topik yang dapat diverifikasi."""

    rendah = re.sub(r"\s+", " ", str(question).strip().lower())

    if not rendah or len(rendah) > 120:
        return False
    if _URL_RE.search(question) or _topic(question):
        return False
    if _topik_pertanyaan(question):
        return False
    if (
        _adalah_profil_pertanyaan(question)
        or _adalah_pertanyaan_tahun_berdiri(question)
    ):
        return False
    if any(penanda in rendah for penanda in _PENANDA_UIR):
        return False

    return any(tanda in rendah for tanda in _TANDA_AMBIGU)


def _potongan_relevan(teks, pertanyaan):
    """Ambil bagian teks halaman yang benar-benar berkaitan dengan pertanyaan."""

    if not isinstance(teks, str) or not teks.strip():
        return []

    token_pertanyaan = _tokens(pertanyaan)
    topik = set(_topik_pertanyaan(pertanyaan))
    ratakan = re.sub(r"[ \t]+", " ", teks)
    calon = []

    for baris in re.split(r"(?<=[.!?])\s+|\n+", ratakan):
        baris = baris.strip(" \t•-")

        if len(baris) < 40 or len(baris) > 600:
            continue

        rendah = baris.lower()
        token = _tokens(baris)
        skor = len(token & token_pertanyaan)

        for nama in topik:
            if any(kata in rendah for kata in _TOPIK_PENCARIAN.get(nama, ())):
                skor += 2

        if skor <= 0:
            continue

        if baris not in [isi for _, isi in calon]:
            calon.append((skor, baris))

    calon.sort(key=lambda pasangan: -pasangan[0])

    return [isi for _, isi in calon[:4]]


def _ringkas_untuk_model(result, pertanyaan=""):
    """Salinan hasil tool yang ringkas untuk dikirim ke model.

    Fungsi ini TIDAK mengubah data asli yang dipakai verifikasi. Hanya salinan
    untuk payload model yang dipangkas: teks halaman diganti cuplikan relevan,
    daftar hasil pencarian dibatasi, dan cuplikan panjang dipendekkan.
    """

    if not isinstance(result, dict):
        return result

    salinan = dict(result)
    data = salinan.get("data")

    if isinstance(data, dict):
        data_salin = dict(data)
        teks = data_salin.get("teks")
        if isinstance(teks, str) and len(teks) > MAX_EVIDEN_MODEL:
            potongan = _potongan_relevan(teks, pertanyaan)
            ringkas = "\n".join(potongan)
            if len(ringkas) < 400:
                ringkas = teks[:MAX_EVIDEN_MODEL]
            data_salin["teks"] = ringkas[:MAX_EVIDEN_MODEL]
            data_salin["teks_diringkas"] = True
            data_salin["panjang_asli"] = len(teks)
        salinan["data"] = data_salin

    teks_langsung = salinan.get("teks")
    if isinstance(teks_langsung, str) and len(teks_langsung) > MAX_EVIDEN_MODEL:
        potongan = _potongan_relevan(teks_langsung, pertanyaan)
        ringkas = "\n".join(potongan)
        if len(ringkas) < 400:
            ringkas = teks_langsung[:MAX_EVIDEN_MODEL]
        salinan["teks"] = ringkas[:MAX_EVIDEN_MODEL]
        salinan["teks_diringkas"] = True
        salinan["panjang_asli"] = len(teks_langsung)

    hasil = salinan.get("results")
    if isinstance(hasil, list):
        salinan["results"] = [
            {
                **item,
                "cuplikan": str(item.get("cuplikan", ""))[:MAX_CUPLIKAN_MODEL],
            }
            if isinstance(item, dict)
            else item
            for item in hasil[:MAX_HASIL_MODEL]
        ]
        if len(hasil) > MAX_HASIL_MODEL:
            salinan["results_diringkas"] = True

    return salinan


def _gabungkan_bukti(daftar, rekam):
    """Hapus duplikat URL sambil mempertahankan asal setiap informasi."""

    if not isinstance(rekam, dict) or not rekam.get("url"):
        return daftar

    for lama in daftar:
        if lama.get("url") != rekam["url"]:
            continue

        for kunci in (
            "judul", "waktu", "waktu_cache", "panjang",
            "sumber_kandidat", "pertanyaan",
        ):
            if rekam.get(kunci) and not lama.get(kunci):
                lama[kunci] = rekam[kunci]

        for potongan in rekam.get("bagian") or []:
            if potongan not in lama.setdefault("bagian", []):
                lama["bagian"].append(potongan)

        return daftar

    daftar.append(rekam)
    return daftar


def _entitas_kapital(kalimat):
    """Ambil calon nama/entitas dari huruf kapital di tengah kalimat."""

    kata = re.findall(r"\b[A-Z][A-Za-z'’-]{2,}\b", kalimat)

    return [
        nilai for nilai in kata
        if nilai.lower() not in _KATA_JAWABAN_UMUM
        and not nilai.isupper()
    ]


def _klaim_umum_didukung(answer, records, question):
    """Verifikasi konservatif untuk topik di luar katalog fakultas/prodi."""

    teks_bukti = " ".join(
        re.sub(r"\s+", " ", str(record.get("teks", "")))
        for record in records
        if isinstance(record, dict)
    )
    rendah = teks_bukti.lower()
    token_bukti = _tokens(teks_bukti)

    for kalimat in re.split(r"(?<=[.!?])\s+|\n+", answer):
        baris = kalimat.strip(" \t•-")

        if not baris or re.match(r"^sumber\b", baris, re.I):
            continue

        if re.match(r"^catatan\b", baris, re.I):
            continue

        token = _tokens(baris) - _KATA_JAWABAN_UMUM

        if len(token) < 2:
            continue

        for angka in re.findall(r"\b\d+(?:[.,]\d+)?\b", baris):
            if angka not in rendah:
                return False, (
                    "Angka '%s' pada jawaban tidak ditemukan pada "
                    "halaman resmi yang terbaca." % angka
                )

        for nama in _entitas_kapital(baris):
            if nama.lower() not in rendah:
                return False, (
                    "Nama '%s' pada jawaban tidak ditemukan pada "
                    "halaman resmi yang terbaca." % nama
                )

        # Tidak ada pemeriksaan irisan token di sini. Model boleh merangkai
        # ulang kalimat dengan kata pilihannya sendiri. Klaim faktual yang
        # spesifik sudah dicek lewat angka dan nama yang tercantum di atas;
        # bila keduanya tidak ada, tidak ada dasar untuk menolak ringkasan
        # umum tersebut.

    return True, ""


def _deteksi_konflik(records):
    """Temukan fakta yang bertentangan antarhalaman yang terbaca."""

    konflik = []
    tahun_berdiri = set()

    for record in records:
        teks = re.sub(r"\s+", " ", str(record.get("teks", "")))

        for kalimat in re.split(r"(?<=[.!?])\s+", teks):
            rendah = kalimat.lower()

            if not re.search(r"\b(?:berdiri|didirikan)\b", rendah):
                continue
            if not (
                "universitas islam riau" in rendah
                or re.search(r"\buir\b", rendah)
            ):
                continue
            if any(
                kata in rendah
                for kata in (
                    "program studi", "prodi", "fakultas", "doktor",
                    "magister", "jurusan",
                )
            ):
                continue

            tahun_berdiri.update(re.findall(r"\b(?:19|20)\d{2}\b", kalimat))

    if len(tahun_berdiri) > 1:
        konflik.append(
            "Tahun berdiri universitas disebutkan berbeda pada beberapa "
            "halaman: " + ", ".join(sorted(tahun_berdiri)) + "."
        )

    skor_akreditasi = {}

    for record in records:
        for nama, nilai in peta_akreditasi(
            str(record.get("teks", ""))
        ).items():
            skor_akreditasi.setdefault(nama.lower(), set()).add(nilai.upper())

    bertentangan = [
        nama for nama, nilai in skor_akreditasi.items()
        if len(nilai) > 1
    ]

    if bertentangan:
        konflik.append(
            "Akreditasi untuk " + ", ".join(sorted(bertentangan)[:5])
            + " tidak konsisten antarhalaman yang terbaca."
        )

    return konflik


def _adalah_artikel(title, url):
    """Kenali berita/pengumuman dari judul dan URL, bukan dari isi saja."""
    if any(penanda in url for penanda in _BERITA_PATH):
        return True

    slug = url.rstrip("/").rsplit("/", 1)[-1]
    if slug.isdigit():
        return True

    return any(penanda in title for penanda in _BERITA_JUDUL)


def _authoritative_catalog(record, topic):
    """Syarat konservatif untuk pertanyaan daftar: bukan sekadar berita.

    Pengenalan katalog memakai empat hal:
    1. provenance ``sumber`` (harus hasil baca halaman, bukan keluaran model),
    2. ``teks`` halaman yang benar-benar terbaca,
    3. judul dan URL halaman,
    4. penanda artikel/berita yang menyingkirkan kandidat palsu.

    Halaman seperti "/program-pendidikan/" kini dikenali karena isinya memuat
    "FAKULTAS DAN PASCASARJANA" dan "Tabel Akreditasi Program Studi", walaupun
    judulnya hanya "Program Pendidikan".
    """
    if record.get("sumber") not in _SUMBER_BACA:
        return False

    title = str(record.get("judul", "")).lower()
    url = str(record.get("url", "")).lower()
    page_text = str(record.get("teks", "")).lower()

    # Halaman terlalu pendek tidak cukup untuk dijadikan bukti daftar.
    if len(page_text) <= 250:
        return False

    if _adalah_artikel(title, url):
        return False

    if topic == "fakultas":
        kata_topik = ("fakultas",)
    elif topic == "program studi":
        kata_topik = ("program studi", "program-studi", "prodi")
    else:
        return False

    # Satu penyebutan dalam artikel tidak dianggap sebagai katalog.
    jumlah_topik = sum(page_text.count(kata) for kata in kata_topik)
    if jumlah_topik < 2:
        return False

    # Kata "daftar" pada judul berita tidak cukup. Halaman katalog resmi
    # dikenali dari penanda struktural pada ISI halaman yang benar-benar
    # dibaca, misalnya "FAKULTAS DAN PASCASARJANA" atau "Nama Prodi".
    struktur_isi = any(
        penanda in page_text for penanda in _KATALOG_TEKS
    )

    return struktur_isi


def _unit_fakultas(question):
    """Nama fakultas yang disebut eksplisit setelah kata 'fakultas'."""
    return [
        nama
        for nama in re.findall(
            r"\bfakultas\s+([a-z]+)\b", str(question or "").lower()
        )
        if nama not in {
            "di", "dan", "atau", "yang", "the", "tersebut", "ini", "itu",
            # Frasa umum yang bukan nama unit: "fakultas Universitas Islam
            # Riau", "daftar fakultas", "jumlah fakultas", dan sejenisnya.
            "universitas", "islam", "riau", "uir", "daftar", "sebutkan",
            "apa", "saja", "tersedia", "semua", "seluruh", "lengkap",
            "jumlah", "berapa",
        }
    ]


def _jumlah_program_studi_answer(records, question):
    """Jawab pertanyaan jumlah program studi dari kalimat angka di katalog.

    Fungsi ini TIDAK menyimpan angka. Kalimat diambil verbatim dari halaman
    resmi yang berhasil dibaca dan dipakai hanya bila kalimat itu memang
    menyebut jumlah program studi secara eksplisit.
    """
    pertanyaan = str(question).lower()

    if not re.search(r"\bberapa\b", pertanyaan):
        return None
    if not re.search(r"\b(program studi|prodi)\b", pertanyaan):
        return None

    # Pertanyaan yang dibatasi pada satu fakultas tidak dijawab dengan jumlah
    # seluruh program studi UIR.
    if _unit_fakultas(pertanyaan):
        return None

    for record in records or []:
        if not _authoritative_catalog(record, "program studi"):
            continue

        teks = re.sub(r"\s+", " ", str(record.get("teks", "")))

        for kalimat in re.split(r"(?<=[.!?])\s+", teks):
            kalimat = kalimat.strip(" \t•-")
            rendah = kalimat.lower()

            if not 40 <= len(kalimat) <= 600:
                continue
            if not re.search(
                r"\d+\s+(?:program studi|prodi)\b", rendah
            ):
                continue
            if not re.search(
                r"(?:menyediakan|terdapat|terdiri|memiliki|jumlah|ada)",
                rendah,
            ):
                continue

            return "Berdasarkan halaman resmi UIR:\n- " + kalimat

    return None


def _jumlah_fakultas_answer(records, question):
    """Jawab jumlah fakultas hanya dari kalimat angka pada katalog resmi.

    Sama seperti jumlah program studi, angka tidak pernah disimpan di kode.
    Kalimat diambil verbatim dari halaman yang benar-benar terbaca.
    """
    pertanyaan = str(question).lower()

    if not re.search(r"\bberapa\b", pertanyaan):
        return None
    if not re.search(r"\bfakultas\b", pertanyaan):
        return None
    if _unit_fakultas(pertanyaan):
        # Jumlah satu fakultas tidak sama dengan jumlah seluruh fakultas UIR.
        return None

    for record in records or []:
        if not _authoritative_catalog(record, "fakultas"):
            continue

        teks = re.sub(r"\s+", " ", str(record.get("teks", "")))

        for kalimat in re.split(r"(?<=[.!?])\s+", teks):
            kalimat = kalimat.strip(" \t•-")
            rendah = kalimat.lower()

            if not 40 <= len(kalimat) <= 600:
                continue
            if not re.search(r"\d+\s+fakultas\b", rendah):
                continue
            if not re.search(
                r"(?:menyediakan|terdapat|terdiri|memiliki|jumlah|ada)",
                rendah,
            ):
                continue

            return "Berdasarkan halaman resmi UIR:\n- " + kalimat

    return None


def _adalah_profil_pertanyaan(question):
    """Apakah pertanyaan meminta gambaran umum/profil/sejarah UIR?"""
    q = str(question).lower()

    return any(penanda in q for penanda in (
        "apa itu",
        "profil",
        "sejarah",
        "tentang uir",
        "tentang universitas islam riau",
    ))


def _adalah_pertanyaan_tahun_berdiri(question):
    """Kenali pertanyaan tahun berdiri UIR, bukan berdirinya unit/artikel."""
    q = str(question).lower()

    return (
        ("berdiri" in q or "didirikan" in q)
        and ("universitas" in q or "uir" in q)
    )


_PENANDA_UIR = (
    "uir",
    "universitas islam riau",
    "universitas",
    "kampus",
    "mahasiswa",
)

_TAHUN_DEPAN_RE = re.compile(r"\b(20[2-9]\d|21\d{2})\b")


def _adalah_pertanyaan_di_luar_cakupan(question):
    """Pertanyaan tanpa penanda UIR/topik berada di luar cakupan chatbot."""
    q = str(question).lower()

    # Pertanyaan yang memuat URL ditangani oleh validasi URL yang sudah ada;
    # jangan menggolongkannya sebagai topik umum di luar cakupan.
    if _URL_RE.search(question):
        return False
    if perencana.pahami_dosen(question)["aktif"]:
        return False
    if _topic(question):
        return False
    if _adalah_profil_pertanyaan(question):
        return False
    if _adalah_pertanyaan_tahun_berdiri(question):
        return False

    return not any(penanda in q for penanda in _PENANDA_UIR)


def _tahun_diminta(question):
    """Tahun masa depan yang disebut pada pertanyaan, misalnya 2030."""
    return set(_TAHUN_DEPAN_RE.findall(str(question)))


def _kata_topik_pertanyaan(question):
    """Kata isi pertanyaan tanpa kata tahun dan tanpa angka tahun."""
    return {
        kata for kata in _tokens(question)
        if kata != "tahun" and not kata.isdigit()
    }


def _alasan_jawaban_melenceng(question):
    """Pesan jujur ketika jawaban tidak menyentuh pokok pertanyaan."""
    q = str(question or "").lower()

    if re.search(r"\bwakil\b", q):
        return (
            "Data wakil rektor belum ditemukan pada halaman resmi UIR "
            "yang berhasil dibaca. Saya tidak akan menebak namanya."
        )
    if re.search(r"\bdekan\b", q):
        return (
            "Nama dekan yang diminta belum ditemukan pada halaman resmi "
            "UIR yang berhasil dibaca. Saya tidak akan menebak namanya."
        )
    if re.search(r"\bfakultas\b", q) and re.search(r"\bberapa\b|\bjumlah\b", q):
        return (
            "Jumlah fakultas tidak ditemukan secara eksplisit pada halaman "
            "resmi UIR yang berhasil dibaca. Saya tidak akan memperkirakannya."
        )

    return (
        "Jawaban yang ditemukan tidak menjawab pokok pertanyaan. "
        "Informasi yang diminta belum ditemukan pada halaman resmi UIR "
        "yang berhasil dibaca."
    )


def _jawaban_menjawab_pertanyaan(question, answer):
    """Guard final fail-closed untuk mencegah jawaban melenceng.

    Pemeriksaan ini sengaja konservatif. Bila ragu, fungsi mengembalikan
    False agar aplikasi mengakui keterbatasan daripada mengklaim jawaban
    yang tidak benar-benar menjawab pertanyaan.
    """
    q = str(question or "").lower()
    isi = str(answer or "").split("\n\nSumber", 1)[0].lower()

    if not isi.strip():
        return False

    kata = _tokens(isi)

    # "Rektor" saja tidak menjawab pertanyaan tentang "wakil rektor".
    if re.search(r"\bwakil\b", q) and "wakil" not in kata:
        return False

    # "Fakultas Pertanian" pada berita tidak menjawab "Fakultas Teknik".
    for unit in _unit_fakultas(question):
        if unit not in kata:
            return False

    if re.search(r"\bdekan\b", q) and not (
        "dekan" in kata or "pimpinan" in kata
    ):
        return False

    # Angka tanggal pada artikel bukan jawaban pertanyaan jumlah.
    # Pola ini hanya untuk pertanyaan yang memang meminta jumlah fakultas
    # atau program studi, bukan pertanyaan lain yang menyebut fakultas.
    jumlah_fakultas = re.search(
        r"\b(?:berapa|jumlah)\s+(?:jumlah\s+|banyak\s+)?fakultas\b", q
    )
    jumlah_prodi = re.search(
        r"\b(?:berapa|jumlah)\s+(?:jumlah\s+|banyak\s+)?"
        r"(?:program studi|prodi)\b",
        q,
    )

    if jumlah_fakultas and not re.search(r"\d+\s+fakultas\b", isi):
        return False
    if jumlah_prodi and not re.search(
        r"\d+\s+(?:program studi|prodi)\b", isi
    ):
        return False

    return True


def _tahun_tersedia_di_bukti(tahun, records, kata_topik=None):
    """Tahun harus muncul pada kalimat yang membahas pokok pertanyaan.

    Kehadiran tahun di artikel yang tidak berkaitan (misalnya target produksi
    minyak 2030) tidak boleh dianggap sebagai bukti jumlah mahasiswa UIR.
    """
    if not tahun:
        return True

    topik = set(kata_topik or ())

    for record in records:
        if not isinstance(record, dict):
            continue

        teks = re.sub(r"\s+", " ", str(record.get("teks", "")))

        for kalimat in re.split(r"(?<=[.!?])\s+|\n+", teks):
            rendah = kalimat.lower()

            if not any(nilai in rendah for nilai in tahun):
                continue
            if not topik or any(kata in rendah for kata in topik):
                return True

    return False


def _prioritas_url_profil(url):
    """Utamakan halaman profil resmi saat mencari bukti sejarah UIR."""
    alamat = str(url).lower()
    bagian = urllib.parse.urlsplit(alamat)

    # Artefak editor seperti "/?elementor_library=profil-uir" bukan halaman
    # publik yang stabil dan sengaja ditempatkan paling belakang.
    if bagian.query:
        return 3

    if bagian.path.rstrip("/").endswith("/profil"):
        return 0

    return _prioritas_url(alamat) + 1


def _authoritative_profile(record):
    """Kenali halaman profil resmi dari provenance, URL, dan isi halaman."""
    if record.get("sumber") not in _SUMBER_BACA:
        return False

    teks = str(record.get("teks", ""))
    if len(teks) <= 250:
        return False

    url = str(record.get("url", ""))
    judul = str(record.get("judul", ""))

    if _adalah_artikel(judul, url):
        return False

    bagian = urllib.parse.urlsplit(url)

    # URL dengan query string biasanya artefak editor, bukan halaman publik
    # yang stabil, misalnya "/?elementor_library=profil-uir".
    if bagian.query:
        return False

    path = bagian.path.lower().rstrip("/")
    label = (judul + " " + url).lower()

    halaman_profil = (
        path.endswith("/profil")
        or any(penanda in label for penanda in (
            "profil universitas islam riau",
            "profil uir",
            "sejarah dan visi misi uir",
        ))
    )

    if not halaman_profil:
        return False

    isi = teks.lower()
    return (
        "sejarah singkat" in isi
        and "universitas islam riau" in isi
    )


def _profil_answer(records):
    """Susun jawaban profil hanya dari kalimat halaman profil yang terbaca."""
    profil = [
        record for record in records
        if _authoritative_profile(record)
    ]

    if not profil:
        return None

    pola = re.compile(
        r"(Universitas Islam Riau(?: \(UIR\))?\s+"
        r"(?:adalah|merupakan)[^.]{20,500}\.)",
        re.I,
    )

    for record in profil:
        teks = re.sub(r"\s+", " ", str(record.get("teks", "")))

        for cocok in pola.finditer(teks):
            kalimat = cocok.group(1).strip()

            return (
                "Berdasarkan halaman profil resmi UIR, "
                + kalimat
                + "\n\nSumber:\n- " + str(record.get("url", ""))
            )

    return None


# ------------------------------------------------------------------
# Bukti lokasi/alamat/kontak. Semua nilai diambil verbatim dari halaman
# kontak/profil yang terbaca; tidak ada alamat atau nomor yang disimpan di sini.
# ------------------------------------------------------------------

_ALAMAT_LABEL = frozenset({
    "alamat", "alamat kampus", "alamat uir",
    "alamat universitas islam riau", "lokasi kampus", "letak kampus",
})


def _baris_alamat(baris):
    """Kenali baris alamat jalan tanpa bergantung pada nama tempat tertentu."""

    nilai = re.sub(r"\s+", " ", str(baris or "")).strip(" ,;:-")
    rendah = nilai.lower()

    if not re.search(r"\b(jl|jalan)\b", rendah):
        return False
    if not re.search(r"\d", nilai):
        return False
    if len(nilai) < 25 or len(nilai) > 300:
        return False

    return True


def _ekstrak_alamat(teks):
    """Ambil satu baris alamat dari teks halaman yang sudah dibaca."""

    baris = _baris_bersih(teks)

    for indeks, nilai in enumerate(baris):
        if nilai.lower().strip(": ") not in _ALAMAT_LABEL:
            continue

        for calon in baris[indeks + 1:indeks + 5]:
            if _baris_alamat(calon):
                return calon

    for nilai in baris:
        if _baris_alamat(nilai) and (
            "kode pos" in nilai.lower() or re.search(r"\b\d{5}\b", nilai)
        ):
            return nilai

    for nilai in baris:
        if _baris_alamat(nilai):
            return nilai

    return ""


def _ekstrak_kontak(teks):
    """Ambil baris kontak yang benar-benar tertulis pada halaman."""

    hasil = []

    for nilai in _baris_bersih(teks):
        rendah = nilai.lower()

        if re.match(
            r"^(telepon|telp|fax|wa|whatsapp|email|e-mail)\s*:",
            rendah,
        ) or re.search(r"\[at\]|\(at\)", rendah):
            if nilai not in hasil:
                hasil.append(nilai)

        if len(hasil) >= 6:
            break

    return hasil


def _filter_kontak(daftar, question):
    """Saring baris kontak sesuai bagian yang diminta pengguna."""

    rendah = str(question).lower()

    if "email" in rendah or "e-mail" in rendah:
        return [
            baris for baris in daftar
            if baris.lower().startswith(("email", "e-mail"))
            or "[at]" in baris.lower()
            or "(at)" in baris.lower()
        ]
    if "fax" in rendah:
        return [baris for baris in daftar if baris.lower().startswith("fax")]
    if "telepon" in rendah or "telp" in rendah:
        return [
            baris for baris in daftar
            if baris.lower().startswith(("telepon", "telp"))
        ]
    if "wa" in rendah or "whatsapp" in rendah:
        return [
            baris for baris in daftar
            if baris.lower().startswith(("wa", "whatsapp"))
        ]

    return daftar


def _authoritative_lokasi(record):
    """Halaman kontak/profil resmi yang boleh dipakai sebagai bukti alamat."""

    if not isinstance(record, dict):
        return False
    if record.get("sumber") not in _SUMBER_BACA:
        return False

    judul = str(record.get("judul", ""))
    url = str(record.get("url", ""))
    teks = str(record.get("teks", ""))

    if _adalah_artikel(judul, url):
        return False

    bagian = urllib.parse.urlsplit(url)
    if bagian.query:
        return False

    path = bagian.path.lower().rstrip("/")
    if not (
        path.endswith("/kontak")
        or path.endswith("/maps")
        or path.endswith("/profil")
        or "hubungi" in path
    ):
        return False

    if len(teks) <= 120:
        return False

    return bool(_ekstrak_alamat(teks))


def _lokasi_answer(records, question):
    """Susun jawaban lokasi/kontak dari baris halaman resmi yang terbaca."""

    info = perencana.pahami_lokasi(question)
    if not info["aktif"]:
        return None

    sah = [
        record for record in records
        if _authoritative_lokasi(record)
    ]
    sah.sort(key=lambda record: (
        _prioritas_url_lokasi(record.get("url", "")),
        -len(str(record.get("teks", ""))),
    ))

    if not sah:
        return None

    record = sah[0]
    teks = str(record.get("teks", ""))
    alamat = _ekstrak_alamat(teks)
    kontak = _filter_kontak(_ekstrak_kontak(teks), question)

    if info["kind"] == "kontak":
        butir = kontak or ([alamat] if alamat else [])
        judul = "Kontak UIR"
    else:
        butir = [alamat] if alamat else []
        judul = "Alamat kampus UIR"

    if not butir or any(not item for item in butir):
        return None

    isi = (
        "Berdasarkan halaman resmi UIR yang berhasil dibaca, "
        + judul + ":\n\n"
        + "\n".join("- " + item for item in butir)
    )

    if info["kind"] == "peta":
        isi += (
            "\n\nCatatan: tautan peta tidak disertakan karena tidak muncul "
            "sebagai URL pada teks halaman yang berhasil dibaca."
        )

    record["digunakan_untuk"] = ["lokasi/kontak UIR"]

    return (
        isi + "\n\nSumber:\n- " + str(record.get("url", ""))
    )


def _baris_klaim_lokasi(baris):
    rendah = str(baris or "").lower().strip(" \t•-")

    return bool(
        (
            re.search(r"\b(jl|jalan)\b", rendah)
            and re.search(r"\d", rendah)
        )
        or re.match(
            r"^(telepon|telp|fax|wa|whatsapp|email|e-mail)\s*:",
            rendah,
        )
        or re.search(r"\[at\]|\(at\)", rendah)
    )


def _klaim_lokasi_didukung(answer, records, question):
    """Verifikasi alamat/kontak hanya terhadap halaman kontak/profil resmi."""

    info = perencana.pahami_lokasi(question)
    if not info["aktif"]:
        return None

    sah = [
        record for record in records
        if _authoritative_lokasi(record)
    ]
    if not sah:
        return False, (
            "Alamat/lokasi kampus tidak ditemukan pada halaman kontak/profil "
            "resmi UIR yang berhasil dibaca."
        )

    teks = "\n".join(
        "\n".join(_baris_bersih(record.get("teks", "")))
        for record in sah
    ).lower()
    isi = str(answer or "").split("\n\nSumber", 1)[0]
    klaim = [
        re.sub(r"\s+", " ", baris).strip(" \t•-")
        for baris in isi.splitlines()
        if _baris_klaim_lokasi(baris)
    ]

    if not klaim:
        return False, (
            "Jawaban tidak memuat alamat atau kontak yang dapat diverifikasi "
            "dari halaman resmi yang terbaca."
        )

    for baris in klaim:
        for angka in re.findall(r"\b\d+(?:[.,]\d+)?\b", baris):
            if angka not in teks:
                return False, (
                    "Angka '%s' pada alamat/kontak tidak ditemukan pada "
                    "halaman resmi yang terbaca." % angka
                )

        normal = baris.lower().strip(" .;:")
        if normal not in teks:
            return False, (
                "Baris '%s' tidak ditemukan secara eksplisit pada halaman "
                "kontak/profil resmi yang terbaca." % baris[:120]
            )

    url_sah = {str(record.get("url", "")) for record in sah}
    for url in _URL_RE.findall(str(answer or "")):
        if url.rstrip(".,;") not in url_sah:
            return False, (
                "Tautan '%s' tidak mendukung klaim lokasi/kontak UIR." % url
            )

    return True, ""


def _extract_answer_items(answer):
    """Ambil butir jawaban, bukan tautan pada bagian ``Sumber:``."""
    items = []
    bagian_sumber = False

    for line in answer.splitlines():
        line = line.strip()

        if not line:
            continue

        if re.match(r"^sumber\s*:", line, re.I):
            bagian_sumber = True
            continue

        if bagian_sumber:
            continue

        match = re.match(r"^(?:[-*•]|\d+[.)])\s*(.+)$", line)

        if not match:
            continue

        value = match.group(1).strip()
        value = re.sub(r"\s*\([^)]*\)\s*$", "", value)
        value = re.sub(r"\s*[—–:-].*$", "", value).strip(" .;")

        if re.fullmatch(r"(?:https?://|www\.)\S+", value, re.I):
            continue

        if len(value) >= 3:
            items.append(value)

    return items


def _jumlah_diizinkan(parse, topic, teks):
    """Kumpulkan jumlah yang benar-benar ada di katalog (bukan perkiraan)."""
    if topic == "fakultas":
        nilai = {len(parse["fakultas"])}
        pola = r"(\d+)\s+fakultas"
    else:
        nilai = {len(parse["program_studi"])}
        pola = r"(\d+)\s+(?:program studi|prodi)"

    for angka in re.findall(pola, teks, re.I):
        nilai.add(int(angka))

    return nilai


def _cek_jumlah(answer, topic, diizinkan):
    """Tolak angka jumlah yang tidak tertulis di sumber."""
    if topic == "fakultas":
        pola = r"(\d+)\s+fakultas|fakultas\s+(\d+)"
        label = "fakultas"
    else:
        pola = (
            r"(\d+)\s+(?:program studi|prodi)"
            r"|(?:program studi|prodi)\s+(\d+)"
        )
        label = "program studi"

    for cocok in re.finditer(pola, answer, re.I):
        angka = int(cocok.group(1) or cocok.group(2))

        if angka not in diizinkan:
            return False, (
                f"Jumlah {label} {angka} tidak ditemukan secara eksplisit "
                "pada sumber."
            )

    return True, ""


def _cek_akreditasi(answer, peta):
    """ pastikan akreditasi berasal dari baris prodi yang disebutkan."""
    disebut = {
        nama: akreditasi
        for nama, akreditasi in peta.items()
        if nama in answer.lower()
    }

    if not disebut:
        return True, ""

    di_jawaban = {
        nilai.upper() for nilai in AKREDITASI_RE.findall(answer)
    }

    if not di_jawaban:
        return True, ""

    diizinkan = set(disebut.values())

    if not di_jawaban <= diizinkan:
        return False, (
            "Akreditasi yang disebutkan tidak ditemukan secara eksplisit "
            "pada baris program studi yang sesuai."
        )

    return True, ""


def _cek_tahun_berdiri(answer, records):
    """Verifikasi tahun berdiri universitas, bukan berdirinya unit/artikel."""
    tahun = set(re.findall(r"\b(?:19|20)\d{2}\b", str(answer)))

    if not tahun:
        return False, (
            "Tahun berdiri universitas tidak ditemukan secara eksplisit "
            "pada sumber."
        )

    # Teks halaman hasil ekstraksi sering terbungkus baris di tengah
    # kalimat. Ratakan spasi tiap halaman dulu agar "berdiri", nama UIR,
    # dan tahun tetap berada dalam satu kalimat yang sama saat diuji.
    teks = "\n".join(
        re.sub(r"\s+", " ", str(record.get("teks", "")))
        for record in records
        if isinstance(record, dict)
    )

    kalimat = re.split(r"(?<=[.!?])\s+|\n+", teks)

    for nilai in tahun:
        for baris in kalimat:
            rendah = baris.lower()

            if (
                nilai in rendah
                and re.search(r"\b(?:berdiri|didirikan)\b", rendah)
                and (
                    "universitas islam riau" in rendah
                    or re.search(r"\buir\b", rendah)
                )
                and not any(
                    kata in rendah
                    for kata in (
                        "program studi", "prodi", "fakultas",
                        "doktor", "magister", "jurusan",
                    )
                )
            ):
                return True, ""

    return False, (
        "Tahun berdiri universitas tidak ditemukan secara eksplisit "
        "pada sumber."
    )


def _claims_supported(answer, records, question):
    """Validasi klaim terhadap BAGIAN katalog yang sesuai.

    Nama fakultas hanya boleh diambil dari bagian fakultas. Nama program studi
    hanya boleh diambil dari kolom ``Nama Prodi`` pada tabel akreditasi.
    Label navigasi seperti ``Gen Unggul`` atau ``PMB UIR`` tidak pernah
    menjadi bukti. Pemeriksaan tetap fail-closed.
    """
    if _adalah_pertanyaan_tahun_berdiri(question):
        return _cek_tahun_berdiri(answer, records)

    tahun_diminta = _tahun_diminta(question)
    if tahun_diminta and not _tahun_tersedia_di_bukti(
        tahun_diminta,
        records,
        _kata_topik_pertanyaan(question),
    ):
        return False, (
            "Angka untuk tahun yang diminta tidak ditemukan pada halaman "
            "resmi UIR yang berhasil dibaca."
        )

    # Pertanyaan "berapa" wajib dijawab dengan angka. Jawaban naratif tanpa
    # angka tidak boleh lolos hanya karena menyebut kata kunci topik.
    if re.search(r"\bberapa\b", str(question), re.I):
        if not re.search(r"\d", str(answer)):
            return False, (
                "Jawaban tidak memuat angka untuk pertanyaan jumlah."
            )

    daftar_topik = set(_topik_pertanyaan(question))

    # Verifikasi per klaim: setiap nama pejabat, nama beasiswa, dan langkah
    # prosedur harus didukung oleh halaman yang menyatakannya. Jika aturan
    # khusus tidak menemukan klaim yang dapat dinilai, kembalikan None agar
    # verifikasi umum tetap berjalan.
    if "pimpinan" in daftar_topik:
        khusus = _klaim_identitas_didukung(answer, records, question)
        if khusus is not None:
            return khusus

    if "beasiswa" in daftar_topik:
        khusus = _klaim_beasiswa_didukung(answer, records, question)
        if khusus is not None:
            return khusus

    if "pmb" in daftar_topik:
        khusus = _klaim_pmb_didukung(answer, records, question)
        if khusus is not None:
            return khusus

    if "lokasi" in daftar_topik:
        khusus = _klaim_lokasi_didukung(answer, records, question)
        if khusus is not None:
            return khusus

    topic = _topic(question)

    if not topic:
        return _klaim_umum_didukung(answer, records, question)

    catalogs = [
        record
        for record in records
        if _authoritative_catalog(record, topic)
    ]

    if not catalogs:
        return (
            False,
            "Belum ada halaman resmi berbentuk katalog/daftar yang mendukung permintaan daftar.",
        )

    teks = "\n".join(
        str(record.get("teks", "")) for record in catalogs
    )
    parses = [parse_katalog(str(record.get("teks", ""))) for record in catalogs]

    if topic == "fakultas":
        diizinkan = {
            nama.lower()
            for parse in parses
            for nama in parse["fakultas"]
        }
        peta = {}
    else:
        peta = {}
        for record in catalogs:
            peta.update(peta_akreditasi(str(record.get("teks", ""))))

        # Pertanyaan seperti "program studi Fakultas Teknik" hanya boleh
        # memakai baris tabel yang namanya diawali "Teknik ".
        if "fakultas teknik" in question.lower():
            peta = {
                nama: akreditasi
                for nama, akreditasi in peta.items()
                if nama.startswith("teknik ")
            }

        diizinkan = set(peta)

    if not diizinkan:
        return (
            False,
            "Bagian katalog yang diminta tidak ditemukan secara eksplisit pada sumber.",
        )

    if _is_list_question(question):
        items = _extract_answer_items(answer)

        # Jawaban satu butir sering ditulis sebagai prosa, bukan daftar.
        # Nama yang diakui tetap harus muncul eksplisit di katalog.
        if not items:
            jawaban = answer.lower()
            items = [
                nama for nama in sorted(diizinkan)
                if nama in jawaban
            ]

        if not items:
            return (
                False,
                "Jawaban daftar tidak memiliki butir yang dapat diverifikasi.",
            )

        unsupported = [
            item for item in items
            if item.lower() not in diizinkan
        ]

        if unsupported:
            return (
                False,
                "Sebagian butir jawaban tidak ditemukan secara eksplisit "
                "pada bagian katalog yang sesuai.",
            )
    else:
        if not any(nama in answer.lower() for nama in diizinkan):
            return (
                False,
                "Nama yang disebutkan tidak ditemukan secara eksplisit "
                "pada bagian katalog yang sesuai.",
            )

    jumlah = {
        nilai
        for parse in parses
        for nilai in _jumlah_diizinkan(parse, topic, teks)
    }

    valid, reason = _cek_jumlah(answer, topic, jumlah)

    if not valid:
        return False, reason

    if topic == "program studi":
        valid, reason = _cek_akreditasi(answer, peta)

        if not valid:
            return False, reason

    return True, ""


def _butuh_katalog(question):
    """Apakah pertanyaan meminta daftar lengkap dari katalog?"""
    if not _is_list_question(question) or not _topic(question):
        return False

    q = question.lower()

    # "sebutkan satu fakultas" tidak boleh diganti dengan seluruh daftar.
    return not re.search(
        r"\b(satu|salah satu|sebuah|contoh|salah)\b",
        q,
    )


def _ada_butir_didukung(answer, records, topik):
    """Apakah minimal satu butir jawaban benar-benar ada di katalog?

    Dipakai untuk membedakan kesalahan sebagian (misalnya model menambahkan
    satu nama rekaan di antara nama yang benar) dari jawaban yang seluruhnya
    rekaan. Nama tetap diambil dari parser, bukan dari model.
    """
    if not topik:
        return False

    katalog = [
        record for record in records
        if _authoritative_catalog(record, topik)
    ]

    if not katalog:
        return False

    diizinkan = set()

    for record in katalog:
        teks = str(record.get("teks", ""))
        parse = parse_katalog(teks)

        if topik == "fakultas":
            diizinkan.update(
                nama.lower() for nama in parse["fakultas"]
            )
        else:
            diizinkan.update(peta_akreditasi(teks))

    butir = [item.lower() for item in _extract_answer_items(answer)]

    return any(item in diizinkan for item in butir)


def _prioritas_url(url):
    """Urutkan kandidat: katalog resmi lebih awal, berita lebih belakang."""
    alamat = str(url).lower()

    if any(penanda in alamat for penanda in _BERITA_PATH):
        return 1

    if any(penanda in alamat for penanda in (
        "program-pendidikan", "program-studi", "/pendidikan",
        "/akademik", "/fakultas", "/pimpinan", "/rektor",
        "/struktur-organisasi", "/profil", "/fasilitas", "/sarana",
    )):
        return 0

    return 2


def _katalog_answer(answer, records, question, hanya_prodi=True):
    """Susun jawaban katalog dari teks halaman yang benar-benar terbaca.

    Fungsi ini tidak menebak. Semua nama diambil dari parser deterministik
    :mod:`app.katalog`. Secara default pemakaiannya dibatasi pada program studi
    sebagai perbaikan jawaban yang gagal diverifikasi; untuk fakultas, jawaban
    model yang tidak didukung tetap ditolak. Saat ``hanya_prodi=False``,
    fungsi ini juga melengkapi daftar fakultas yang sudah terverifikasi.
    """
    topik = _topic(question)

    if not topik or (hanya_prodi and topik != "program studi"):
        return None

    pertanyaan = question.lower()

    if not _is_list_question(question) and "akreditasi" not in pertanyaan:
        return None

    catalogs = [
        record
        for record in records
        if _authoritative_catalog(record, topik)
    ]

    if not catalogs:
        return None

    parses = [
        parse_katalog(str(record.get("teks", "")))
        for record in catalogs
    ]

    sumber = []
    for record in catalogs:
        if record["url"] not in sumber:
            sumber.append(record["url"])

    if topik == "fakultas":
        nama_fakultas = []
        for parse in parses:
            for nama in parse["fakultas"]:
                if nama not in nama_fakultas:
                    nama_fakultas.append(nama)

        if not nama_fakultas:
            return None

        daftar = "\n".join(
            f"{i}. {nama}"
            for i, nama in enumerate(nama_fakultas, 1)
        )

        return (
            f"Berdasarkan katalog UIR, terdapat "
            f"{len(nama_fakultas)} fakultas:\n\n"
            + daftar + "\n\nSumber:\n"
            + "\n".join("- " + url for url in sumber)
        )

    baris = []
    for parse in parses:
        for item in parse["program_studi"]:
            if not any(
                ada["nama"] == item["nama"] for ada in baris
            ):
                baris.append(item)

    if not baris:
        return None

    if "akreditasi" in pertanyaan:
        rendah = answer.lower()
        dipilih = next(
            (
                item for item in baris
                if item["nama_norm"].lower() in rendah
                or item["nama"].lower() in rendah
            ),
            baris[0],
        )
        isi = (
            f"{dipilih['nama']} memiliki akreditasi "
            f"{dipilih['akreditasi']} sesuai katalog."
        )
        return (
            isi + "\n\nSumber:\n"
            + "\n".join("- " + url for url in sumber)
        )

    if "fakultas teknik" in pertanyaan:
        baris = [
            item for item in baris
            if item["nama_norm"].lower().startswith("teknik ")
        ]

        if not baris:
            return None

        pembuka = (
            "Program studi Fakultas Teknik yang tercantum "
            "dalam katalog UIR:"
        )
    else:
        pembuka = (
            f"Berdasarkan katalog UIR, terdapat "
            f"{len(baris)} program studi:"
        )

    daftar = "\n".join(
        f"{i}. {item['nama']}"
        for i, item in enumerate(baris, 1)
    )

    return (
        pembuka + "\n\n" + daftar + "\n\nSumber:\n"
        + "\n".join("- " + url for url in sumber)
    )


def _satu_butir_katalog(records, topic):
    """Satu butir dari katalog resmi untuk permintaan "satu saja".

    Dipakai hanya bila model menolak memberi butir padahal katalog resmi sudah
    terbaca. Nama tetap diambil dari parser deterministik, bukan dari model.
    """
    if topic not in ("fakultas", "program studi"):
        return None

    for record in records:
        if not _authoritative_catalog(record, topic):
            continue

        parse = parse_katalog(str(record.get("teks", "")))

        if topic == "fakultas":
            butir = parse["fakultas"]
        else:
            butir = [item["nama"] for item in parse["program_studi"]]

        if not butir:
            continue

        return (
            f"Berdasarkan katalog resmi UIR, salah satu {topic} adalah "
            f"{butir[0]}.\n\nSumber:\n- " + str(record.get("url", ""))
        )

    return None


def _jawaban_multi_aspek(records, question, answer=""):
    """Susun jawaban fakultas + program studi dari bagian katalog yang terbaca."""

    topik = set(_topik_pertanyaan(question))

    if not {"fakultas", "program studi"} <= topik:
        return None

    bagian = []

    for nama in ("fakultas", "program studi"):
        potongan = _katalog_answer(
            answer,
            records,
            "daftar " + nama,
            hanya_prodi=False,
        )

        if not potongan:
            continue

        isi = potongan.split("\n\nSumber:", 1)[0].strip()

        if isi:
            bagian.append(isi)

    return "\n\n".join(bagian) if len(bagian) >= 2 else None


def _akreditasi_answer(records, question, batas=8):
    """Ringkasan akreditasi dari bagian katalog, bukan dari berita.

    Fungsi ini hanya dipakai untuk pertanyaan umum. Bila pengguna menyebut
    satu nama program studi, verifikasi per baris tetap mengambil alih.
    """

    if "akreditasi" not in str(question).lower():
        return None

    katalog = [
        record for record in records
        if _authoritative_catalog(record, "program studi")
    ]

    if not katalog:
        return None

    peta = {}
    for record in katalog:
        peta.update(peta_akreditasi(str(record.get("teks", ""))))

    if not peta:
        return None

    rendah = str(question).lower()
    if any(nama.lower() in rendah for nama in peta):
        return None

    items = list(peta.items())[:batas]
    baris = "\n".join(
        "- %s: %s" % (nama, nilai)
        for nama, nilai in items
    )

    isi = (
        "Berdasarkan katalog UIR, informasi akreditasi program studi "
        "meliputi:\n\n" + baris
    )

    if len(peta) > batas:
        isi += (
            "\n\nMasih ada %d program studi lain pada katalog yang sama."
            % (len(peta) - batas)
        )

    sumber = []
    for record in katalog:
        if record.get("url") and record["url"] not in sumber:
            sumber.append(record["url"])

    return (
        isi + "\n\nSumber:\n"
        + "\n".join("- " + url for url in sumber)
    )


def _jawaban_sebagian(records, question, answer=""):
    """Jawab bagian yang terbukti dan sebutkan bagian yang belum ditemukan."""

    multi = _jawaban_multi_aspek(records, question, answer)

    if multi is not None:
        return multi

    topik = _topik_pertanyaan(question)

    if not topik:
        return None

    bagian = []
    belum = []

    for nama in topik:
        potongan = None

        if nama in ("fakultas", "program studi"):
            potongan = _katalog_answer(
                answer,
                records,
                "daftar " + nama,
                hanya_prodi=False,
            )

        elif nama == "sejarah":
            potongan = _profil_answer(records)

        if potongan:
            isi = potongan.split("\n\nSumber:", 1)[0].strip()

            if isi:
                bagian.append(isi)
                continue

        # Untuk topik non-katalog, pakai potongan halaman yang benar-benar
        # mengandung penanda topik tersebut.
        potongan_topik = []

        for record in records:
            if nama not in _topik_record(record):
                continue

            for pot in record.get("bagian") or []:
                if pot not in potongan_topik:
                    potongan_topik.append(pot)

        if potongan_topik:
            bagian.append("\n".join(potongan_topik[:3]))
        else:
            belum.append(_LABEL_TOPIK.get(nama, nama))

    if not bagian:
        return None

    teks = "\n\n".join(bagian)

    if belum:
        teks += (
            "\n\nBelum ditemukan pada halaman resmi yang berhasil dibaca: "
            + ", ".join(belum) + "."
        )

    return teks


def _ada_data_katalog(records, topik):
    """Apakah salah satu halaman memuat butir katalog yang dapat dipakai?"""
    if not topik:
        return False

    for record in records:
        if not _authoritative_catalog(record, topik):
            continue

        teks = str(record.get("teks", ""))
        if topik == "fakultas" and parse_katalog(teks)["fakultas"]:
            return True
        if topik == "program studi" and peta_akreditasi(teks):
            return True

    return False


def _ada_halaman_kanonik(records):
    """Apakah bukti berisi halaman resmi, bukan hanya artikel berita?"""
    for record in records:
        url = str(record.get("url", ""))

        if urllib.parse.urlsplit(url).query:
            continue
        if _adalah_artikel(str(record.get("judul", "")), url):
            continue
        if str(record.get("jenis", "")).lower() == "post":
            continue

        return True

    return False


def _tambah_sumber(answer, urls):
    """Tambahkan URL ke bagian Sumber yang sudah ada, bukan bagian baru."""

    teks = str(answer).rstrip()

    if not urls:
        return teks

    baris = teks.splitlines()
    indeks = None

    for i, nilai in enumerate(baris):
        if re.match(
            r"^sumber(?:\s+berdasarkan bagian)?\s*:",
            nilai.strip(),
            re.I,
        ):
            indeks = i

    if indeks is None:
        return (
            teks + "\n\nSumber:\n"
            + "\n".join("- " + url for url in urls)
        )

    akhir = len(baris)

    for i in range(indeks + 1, len(baris)):
        if re.match(r"^(?:sumber|catatan)\b", baris[i].strip(), re.I):
            akhir = i
            break

    sudah = "\n".join(baris[indeks + 1:akhir])
    tambahan = ["- " + url for url in urls if url not in sudah]

    if not tambahan:
        return teks

    while akhir > indeks + 1 and not baris[akhir - 1].strip():
        akhir -= 1

    baris[akhir:akhir] = tambahan

    return "\n".join(baris).rstrip()


def _rekam_mendukung_jawaban(record, question, isi):
    """Apakah halaman ini benar-benar menyumbang isi jawaban?"""

    topik = _topic(question)
    daftar_topik = set(_topik_pertanyaan(question))
    teks = str(record.get("teks", ""))

    if perencana.pahami_dosen(question)["aktif"]:
        bukti = _profesor_evidence([record], question)
        if not bukti:
            return False
        verifikasi = _klaim_profesor_didukung(isi, [record], question)
        return verifikasi is None or bool(verifikasi[0])

    # Pertanyaan katalog/akreditasi/sejarah harus memakai halaman yang
    # benar-benar memuat data, bukan berita yang kebetulan lewat filter
    # relevansi. Untuk topik lain, final_records sudah melewati filter
    # relevansi per giliran sehingga boleh dipakai sebagai sumber.
    if topik:
        return _ada_data_katalog([record], topik)
    if "akreditasi" in daftar_topik:
        return bool(peta_akreditasi(teks))
    if "sejarah" in daftar_topik:
        return bool(_authoritative_profile(record))

    rendah = str(question).lower()

    if "pimpinan" in daftar_topik and any(
        tanda in rendah for tanda in (
            "siapa", "rektor", "pimpinan", "jabatan", "dekan",
        )
    ):
        if not _record_identitas([record]):
            return False

        verifikasi = _klaim_identitas_didukung(isi, [record], question)
        if verifikasi is not None:
            return bool(verifikasi[0])

        return (
            _prioritas_url_identitas(record.get("url", "")) <= 2
            and bool(_pasangan_peran_nama(teks))
        )

    if "beasiswa" in daftar_topik:
        return _authoritative_beasiswa(record)

    if "pmb" in daftar_topik:
        return bool(_record_pmb([record]))

    if perencana.pahami_lokasi(question)["aktif"]:
        return _authoritative_lokasi(record) and bool(
            _ekstrak_alamat(teks) or _ekstrak_kontak(teks)
        )

    return True


def _sumber_dipakai(answer, records, question):
    """Kumpulkan URL halaman yang benar-benar dipakai, bukan sekadar dibaca."""

    isi = answer.split("\n\nSumber", 1)[0].strip()
    keluaran = []

    for record in records:
        url = str(record.get("url", ""))

        if not url or url in keluaran:
            continue

        if url in isi or _rekam_mendukung_jawaban(record, question, isi):
            keluaran.append(url)

    return keluaran


def _ganti_sumber(answer, urls):
    """Buang bagian Sumber lama, lalu tulis ulang hanya dari ``urls``."""

    baris = str(answer).splitlines()
    indeks = None

    for i, nilai in enumerate(baris):
        if re.match(
            r"^sumber(?:\s+berdasarkan bagian)?\s*:",
            nilai.strip(),
            re.I,
        ):
            indeks = i
            break

    if indeks is not None:
        baris = baris[:indeks]

    teks = "\n".join(baris).rstrip()

    if not urls:
        return teks

    return (
        teks + "\n\nSumber:\n"
        + "\n".join("- " + url for url in urls)
    )


def _sumber_per_bagian(answer, records, question):
    """Ganti bagian Sumber dengan URL yang benar-benar dipakai per topik."""

    if not records:
        return answer

    baris_isi = []

    for baris in str(answer).splitlines():
        if re.match(r"^sumber(?:\s+berdasarkan bagian)?\s*:", baris, re.I):
            break
        baris_isi.append(baris)

    isi = "\n".join(baris_isi).rstrip()
    topik = _topik_pertanyaan(question)
    daftar_sumber = []

    if len(topik) > 1:
        for nama in topik:
            urls = []

            for record in records:
                url = record.get("url", "")
                if (
                    nama in _topik_record(record)
                    and url
                    and url not in urls
                ):
                    urls.append(url)

            if urls:
                daftar_sumber.append(
                    "- %s: %s" % (
                        _LABEL_TOPIK.get(nama, nama),
                        ", ".join(urls),
                    )
                )

    if not daftar_sumber:
        for record in records:
            judul = str(record.get("judul", "")).strip()
            label = judul[:100] if judul else "halaman UIR"
            daftar_sumber.append(
                "- %s — %s" % (label, record.get("url", ""))
            )

    return (
        isi
        + "\n\nSumber berdasarkan bagian:\n"
        + "\n".join(daftar_sumber)
    )


class Orkestrator:
    def __init__(
        self,
        client=None,
        max_langkah=MAX_LANGKAH,
        verifikasi_wajib=None,
        jalur_cepat=None,
    ):
        self._config_error = None

        if client is None:
            try:
                client = buat_client()
            except (KonfigurasiTidakValid, ModelError) as exc:
                self._config_error = exc
                client = None

        produksi = bool(
            getattr(
                client,
                "mode_produksi",
                isinstance(client, OllamaClient),
            )
        )

        if verifikasi_wajib is None:
            verifikasi_wajib = produksi

        self.client = client
        self.verifikasi_wajib = bool(verifikasi_wajib)

        # Jalur cepat hanya dipakai pada mode produksi (klien nyata, baik
        # DeepSeek API maupun Ollama rollback). Unit test dengan klien tiruan
        # tetap menguji alur agen penuh.
        if jalur_cepat is None:
            jalur_cepat = produksi
        self.jalur_cepat = bool(jalur_cepat and self.verifikasi_wajib)

        self.max_langkah = max(
            1,
            min(int(max_langkah), MAX_LANGKAH),
        )

        self.riwayat = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            }
        ]

        self._reset_turn()

    def _reset_turn(self):
        self.sumber = []
        self.terbaca = []

        # self.bukti sengaja dipertahankan lintas giliran: ini memori sesi
        # berupa halaman yang benar-benar berhasil dibaca. Yang direset hanya
        # data khusus giliran (URL, kandidat, dan jumlah langkah).
        if not hasattr(self, "bukti"):
            self.bukti = []

        self.kandidat = []
        self.info_kandidat = {}
        self.turn_url = []
        self.url_diizinkan = set()
        self.status_sumber = {}
        self.pencarian_terakhir = None

        self.langkah = 0
        self.pertanyaan = ""
        self._jalur_cepat_aktif = False
        self._awal_giliran = len(getattr(self, "riwayat", [])) or 1

    def _catat_status(self, url, status, **detail):
        """Catat status sumber: retrieved -> read -> evidence -> used -> cited."""

        if not url or status not in _STATUS_SUMBER:
            return

        entri = self.status_sumber.setdefault(url, {
            "retrieved": False,
            "read": False,
            "evidence": False,
            "used": False,
            "cited": False,
            "judul": "",
            "jenis": "",
            "tanggal": "",
            "http": None,
            "panjang_teks": 0,
            "skor": 0.0,
            "alasan": [],
        })

        entri[status] = True

        for kunci, nilai in detail.items():
            if kunci == "alasan":
                if nilai and nilai not in entri["alasan"]:
                    entri["alasan"].append(str(nilai))
            elif kunci in entri:
                entri[kunci] = nilai

    def _catat_alasan(self, url, alasan):
        """Tambahkan alasan terima/tolak tanpa mengubah status sumber."""

        if not url or not alasan:
            return

        entri = self.status_sumber.setdefault(url, {
            "retrieved": False,
            "read": False,
            "evidence": False,
            "used": False,
            "cited": False,
            "judul": "",
            "jenis": "",
            "tanggal": "",
            "http": None,
            "panjang_teks": 0,
            "skor": 0.0,
            "alasan": [],
        })

        if alasan not in entri["alasan"]:
            entri["alasan"].append(str(alasan))

    def _tandai_status_akhir(self, answer, dipakai):
        """Tandai status used/cited hanya untuk sumber pendukung jawaban."""

        isi = str(answer).split("\n\nSumber", 1)[0].strip().lower()

        for url, entri in self.status_sumber.items():
            if not entri.get("evidence"):
                continue

            rekam = next(
                (
                    record for record in self.bukti
                    if str(record.get("url", "")) == url
                ),
                None,
            )
            label = list((rekam or {}).get("digunakan_untuk") or [])
            label_dipakai = any(
                str(item).lower() in isi for item in label
            )

            if url in dipakai or label_dipakai:
                entri["used"] = True
                self._catat_alasan(
                    url,
                    "dipakai untuk menyusun bagian jawaban yang didukung bukti",
                )

            if url in dipakai:
                entri["cited"] = True
                self._catat_alasan(
                    url,
                    "dikutip pada bagian Sumber karena mendukung klaim",
                )
            elif url in isi:
                self._catat_alasan(
                    url,
                    "URL disebut pada jawaban tetapi tidak masuk daftar sitasi "
                    "karena tidak mendukung klaim akhir",
                )

    def tanya(self, pertanyaan):
        self._reset_turn()

        if not isinstance(pertanyaan, str) or not pertanyaan.strip():
            return _hasil(
                False,
                "validasi_gagal",
                pesan="Pertanyaan harus berupa teks tidak kosong.",
            )

        self.pertanyaan = pertanyaan.strip()

        if self._config_error:
            return self._error(self._config_error)

        for match in _URL_RE.findall(self.pertanyaan):
            address = _validasi_url(match.rstrip(".,;!?)}>"))

            if address:
                self.turn_url.append(address)
                self.url_diizinkan.add(address)

        # Batas giliran dipakai supaya payload tool dari percakapan lama tidak
        # ikut dikirim ke model. Riwayat asli tetap lengkap di self.riwayat.
        self._awal_giliran = len(self.riwayat)

        self._append({
            "role": "user",
            "content": self.pertanyaan,
        })

        telemetri.tandai("intent_mulai")

        # Pertanyaan yang hanya merujuk ke jawaban sebelumnya tanpa topik
        # tidak boleh ditebak. Minta klarifikasi singkat tanpa memanggil model.
        if (
            self.verifikasi_wajib
            and _adalah_pertanyaan_ambigu(self.pertanyaan)
        ):
            return self._minta_klarifikasi()

        # Pertanyaan yang tidak memuat penanda UIR maupun topik akademik
        # ditolak lebih awal. Ini mencegah model menjawab topik umum dengan
        # mengaitkannya ke halaman UIR yang kebetulan terbaca pada sesi.
        if (
            self.verifikasi_wajib
            and not self._ada_jawaban_terverifikasi_sebelumnya()
            and _adalah_pertanyaan_di_luar_cakupan(self.pertanyaan)
        ):
            return self._reject(
                "Pertanyaan di luar cakupan informasi publik UIR.",
                status="bukti_tidak_cukup",
            )

        # Model cenderung membaca berita satu per sampai batas langkah habis.
        # Untuk topik yang memiliki halaman indeks resmi, halaman kanonik
        # dibaca lebih dulu (maksimal dua halaman) agar bukti kuat tersedia
        # walaupun model kemudian memilih kandidat yang kurang tepat.
        if self._perlu_bootstrap_evidence():
            self._ensure_evidence(
                query=self.pertanyaan,
                paksa_cari=True,
                hanya_kanonik=True,
            )

        telemetri.tandai("intent_selesai")

        # Jalur cepat: bila parser bukti sudah menghasilkan jawaban yang
        # konsisten, lewati pemanggilan model. _finalize tetap menjalankan
        # verifikasi klaim dan sitasi seperti biasa.
        jawaban_awal = self._jawaban_awal_terverifikasi()

        if jawaban_awal is not None:
            self._jalur_cepat_aktif = True
            telemetri.tambah("jalur_cepat")
            telemetri.tandai("jalur_cepat")
            return self._finalize(
                {"role": "assistant", "content": jawaban_awal},
                "berhasil",
            )

        telemetri.tandai("pemilihan_tool_mulai")

        try:
            return self._cycle()

        except (OllamaError, ModelError) as exc:
            LOGGER.warning("Model error: %s", exc)
            return self._error(exc)

        except Exception:
            LOGGER.exception("Kesalahan internal orchestrator")
            return _hasil(
                False,
                "kesalahan_internal",
                sumber=self.sumber,
                terbaca=self.terbaca,
                langkah=self.langkah,
                pesan="Terjadi kesalahan internal; periksa log.",
                status_sumber=self.status_sumber,
            )

    def _messages_untuk_model(self):
        """Susun pesan untuk model tanpa payload tool dari giliran lama.

        Riwayat percakapan (pertanyaan dan jawaban) tetap dikirim supaya
        pertanyaan lanjutan masih dipahami. Pesan tool lama dan assistant
        lama yang memuat tool_calls dibuang karena isinya sudah tidak relevan
        dan hanya memperbesar prompt.
        """

        if not self.riwayat:
            return []

        awal = max(1, int(getattr(self, "_awal_giliran", 1)))
        pesan = [self.riwayat[0]]

        for indeks, item in enumerate(self.riwayat[1:], start=1):
            if indeks < awal:
                if (
                    item.get("role") in ("user", "assistant")
                    and not item.get("tool_calls")
                ):
                    pesan.append(item)
                continue
            pesan.append(item)

        return pesan

    def _cycle(self):
        while True:
            ke_model = telemetri.tambah(
                "panggilan_model", kembalikan=True
            )

            with telemetri.ukur(
                "model", panggilan_ke=ke_model, fase="utama"
            ):
                response = self.client.chat(
                    messages=self._messages_untuk_model(),
                    tools=TOOLS,
                )

            message = response.get("message")
            telemetri.tandai("pemilihan_tool_selesai")

            if not isinstance(message, dict):
                raise ResponsTidakValid("Pesan model tidak valid.")

            calls = message.get("tool_calls") or []

            if not isinstance(calls, list):
                raise ResponsTidakValid("tool_calls bukan list.")

            if not calls:
                return self._finalize(message, "berhasil")

            self._append(self._assistant_message(message))

            reached = False

            for call in calls:
                if self.langkah >= self.max_langkah:
                    reached = True

                    self._append(
                        self._tool_message(
                            _parse_call(call)[0],
                            {
                                "ok": False,
                                "status": "batas_langkah",
                                "pesan": (
                                    f"Batas maksimal {self.max_langkah} "
                                    "langkah tercapai."
                                ),
                            },
                        )
                    )
                    continue

                self.langkah += 1

                name, args = _parse_call(call)

                with telemetri.ukur(
                    "tool",
                    tool=name,
                    alat_ke=telemetri.nomor_tool(name),
                    model_ke=ke_model,
                ):
                    result = self._run_validated(name, args)

                telemetri.catat_tool(name, result.get("status", ""))

                if name == "baca_cache":
                    telemetri.catat_cache(result.get("status", ""))

                self._record(name, result)
                self._append(self._tool_message(name, result))

            if reached:
                ke_model = telemetri.tambah(
                    "panggilan_model", kembalikan=True
                )

                with telemetri.ukur(
                    "model", panggilan_ke=ke_model, fase="batas_langkah"
                ):
                    final_response = self.client.chat(
                        messages=self._messages_untuk_model(),
                        tools=None,
                    )

                final = final_response.get("message", {})

                return self._finalize(
                    final if isinstance(final, dict) else {},
                    "batas_langkah",
                    "Batas maksimal pemanggilan tool tercapai.",
                )

    def _run_validated(self, name, args):
        if name not in _ARG_KEYS:
            return {
                "ok": False,
                "status": "tool_tidak_dikenal",
                "pesan": "Tool tidak dikenal.",
            }

        if not _valid_args(name, args):
            return {
                "ok": False,
                "status": "argumen_tidak_valid",
                "pesan": "Argumen tool tidak sesuai.",
            }

        if name in ("baca_cache", "ambil_halaman"):
            address = _validasi_url(args["url"])

            if not address:
                return {
                    "ok": False,
                    "status": "url_tidak_valid",
                    "pesan": "URL tidak diizinkan.",
                }

            if (
                self.verifikasi_wajib
                and address not in self.url_diizinkan
            ):
                return {
                    "ok": False,
                    "status": "url_tidak_diizinkan",
                    "pesan": (
                        "URL tidak berasal dari pencarian "
                        "atau input pengguna."
                    ),
                }

            # Mode non-ketat tidak membatasi URL ke hasil pencarian, tetapi
            # URL yang lolos validasi tetap harus tercatat sebagai provenance
            # agar _record() dapat menyimpannya sebagai bukti yang terbaca.
            if not self.verifikasi_wajib:
                self.url_diizinkan.add(address)

            args = {"url": address}

        try:
            if name == "cari_uir":
                with telemetri.ukur("cari_uir"):
                    result = cari_uir(**args)

                self.pencarian_terakhir = result
                _catat_pencarian(result)

                if result.get("ok"):
                    self._record_search(result)

                return result

            if name == "baca_cache":
                with telemetri.ukur("baca_cache"):
                    return baca_cache(**args)

            with telemetri.ukur("ambil_halaman"):
                result = ambil_halaman(**args)

            if result.get("ok") and result.get("status") == "berhasil":
                result = dict(result)
                with telemetri.ukur("simpan_cache"):
                    result["cache"] = simpan_cache(dict(result))

            return result

        except Exception:
            LOGGER.exception("Tool %s gagal", name)

            return {
                "ok": False,
                "status": "tool_error",
                "pesan": "Tool gagal dijalankan.",
            }

    def _record(self, name, result):
        if name not in ("baca_cache", "ambil_halaman"):
            return

        if not isinstance(result, dict):
            return

        if (
            result.get("ok") is not True
            or result.get("status") not in _STATUS_BACA
        ):
            return

        data = _extract_data(result)

        url = _validasi_url(
            data.get("url") or result.get("url")
        )

        text = data.get("teks")

        if (
            not url
            or not isinstance(text, str)
            or not text.strip()
            or url not in self.url_diizinkan
        ):
            return

        info = self.info_kandidat.get(url, {})
        record = {
            "url": url,
            "judul": data.get("judul", "") or info.get("judul", ""),
            "teks": text,
            "sumber": name,
            "waktu": data.get("waktu", "") or result.get("waktu", ""),
            "waktu_cache": result.get("waktu_cache", ""),
            "panjang": data.get("panjang", len(text)),
            "sumber_kandidat": info.get("sumber", ""),
            "jenis": info.get("jenis", ""),
            "subkueri": list(info.get("subkueri", [])),
            "pertanyaan": self.pertanyaan,
            "bagian": _potongan_relevan(text, self.pertanyaan),
        }

        self._catat_status(
            url,
            "read",
            judul=record["judul"],
            jenis=record.get("jenis", ""),
            http=200,
            panjang_teks=len(text),
            alasan="isi halaman berhasil diekstrak dari " + name,
        )

        self.bukti = _gabungkan_bukti(self.bukti, record)

        self._add_unique(self.terbaca, url)
        self._add_unique(self.sumber, url)

    def _urutan_kandidat(self, url):
        """Utamakan kandidat relevan dan beragam, bukan satu halaman universal."""

        info = self.info_kandidat.get(url, {})
        profil = (
            _adalah_profil_pertanyaan(self.pertanyaan)
            or _adalah_pertanyaan_tahun_berdiri(self.pertanyaan)
        )
        daftar_topik = _topik_pertanyaan(self.pertanyaan)
        prioritas = (
            _prioritas_url_profil(url) if profil
            else _prioritas_url_topik(url, daftar_topik)
        )

        judul = info.get("judul", "")
        cuplikan = info.get("cuplikan", "")
        skor = _skor(self.pertanyaan, judul, cuplikan)

        info_lokasi = perencana.pahami_lokasi(self.pertanyaan)
        if info_lokasi["aktif"]:
            label = (judul + " " + cuplikan + " " + url).lower()
            if _prioritas_url_lokasi(url) <= 1:
                skor += 1.5
            elif _prioritas_url_lokasi(url) >= 3 or any(
                penanda in label for penanda in (
                    "cbt", "jadwal", "pmb", "pendaftaran", "ujian",
                )
            ):
                skor -= 1.5

        info_dosen = perencana.pahami_dosen(self.pertanyaan)
        if info_dosen["aktif"] and info_dosen["teknik"]:
            label = (judul + " " + cuplikan + " " + url).lower()
            if any(penanda in label for penanda in (
                "fakultas teknik", "fakultas-teknik", "teknik sipil",
                "teknik-sipil", "teknik perminyakan", "prodi teknik",
            )):
                skor += 1.5
            else:
                skor -= 0.5

        topik_q = set(_topik_pertanyaan(self.pertanyaan))
        topik_k = _topik_teks(judul + " " + cuplikan + " " + url)

        if topik_q & topik_k:
            skor += 0.5
        if info.get("terverifikasi_sitemap"):
            skor += 0.1

        return (prioritas, -skor, url)

    def _kandidat_relevan(self, url):
        """Jangan membuang langkah untuk kandidat yang jelas tidak relevan."""

        if url in self.turn_url:
            return True

        info = self.info_kandidat.get(url)

        if not info:
            return True

        teks = " ".join((
            info.get("judul", ""),
            info.get("cuplikan", ""),
            url,
        ))
        topik_q = set(_topik_pertanyaan(self.pertanyaan))
        topik_k = _topik_teks(teks)

        if perencana.pahami_lokasi(self.pertanyaan)["aktif"]:
            if _prioritas_url_lokasi(url) <= 1:
                return True
            if _prioritas_url_lokasi(url) >= 3 or any(
                penanda in teks.lower() for penanda in (
                    "cbt", "jadwal", "pmb", "pendaftaran", "ujian",
                )
            ):
                return False

        if topik_q & topik_k:
            return True
        if perencana.pahami_dosen(self.pertanyaan)["aktif"] and any(
            penanda in teks.lower() for penanda in (
                "profesor", "guru besar", "guru-besar", "dosen",
                "fakultas teknik", "profil dosen",
            )
        ):
            return True
        if _skor(self.pertanyaan, info.get("judul", ""), teks) >= 0.15:
            return True

        # Halaman indeks resmi (misalnya /program-pendidikan/) sering memiliki
        # judul umum dan cuplikan yang belum menyebut kata topik. Jangan buang
        # halaman tersebut hanya karena metadata pencarian mismet.
        if (
            _url_kanonik_topik(url, _topik_pertanyaan(self.pertanyaan))
            and "elementor_library" not in url.lower()
            and not _adalah_artikel(info.get("judul", ""), url)
            and (
                _butuh_katalog(self.pertanyaan)
                or _topic(self.pertanyaan)
                or _topik_pertanyaan(self.pertanyaan)
            )
        ):
            return True

        return _authoritative_catalog(
            {"url": url, "judul": info.get("judul", ""), "teks": info.get("cuplikan", ""), "sumber": "baca_cache"},
            _topic(self.pertanyaan) or "fakultas",
        )

    def _perlu_bootstrap_evidence(self):
        """Apakah topik ini perlu halaman indeks resmi dibaca lebih awal?"""

        if not self.verifikasi_wajib or self.turn_url:
            return False

        # Sejarah/tahun berdiri sudah punya jalur repair khusus di finalisasi.
        if (
            _adalah_profil_pertanyaan(self.pertanyaan)
            or _adalah_pertanyaan_tahun_berdiri(self.pertanyaan)
        ):
            return False

        if _butuh_katalog(self.pertanyaan):
            return True

        # Pertanyaan jumlah program studi bukan permintaan daftar, jadi tidak
        # terkena _butuh_katalog. Tetap bootstrapped agar halaman indeks resmi
        # terbaca sebelum model dipanggil.
        if (
            re.search(r"\bberapa\b", self.pertanyaan.lower())
            and "program studi" in set(
                _topik_pertanyaan(self.pertanyaan)
            )
        ):
            return True

        # Sama seperti jumlah program studi, pertanyaan jumlah fakultas perlu
        # halaman katalog kanonik dibaca lebih dulu. Tanpa ini model memilih
        # berita fakultas yang kebetulan cocok, lalu anggaran langkah habis
        # sebelum halaman "Program Pendidikan" sempat dibaca.
        if (
            re.search(r"\bberapa\b", self.pertanyaan.lower())
            and "fakultas" in set(_topik_pertanyaan(self.pertanyaan))
            and not _unit_fakultas(self.pertanyaan)
        ):
            return True

        daftar_topik = set(_topik_pertanyaan(self.pertanyaan))

        # Profesor/Guru Besar hampir selalu memerlukan halaman profil resmi.
        # Bootstrap memastikan pencarian tetap terjadi walaupun model langsung
        # menjawab tanpa memanggil tool.
        if "dosen" in daftar_topik:
            return True

        # Lokasi/alamat kampus harus dibaca dari halaman kontak/profil resmi
        # sebelum model menjawab; berita CBT bukan alamat kampus.
        if "lokasi" in daftar_topik:
            return True

        # Beasiswa hanya perlu bootstrap bila benar-benar meminta daftar/status.
        # "Info beasiswa UIR" sengaja tetap memakai alur lama.
        if "beasiswa" in daftar_topik and (
            _adalah_pertanyaan_luas(self.pertanyaan)
            or "status" in self.pertanyaan.lower()
        ):
            return True

        # PMB hanya di-bootstraps bila menjadi topik utama. Pertanyaan
        # "beasiswa untuk mahasiswa baru" harus tetap memakai bukti beasiswa
        # yang sudah ada, bukan memicu pencarian/pembacaan ulang.
        if "pmb" in daftar_topik and "beasiswa" not in daftar_topik:
            return True

        return bool(
            daftar_topik
            & {
                "akreditasi", "pimpinan", "fasilitas",
                "akademik", "biaya", "kemahasiswaan",
            }
        )

    def _baca_katalog_jumlah(self):
        """Baca satu halaman indeks katalog resmi untuk pertanyaan jumlah.

        URL diambil hanya dari kandidat hasil pencarian yang sudah melewati
        validasi. Tidak ada alamat baru yang dibuat atau ditebak.
        """
        if self.langkah >= self.max_langkah:
            return False

        kandidat = [
            url for url in self.kandidat
            if _validasi_url(url)
            and _prioritas_url(url) == 0
            and "program-pendidikan"
            in urllib.parse.urlparse(url).path.lower()
            and "elementor_library" not in url.lower()
        ]

        berhasil = False
        for url in kandidat[:1]:
            self.langkah += 1

            with telemetri.ukur(
                "tool",
                tool="baca_cache",
                alat_ke=telemetri.nomor_tool("baca_cache"),
            ):
                with telemetri.ukur("baca_cache"):
                    cache = baca_cache(url=url)

            telemetri.catat_tool("baca_cache", cache.get("status", ""))
            telemetri.catat_cache(cache.get("status", ""))

            self._append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": "baca_cache",
                        "arguments": {"url": url},
                    }
                }],
            })
            self._append(self._tool_message("baca_cache", cache))

            if cache.get("ok") and cache.get("status") == "ditemukan":
                self._record("baca_cache", cache)
                berhasil = True

        return berhasil

    def _ensure_evidence(
        self, query=None, paksa_cari=False, hanya_kanonik=False
    ):
        with telemetri.ukur("ensure_evidence"):
            return self._ensure_evidence_impl(
                query, paksa_cari, hanya_kanonik
            )

    def _ensure_evidence_impl(
        self, query=None, paksa_cari=False, hanya_kanonik=False
    ):
        kata_kunci = (query or self.pertanyaan)[:MAX_QUERY]
        urls = list(dict.fromkeys(self.turn_url + self.kandidat))

        # Pemanggil dapat meminta pencarian baru dengan query kanonik bila
        # kandidat lama ternyata tidak relevan (misalnya berita pendirian
        # program studi untuk pertanyaan tahun berdiri universitas).
        if (not urls or paksa_cari) and self.langkah < self.max_langkah:
            self.langkah += 1

            with telemetri.ukur(
                "tool",
                tool="cari_uir",
                alat_ke=telemetri.nomor_tool("cari_uir"),
            ):
                with telemetri.ukur("cari_uir"):
                    search = cari_uir(query=kata_kunci)

                self.pencarian_terakhir = search
                _catat_pencarian(search)

            telemetri.catat_tool(
                "cari_uir", search.get("status", "")
            )
            self._record_search(search)
            urls = list(self.kandidat)

            self._append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": "cari_uir",
                        "arguments": {"query": kata_kunci},
                    }
                }],
            })

            self._append(
                self._tool_message("cari_uir", search)
            )

        if hanya_kanonik:
            # Bootstrap memakai anggaran langkah yang sangat terbatas. Hanya
            # halaman indeks resmi (prioritas URL 0) yang dibaca, maksimal dua
            # halaman. Berita tidak boleh menyita langkah sebelum model jalan.
            urls = [
                url for url in urls
                if _url_kanonik_topik(
                    url, _topik_pertanyaan(self.pertanyaan)
                )
                and "elementor_library" not in url.lower()
                and not _adalah_artikel(
                    self.info_kandidat.get(url, {}).get("judul", ""),
                    url,
                )
            ][:2]

        # Halaman katalog resmi dibaca lebih dulu daripada berita, tetapi
        # semua URL tetap berasal dari pencarian atau input pengguna. Untuk
        # pertanyaan profil/sejarah, halaman /profil/ resmi didahulukan agar
        # bukti tahun berdiri tidak kalah oleh berita yang kebetulan cocok.
        # Bila tidak ada kandidat yang jelas relevan dari judul/cuplikan,
        # tetap baca kandidat terbaik. Metadata pencarian bukan bukti, jadi
        # penilaian akhir tetap dilakukan dari isi halaman.
        relevan = {
            url for url in urls
            if self._kandidat_relevan(url)
        }

        if not relevan:
            relevan = set(urls)

        for url in sorted(urls, key=self._urutan_kandidat):
            if self.langkah >= self.max_langkah:
                break

            if url not in relevan:
                continue

            self.langkah += 1

            with telemetri.ukur(
                "tool",
                tool="baca_cache",
                alat_ke=telemetri.nomor_tool("baca_cache"),
            ):
                with telemetri.ukur("baca_cache"):
                    cache = baca_cache(url=url)

            telemetri.catat_tool(
                "baca_cache", cache.get("status", "")
            )
            telemetri.catat_cache(cache.get("status", ""))

            self._append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": "baca_cache",
                        "arguments": {"url": url},
                    }
                }],
            })

            self._append(
                self._tool_message("baca_cache", cache)
            )

            if cache.get("ok") and cache.get("status") == "ditemukan":
                self._record("baca_cache", cache)

            elif (
                cache.get("status") in _STATUS_CACHE_MISS
                and self.langkah < self.max_langkah
            ):
                self.langkah += 1

                with telemetri.ukur(
                    "tool",
                    tool="ambil_halaman",
                    alat_ke=telemetri.nomor_tool("ambil_halaman"),
                ):
                    with telemetri.ukur("ambil_halaman"):
                        page = ambil_halaman(url=url)

                telemetri.catat_tool(
                    "ambil_halaman", page.get("status", "")
                )

                if page.get("ok") and page.get("status") == "berhasil":
                    page = dict(page)
                    with telemetri.ukur("simpan_cache"):
                        page["cache"] = simpan_cache(dict(page))

                self._append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "function": {
                            "name": "ambil_halaman",
                            "arguments": {"url": url},
                        }
                    }],
                })

                self._append(
                    self._tool_message("ambil_halaman", page)
                )

                self._record("ambil_halaman", page)

            if self._cukup_bukti():
                return True

        return self._cukup_bukti()

    def _cukup_bukti(self):
        with telemetri.ukur("cukup_bukti"):
            return self._cukup_bukti_impl()

    def _cukup_bukti_impl(self):
        """Tentukan kapan agen boleh berhenti membaca halaman.

        Pertanyaan sederhana cukup dengan satu halaman resmi yang relevan.
        Pertanyaan luas/daftar/multi-aspek berusaha memakai sekurang-kurangnya
        dua halaman resmi independen bila tersedia, tetapi tetap fail-closed:
        satu sumber kanonik yang benar masih lebih baik daripada tidak ada
        bukti sama sekali.
        """
        records = self._relevant_records()

        if not records:
            return False

        # Lokasi kampus hanya cukup bila alamat benar-benar terbaca pada
        # halaman kontak/profil resmi. Pemeriksaan ini sengaja sebelum klausul
        # anggaran habis agar artikel CBT tidak dianggap sebagai bukti alamat.
        if perencana.pahami_lokasi(self.pertanyaan)["aktif"]:
            return any(
                _authoritative_lokasi(record)
                and bool(_ekstrak_alamat(record.get("teks", "")))
                for record in records
            )

        # Bila anggaran habis, tidak ada gunanya menahan jawaban hanya karena
        # halaman ideal belum ditemukan. Jawaban tetap dibatasi bukti yang ada.
        if self.langkah >= self.max_langkah:
            return True

        rendah = self.pertanyaan.lower()
        daftar_topik = set(_topik_pertanyaan(self.pertanyaan))

        if perencana.pahami_dosen(self.pertanyaan)["aktif"]:
            if not _profesor_answer(records, self.pertanyaan):
                return False

        if "pmb" in daftar_topik and not _record_pmb(records):
            return False

        if "pimpinan" in daftar_topik and (
            "rektor" in rendah or "siapa" in rendah or "pimpinan" in rendah
        ):
            sah = _record_identitas(records)
            if "rektor" in rendah and "wakil rektor" not in rendah:
                siap = any(
                    _nama_peran(str(record.get("teks", "")), "rektor")
                    for record in sah
                )
            else:
                siap = any(
                    _pasangan_peran_nama(str(record.get("teks", "")))
                    for record in sah
                )

            if not siap:
                return False

        if (
            "beasiswa" in daftar_topik
            and _adalah_pertanyaan_luas(self.pertanyaan)
            and len({record.get("url") for record in records}) < 2
        ):
            return False

        if _butuh_katalog(self.pertanyaan):
            topik = _topic(self.pertanyaan)

            if not any(
                _authoritative_catalog(record, topik)
                for record in records
            ):
                return False

            # Katalog sudah terbaca, tetapi pertanyaan luas tetap boleh
            # mencari pendamping bila masih ada langkah dan halaman lain.

        # Beberapa topik memiliki halaman kanonik tunggal, misalnya sejarah
        # universitas pada /profil/. Satu sumber resmi yang jelas tidak perlu
        # dipaksa menjadi dua hanya demi jumlah.
        topik = _topik_pertanyaan(self.pertanyaan)
        if (
            len(topik) == 1
            and topik[0] == "sejarah"
            and any(_authoritative_profile(record) for record in records)
        ):
            return True

        if (
            len(topik) > 1
            and self.langkah < self.max_langkah
            and len({record["url"] for record in records}) < 2
        ):
            return False

        if (
            _adalah_pertanyaan_luas(self.pertanyaan)
            and self.langkah < self.max_langkah
            and len({record["url"] for record in records}) < 2
        ):
            return False

        return True

    def _pertanyaan_sebelumnya_bercakupan(self):
        """Pertanyaan UIR terakhir pada sesi ini, atau string kosong."""

        if not _adalah_pertanyaan_lanjutan(self.pertanyaan):
            return ""

        batas = max(
            1,
            int(getattr(self, "_awal_giliran", len(self.riwayat))),
        )

        for item in reversed(self.riwayat[:batas]):
            if item.get("role") != "user":
                continue

            teks = str(item.get("content", "")).strip()

            if teks and not _adalah_pertanyaan_di_luar_cakupan(teks):
                return teks

        return ""

    def _pertanyaan_rujukan(self):
        """Pertanyaan yang dipakai menilai bukti pada giliran lanjutan.

        Pertanyaan lanjutan ("sebutkan jabatannya saja") biasanya tidak
        memuat kata kunci topik. Tanpa rujukan ini, halaman resmi dari giliran
        sebelumnya beroleh skor relevansi 0 dan dibuang, sehingga jawaban
        ditolak meskipun buktinya sah. Topik tetap berasal dari sesi yang sama.
        """

        return self._pertanyaan_sebelumnya_bercakupan() or self.pertanyaan

    def _ada_jawaban_terverifikasi_sebelumnya(self):
        """Apakah sesi ini punya jawaban UIR sebelumnya yang bisa dirujuk?

        Pertanyaan lanjutan seperti "Dari jawaban sebelumnya, sebutkan
        jabatannya saja." sengaja tidak mengulang kata UIR atau topiknya.
        Guard luar-cakupan hanya boleh dikendurkan bila riwayat **sesi ini**
        memuat pertanyaan bercakupan UIR dan jawaban asisten yang bukan
        penolakan/klarifikasi. Setiap sesi memakai Orkestrator terpisah,
        sehingga riwayat sesi lain tidak pernah ikut dinilai.
        """

        if not self._pertanyaan_sebelumnya_bercakupan():
            return False

        batas = max(
            1,
            int(getattr(self, "_awal_giliran", len(self.riwayat))),
        )

        for item in self.riwayat[:batas]:
            if (
                item.get("role") != "assistant"
                or item.get("tool_calls")
            ):
                continue

            isi = str(item.get("content", "")).strip()

            if not isi:
                continue

            # Penolakan dan permintaan klarifikasi bukan dasar yang sah untuk
            # menjawab pertanyaan lanjutan.
            if (
                isi.startswith("Maaf,")
                or isi.startswith("Maksud Anda")
                or "di luar cakupan" in isi
                or "belum dapat" in isi
            ):
                continue

            return True

        return False

    def _jalur_cepat_diizinkan(self):
        """Apakah giliran ini boleh melewati pemanggilan model?"""

        return bool(
            self.jalur_cepat
            and self.verifikasi_wajib
            and self.client is not None
            and not _adalah_pertanyaan_lanjutan(self.pertanyaan)
            and not _adalah_pertanyaan_ambigu(self.pertanyaan)
        )

    def _jawaban_awal_terverifikasi(self):
        """Susun kandidat jawaban dari bukti tanpa memanggil model.

        Kandidat tetap dikirim melalui :meth:`_finalize`, sehingga verifikasi
        klaim, sitasi, dan fail-closed TIDAK dilewati. Fungsi ini hanya
        menghemat pemanggilan model bila jawaban memang sudah dapat dirakit
        oleh parser dari halaman resmi yang berhasil dibaca.
        """

        if not self._jalur_cepat_diizinkan():
            return None

        records = self._relevant_records()
        if not records:
            return None

        pertanyaan = self.pertanyaan
        rendah = pertanyaan.lower()
        daftar_topik = set(_topik_pertanyaan(pertanyaan))
        dosen = perencana.pahami_dosen(pertanyaan)
        lokasi = perencana.pahami_lokasi(pertanyaan)

        if dosen["aktif"]:
            tersusun = _profesor_answer(records, pertanyaan)
            if not tersusun:
                return None
            hasil = _klaim_profesor_didukung(
                tersusun, records, pertanyaan
            )
            if hasil is None:
                return None
            valid, _ = hasil
            return tersusun if valid else None

        if lokasi["aktif"]:
            tersusun = _lokasi_answer(records, pertanyaan)
            if not tersusun:
                return None
            hasil = _klaim_lokasi_didukung(
                tersusun, records, pertanyaan
            )
            if hasil is None:
                return None
            valid, _ = hasil
            return tersusun if valid else None

        if (
            "program studi" in daftar_topik
            and re.search(r"\bberapa\b", rendah)
        ):
            tersusun = _jumlah_program_studi_answer(records, pertanyaan)

            if tersusun:
                return tersusun

            # Bootstrap kanonik kadang berhenti sebelum halaman yang memuat
            # kalimat angka terbaca. Baca satu halaman indeks resmi tambahan
            # sebelum menyerah ke model; tetap memakai cache dan batas langkah.
            if self._baca_katalog_jumlah():
                records = self._relevant_records()
                return _jumlah_program_studi_answer(records, pertanyaan)

            return None

        if (
            "fakultas" in daftar_topik
            and re.search(r"\bberapa\b", rendah)
        ):
            tersusun = _jumlah_fakultas_answer(records, pertanyaan)

            if tersusun:
                return tersusun

            if self._baca_katalog_jumlah():
                records = self._relevant_records()
                return _jumlah_fakultas_answer(records, pertanyaan)

            return None

        if {"fakultas", "program studi"} <= daftar_topik:
            return _jawaban_multi_aspek(records, pertanyaan)

        if (
            _adalah_profil_pertanyaan(pertanyaan)
            or _adalah_pertanyaan_tahun_berdiri(pertanyaan)
        ):
            if any(_authoritative_profile(record) for record in records):
                return _profil_answer(records)
            return None

        topik = _topic(pertanyaan)
        if topik and _is_list_question(pertanyaan):
            return _katalog_answer(
                "", records, pertanyaan, hanya_prodi=False
            )

        if any(tanda in rendah for tanda in (
            "siapa", "rektor", "pimpinan", "dekan", "jabatan",
        )):
            tersusun = _identitas_answer(records, pertanyaan)
            if not tersusun:
                return None
            hasil = _klaim_identitas_didukung(
                tersusun, records, pertanyaan
            )
            if hasil is None:
                return tersusun
            valid, _ = hasil
            return tersusun if valid else None

        if "beasiswa" in daftar_topik and (
            _adalah_pertanyaan_luas(pertanyaan) or "status" in rendah
        ):
            return _beasiswa_answer(records, pertanyaan)

        if (
            "pmb" in daftar_topik
            or any(tanda in rendah for tanda in (
                "pendaftaran mahasiswa", "penerimaan mahasiswa",
                "cara daftar", "jalur masuk",
            ))
        ):
            return _prosedur_answer(records, pertanyaan)

        if "akreditasi" in rendah:
            return _akreditasi_answer(records, pertanyaan)

        return None

    def _record_search(self, result):
        if not isinstance(result, dict) or not result.get("ok"):
            return

        baru = 0

        for item in result.get("results", []):
            if not isinstance(item, dict):
                continue

            url = _validasi_url(item.get("url"))

            # Artefak editor (/?elementor_library=...) bukan halaman publik
            # yang stabil dan sering mengembalikan HTTP 401 pada REST.
            if url and "elementor_library" in urllib.parse.urlsplit(url).query.lower():
                continue

            if url:
                self.url_diizinkan.add(url)

                if url not in self.kandidat:
                    baru += 1

                self._add_unique(self.kandidat, url)
                self._add_unique(self.sumber, url)
                self.info_kandidat[url] = {
                    "judul": str(item.get("judul", "")),
                    "cuplikan": str(item.get("cuplikan", "")),
                    "tanggal": str(item.get("tanggal", "")),
                    "jenis": str(item.get("jenis", "")),
                    "sumber": str(
                        item.get("_sumber_kandidat")
                        or result.get("sumber", "")
                    ),
                    "subkueri": list(item.get("subkueri") or []),
                    "terverifikasi_sitemap": bool(
                        item.get("terverifikasi_sitemap", False)
                    ),
                }

                self._catat_status(
                    url,
                    "retrieved",
                    judul=self.info_kandidat[url]["judul"],
                    jenis=self.info_kandidat[url]["jenis"],
                    tanggal=self.info_kandidat[url]["tanggal"],
                    skor=_skor(
                        self.pertanyaan,
                        self.info_kandidat[url]["judul"],
                        self.info_kandidat[url]["cuplikan"],
                    ),
                    alasan=(
                        "ditemukan melalui %s; subkueri %s"
                        % (
                            self.info_kandidat[url]["sumber"] or "pencarian",
                            ", ".join(self.info_kandidat[url]["subkueri"])
                            or "utama",
                        )
                    ),
                )

        if baru:
            telemetri.tambah("halaman_dicari", baru)

    def _relevant_records(self):
        with telemetri.ukur("relevant_records"):
            return self._relevant_records_impl()

    def _relevant_records_impl(self):
        """Pilih bukti untuk giliran ini, bukan sekadar semua halaman lama."""

        relevan = []
        q_rujukan = self._pertanyaan_rujukan()
        topik = _topic(q_rujukan)
        daftar_topik = set(_topik_pertanyaan(q_rujukan))
        profil = (
            _adalah_profil_pertanyaan(self.pertanyaan)
            or _adalah_pertanyaan_tahun_berdiri(self.pertanyaan)
        )
        lanjutan = _adalah_pertanyaan_lanjutan(self.pertanyaan)

        for record in self.bukti:
            # Halaman dari giliran lain hanya dipakai bila pertanyaannya
            # memang lanjutan, topiknya sama, atau halaman itu adalah katalog/
            # profil resmi yang memang memuat topik yang diminta. Pemeriksaan
            # topik juga dipakai ketika model tidak mencari lagi sehingga
            # turn_url kosong; ini mencegah pertanyaan baru tercemar konteks
            # lama tanpa membuang bukti kanonik yang sah untuk kedua topik.
            topik_lama = set(_topik_pertanyaan(record.get("pertanyaan", "")))
            topik_record = _topik_record(record)
            katalog = bool(topik and _authoritative_catalog(record, topik))
            halaman_profil = bool(profil and _authoritative_profile(record))
            cocok_topik = bool(daftar_topik & topik_record)
            bukti_dosen = bool(
                perencana.pahami_dosen(self.pertanyaan)["aktif"]
                and _profesor_evidence([record], self.pertanyaan)
            )
            info_lokasi = perencana.pahami_lokasi(self.pertanyaan)
            halaman_lokasi = bool(
                info_lokasi["aktif"] and _authoritative_lokasi(record)
            )

            # Untuk pertanyaan lokasi, hanya halaman kontak/profil/maps resmi
            # yang memuat alamat yang boleh menjadi bukti.
            if info_lokasi["aktif"] and not halaman_lokasi:
                self._catat_alasan(
                    record.get("url"),
                    "ditolak: bukan halaman kontak/profil resmi untuk lokasi",
                )
                continue

            if (
                daftar_topik
                and topik_lama
                and not (topik_lama & daftar_topik)
                and not lanjutan
                and not katalog
                and not halaman_profil
                and not bukti_dosen
                and not halaman_lokasi
            ):
                self._catat_alasan(
                    record.get("url"),
                    "ditolak: topik halaman tidak sama dengan pertanyaan "
                    "baru dan bukan pertanyaan lanjutan",
                )
                continue

            if (
                self.turn_url
                and record["url"] not in self.turn_url
                and not lanjutan
                and not katalog
                and not halaman_profil
                and not bukti_dosen
                and not halaman_lokasi
            ):
                self._catat_alasan(
                    record.get("url"),
                    "ditolak: halaman berasal dari giliran lain",
                )
                continue

            if (
                daftar_topik
                and topik_record
                and not cocok_topik
                and not katalog
                and not halaman_profil
                and not bukti_dosen
                and not halaman_lokasi
            ):
                continue

            if profil and topik_record and not halaman_profil:
                continue

            skor = _skor(
                q_rujukan,
                record.get("judul", ""),
                record.get("teks", ""),
            )

            if katalog or halaman_profil or halaman_lokasi:
                skor += 0.6
            if cocok_topik:
                skor += 0.4
            if bukti_dosen:
                skor += 0.5
            if record.get("url") in self.turn_url:
                skor += 0.2

            if skor < 0.20 and not (
                katalog or halaman_profil or cocok_topik
                or bukti_dosen or halaman_lokasi
            ):
                self._catat_alasan(
                    record.get("url"),
                    "ditolak: skor relevansi %.2f di bawah ambang dan bukan "
                    "halaman kanonik/topik" % skor,
                )
                continue

            alasan = "diterima sebagai bukti: skor %.2f" % skor
            if katalog or halaman_profil or halaman_lokasi:
                alasan += ", halaman kanonik"
            if cocok_topik:
                alasan += ", topik sesuai"

            self._catat_status(
                record.get("url"),
                "evidence",
                judul=record.get("judul", ""),
                jenis=record.get("jenis", ""),
                tanggal=record.get("tanggal", ""),
                http=200,
                panjang_teks=len(str(record.get("teks", ""))),
                skor=skor,
                alasan=alasan,
            )

            relevan.append((skor, record.get("url", ""), record))

        relevan.sort(key=lambda item: (-item[0], item[1]))

        return [record for _, _, record in relevan]

    def _finalize(self, message, status, extra=""):
        with telemetri.ukur("jawaban_akhir"):
            return self._finalize_impl(message, status, extra)

    def _finalize_impl(self, message, status, extra=""):
        answer = _bersihkan_jawaban(message.get("content", ""))

        if not answer.strip():
            answer = (
                "Maaf, saya belum dapat menyusun jawaban "
                "dari informasi yang tersedia."
            )

        q_rujukan = self._pertanyaan_rujukan()
        daftar_topik = _topik_pertanyaan(q_rujukan)

        if self.verifikasi_wajib:
            records = self._relevant_records()
            topik = _topic(self.pertanyaan)
            butuh_katalog = _butuh_katalog(self.pertanyaan)
            tahun_berdiri = _adalah_pertanyaan_tahun_berdiri(
                self.pertanyaan
            )
            profil = (
                not topik
                and (
                    _adalah_profil_pertanyaan(self.pertanyaan)
                    or tahun_berdiri
                )
            )
            dosen = perencana.pahami_dosen(self.pertanyaan)
            info_lokasi = perencana.pahami_lokasi(self.pertanyaan)

            # Pertanyaan dosen/Profesor/Guru Besar memakai jalur bukti khusus.
            # Pencarian kanonik dipaksa agar tidak terjadi 0 langkah walaupun
            # model langsung menjawab tanpa memanggil tool.
            if dosen["aktif"] and self.langkah < self.max_langkah:
                if not _profesor_answer(records, self.pertanyaan):
                    if self._ensure_evidence(
                        query=_query_profesor_kanonik(self.pertanyaan),
                        paksa_cari=True,
                    ):
                        records = self._relevant_records()

            # Lokasi/alamat juga memakai pencarian kanonik bila bootstrap awal
            # belum menemukan halaman kontak/profil yang memuat alamat.
            if info_lokasi["aktif"] and self.langkah < self.max_langkah:
                if not _lokasi_answer(records, self.pertanyaan):
                    if self._ensure_evidence(
                        query=info_lokasi["kanonik"], paksa_cari=True
                    ):
                        records = self._relevant_records()

            # Pertanyaan daftar tidak boleh dijawab hanya dari berita atau
            # bukti lama. Bila katalog belum terbaca, cari dan baca katalog
            # terlebih dahulu sebelum menilai klaim model.
            if butuh_katalog and not _ada_data_katalog(self.bukti, topik):
                # Bila kandidat dari pencarian model tidak memuat halaman
                # indeks resmi, ulangi pencarian dengan kata kunci kanonik.
                ada_indeks = any(
                    _prioritas_url(url) == 0
                    and "elementor_library" not in url.lower()
                    for url in self.kandidat
                )

                if self._ensure_evidence(
                    query=self.pertanyaan,
                    paksa_cari=not ada_indeks,
                ):
                    records = self._relevant_records()

            # Pertanyaan profil juga tidak boleh dijawab hanya dari berita.
            # Untuk "kapan UIR berdiri", query asli biasanya mengembalikan
            # berita pendirian unit/program studi. Karena itu pencarian profil
            # kanonik dipaksa dan URL /profil/ resmi didahulukan.
            profil_dicoba = False

            if profil and not any(
                _authoritative_profile(record) for record in self.bukti
            ):
                profil_dicoba = True
                if tahun_berdiri:
                    berhasil = self._ensure_evidence(
                        query="profil UIR", paksa_cari=True
                    )
                else:
                    berhasil = self._ensure_evidence()

                if berhasil:
                    records = self._relevant_records()

            # Pertanyaan luas seperti fasilitas atau pimpinan sering dijawab
            # model hanya dari berita. Bila belum ada satu pun halaman resmi
            # (non-artikel) yang terbaca, baca kandidat kanonik yang sudah
            # ada pada hasil pencarian.
            if (
                _adalah_pertanyaan_luas(self.pertanyaan)
                and not butuh_katalog
                and not profil
                and records
                and not _ada_halaman_kanonik(records)
                and self.langkah < self.max_langkah
                and self._ensure_evidence()
            ):
                records = self._relevant_records()

            # Bila pencarian profil kanonik sudah dicoba dan gagal, mengulang
            # pencarian yang sama hanya menambah latensi tanpa bukti baru.
            # Jalur cepat tidak boleh memanggil model lagi karena kandidat
            # jawabannya sudah berasal dari bukti yang tervalidasi.
            if (
                not records
                and not profil_dicoba
                and not self._jalur_cepat_aktif
            ):
                if self._ensure_evidence():
                    ke_model = telemetri.tambah(
                        "panggilan_model", kembalikan=True
                    )

                    with telemetri.ukur(
                        "model", panggilan_ke=ke_model, fase="retry"
                    ):
                        retry_response = self.client.chat(
                            messages=self._messages_untuk_model(),
                            tools=None,
                        )

                    retry = retry_response.get("message", {})

                    if (
                        isinstance(retry, dict)
                        and not retry.get("tool_calls")
                    ):
                        answer = retry.get("content", answer)
                        records = self._relevant_records()

            if not records:
                if info_lokasi["aktif"]:
                    return self._reject(
                        "Alamat/lokasi kampus tidak ditemukan pada halaman "
                        "kontak atau profil resmi UIR yang berhasil dibaca."
                    )
                if dosen["aktif"]:
                    return self._reject(
                        "Isi halaman UIR yang relevan belum berhasil dibaca.",
                        jawaban=self._penolakan_profesor(self.bukti),
                    )
                return self._reject(
                    "Isi halaman UIR yang relevan belum berhasil dibaca."
                )

            daftar_topik = _topik_pertanyaan(q_rujukan)
            sudah_terstruktur = False
            jawaban_dosen = False

            # Jawaban dosen/Profesor/Guru Besar disusun dari nama, jabatan,
            # dan unit yang benar-benar terbaca. Model tidak boleh menambah
            # nama rekaan atau mengubah jabatan.
            if dosen["aktif"]:
                jawaban_dosen = True
                tersusun = _profesor_answer(records, self.pertanyaan)
                if tersusun:
                    valid, reason = _klaim_profesor_didukung(
                        tersusun, records, self.pertanyaan
                    )
                    if valid:
                        answer = tersusun
                        sudah_terstruktur = True
                    else:
                        return self._reject(
                            reason or "Klaim profesor tidak didukung bukti.",
                            jawaban=self._penolakan_profesor(records),
                        )
                else:
                    return self._reject(
                        "Nama dan jabatan Profesor/Guru Besar tidak ditemukan "
                        "pada halaman yang berhasil dibaca.",
                        jawaban=self._penolakan_profesor(records),
                    )

            # Jawaban lokasi/alamat/kontak disusun dari baris halaman kontak
            # atau profil resmi yang benar-benar terbaca. Berita CBT/PMB yang
            # kebetulan memuat kata "lokasi" tidak boleh dipakai sebagai alamat
            # kampus, walaupun model menyertakannya pada jawaban.
            if info_lokasi["aktif"] and not sudah_terstruktur:
                tersusun_lokasi = _lokasi_answer(records, self.pertanyaan)

                if tersusun_lokasi:
                    valid_lokasi, alasan_lokasi = _klaim_lokasi_didukung(
                        tersusun_lokasi, records, self.pertanyaan
                    )
                    if valid_lokasi:
                        answer = tersusun_lokasi
                        sudah_terstruktur = True
                    else:
                        return self._reject(
                            alasan_lokasi
                            or "Klaim lokasi tidak didukung halaman resmi."
                        )
                else:
                    return self._reject(
                        "Alamat/kontak tidak ditemukan pada halaman kontak "
                        "atau profil resmi UIR yang berhasil dibaca."
                    )

            # Untuk tiga topik kritis, jawaban tidak disusun bebas oleh model.
            # Potongan halaman resmi yang terbaca menjadi sumber butir, sedangkan
            # model hanya dipakai untuk pencarian dan navigasi tool.
            # Pertanyaan lanjutan memakai bukti sesi sebelumnya; pencarian
            # tambahan hanya untuk pertanyaan baru. Ini menjaga perilaku lama
            # dan mencegah permintaan halaman berulang.
            # Pertanyaan jumlah program studi harus membaca halaman indeks
            # resmi. Berita yang kebetulan membahas daftar prodi tidak cukup
            # untuk dijadikan dasar angka.
            if (
                self.verifikasi_wajib
                and "program studi" in daftar_topik
                and re.search(r"\bberapa\b", self.pertanyaan, re.I)
                and not any(
                    _jumlah_program_studi_answer([record], self.pertanyaan)
                    for record in records
                )
                and self.langkah < self.max_langkah
            ):
                if self._baca_katalog_jumlah():
                    records = self._relevant_records()

            if (
                self.verifikasi_wajib
                and not _adalah_pertanyaan_lanjutan(self.pertanyaan)
                and self.langkah < self.max_langkah
            ):
                if (
                    "pimpinan" in daftar_topik
                    and not _record_identitas(records)
                ):
                    if self._ensure_evidence(
                        query="pimpinan UIR", paksa_cari=True
                    ):
                        records = self._relevant_records()
                elif (
                    "pmb" in daftar_topik
                    and "beasiswa" not in daftar_topik
                    and not _record_pmb(records)
                ):
                    if self._ensure_evidence(
                        query="penerimaan mahasiswa baru UIR",
                        paksa_cari=True,
                    ):
                        records = self._relevant_records()
                elif (
                    "beasiswa" in daftar_topik
                    and _adalah_pertanyaan_luas(self.pertanyaan)
                    and len({
                        record.get("url") for record in records
                    }) < 2
                ):
                    if self._ensure_evidence(
                        query="beasiswa UIR", paksa_cari=True
                    ):
                        records = self._relevant_records()

            if self.verifikasi_wajib:
                tersusun = _jumlah_program_studi_answer(
                    records, self.pertanyaan
                )
                if tersusun:
                    answer = tersusun
                    sudah_terstruktur = True

                if not sudah_terstruktur:
                    tersusun = _jumlah_fakultas_answer(
                        records, self.pertanyaan
                    )
                    if tersusun:
                        answer = tersusun
                        sudah_terstruktur = True

                if not sudah_terstruktur:
                    tersusun = _identitas_answer(records, self.pertanyaan)
                    if tersusun:
                        answer = tersusun
                        sudah_terstruktur = True

                if not sudah_terstruktur:
                    tersusun = _beasiswa_answer(records, self.pertanyaan)
                    if tersusun:
                        answer = tersusun
                        sudah_terstruktur = True

                if (
                    not sudah_terstruktur
                    and "beasiswa" not in daftar_topik
                ):
                    tersusun = _prosedur_answer(records, self.pertanyaan)
                    if tersusun:
                        answer = tersusun
                        sudah_terstruktur = True

            # Pertanyaan dengan dua bagian katalog disusun dari parser untuk
            # masing-masing bagian. Ini mencegah model menyisipkan topik lain
            # (misalnya berita konferensi) ke dalam jawaban faktual.
            if {"fakultas", "program studi"} <= set(daftar_topik) and all(
                any(
                    _authoritative_catalog(record, nama)
                    for record in records
                )
                for nama in ("fakultas", "program studi")
            ):
                tersusun = _jawaban_multi_aspek(
                    records,
                    self.pertanyaan,
                    answer,
                )

                if tersusun:
                    answer = tersusun
                    sudah_terstruktur = True

            if not sudah_terstruktur and "akreditasi" in self.pertanyaan.lower():
                ringkasan = _akreditasi_answer(records, self.pertanyaan)

                if ringkasan:
                    answer = ringkasan
                    sudah_terstruktur = True

            # Angka untuk tahun mendatang tidak boleh dianggap berhasil hanya
            # karena model menjawab "tidak dapat memberi angka". Bila tahun
            # yang diminta tidak tertulis pada bukti, jawaban ditolak tegas.
            tahun_diminta = _tahun_diminta(self.pertanyaan)

            if not _tahun_tersedia_di_bukti(
                tahun_diminta,
                records,
                _kata_topik_pertanyaan(self.pertanyaan),
            ):
                return self._reject(
                    "Angka untuk tahun yang diminta tidak ditemukan pada "
                    "halaman resmi UIR yang berhasil dibaca."
                )

            if profil:
                # Model bebas memberi ringkasan, tetapi jawaban final tetap
                # diambil dari kalimat halaman profil resmi agar tidak
                # terjadi klaim tahun/tokoh yang tidak bersumber.
                with telemetri.ukur("parsing_profil"):
                    kanonik_profil = _profil_answer(records)

                if kanonik_profil is None:
                    return self._reject(
                        "Kalimat profil resmi tidak ditemukan pada sumber."
                    )

                answer = kanonik_profil

            if sudah_terstruktur:
                valid, reason = True, ""
            else:
                with telemetri.ukur("verifikasi"):
                    valid, reason = _claims_supported(
                        answer,
                        records,
                        q_rujukan,
                    )

            if not valid:
                with telemetri.ukur("parsing_katalog"):
                    kanonik = _katalog_answer(
                        answer,
                        records,
                        self.pertanyaan,
                    )

                # Model sering mencampur nama yang benar dengan satu nama
                # rekaan. Bila ada butir yang benar-benar didukung katalog,
                # susun ulang seluruh daftar dari parser. Jika semua butir
                # rekaan, tetap tolak.
                # Model juga sering menolak memberi daftar padahal katalog
                # sudah terbaca. Itu bukan kesalahan butir, melainkan jawaban
                # kosong, sehingga tetap boleh dilengkapi dari parser.
                butir_model = _extract_answer_items(answer)

                if (
                    kanonik is None
                    and butuh_katalog
                    and (
                        not butir_model
                        or _ada_butir_didukung(
                            answer, records, topik
                        )
                    )
                ):
                    with telemetri.ukur("parsing_katalog"):
                        kanonik = _katalog_answer(
                            answer,
                            records,
                            self.pertanyaan,
                            hanya_prodi=False,
                        )

                # Permintaan satu butir ("sebutkan satu fakultas saja") juga
                # bisa ditolak model walau katalog sudah terbaca. Lengkapi
                # dari parser, tetapi jangan gunakan jalur ini untuk
                # pertanyaan ya/tidak atau asumsi yang salah.
                if (
                    kanonik is None
                    and topik
                    and not butuh_katalog
                    and _is_list_question(self.pertanyaan)
                ):
                    with telemetri.ukur("parsing_katalog"):
                        kanonik = _satu_butir_katalog(records, topik)

                if kanonik is None:
                    # Model mungkin benar pada sebagian topik tetapi menambah
                    # klaim pada topik lain yang belum terbukti. Sampaikan
                    # bagian yang didukung dan akui bagian yang belum ada.
                    # Pertanyaan katalog tetap fail-closed bila parsernya tidak
                    # menemukan butir; topik non-katalog boleh diselamatkan
                    # dari potongan halaman resmi yang benar-benar terbaca.
                    sebagian = None

                    if (
                        len(daftar_topik) > 1
                        or (len(daftar_topik) == 1 and not butuh_katalog)
                    ):
                        sebagian = _jawaban_sebagian(
                            records,
                            self.pertanyaan,
                            answer,
                        )

                    if sebagian:
                        answer = sebagian
                        sudah_terstruktur = True
                    else:
                        return self._reject(reason)

                # Jawaban disusun ulang dari bagian katalog yang terbaca,
                # bukan dari butir campuran yang diberikan model.
                if kanonik is not None:
                    answer = kanonik

            if butuh_katalog:
                # Model bisa memberikan daftar yang benar tetapi tidak
                # lengkap. Lengkapi dari parser katalog agar jawaban "apa
                # saja" benar-benar mencakup seluruh butir di sumber.
                with telemetri.ukur("parsing_katalog"):
                    lengkap = _katalog_answer(
                        answer,
                        records,
                        self.pertanyaan,
                        hanya_prodi=False,
                    )

                if lengkap is not None:
                    answer = lengkap

            # Guard terakhir: jalur jawaban sebagian/paragraf halaman tidak
            # boleh mengubah penolakan verifikasi menjadi "berhasil" tanpa
            # angka untuk pertanyaan jumlah.
            if (
                not dosen["aktif"]
                and re.search(r"\bberapa\b", self.pertanyaan, re.I)
                and not re.search(r"\d", str(answer))
            ):
                return self._reject(
                    "Jawaban tidak memuat angka untuk pertanyaan jumlah."
                )

        elif self.sumber and not self.terbaca:
            # Mode non-ketat: tidak ada halaman yang benar-benar dibaca.
            # Jawaban tetap diberikan, tetapi diberi catatan jujur dan
            # tautan sumber agar pengguna tidak mengira sudah diverifikasi.
            answer = (
                answer.rstrip()
                + "\n\nCatatan: jawaban ini belum diverifikasi terhadap "
                + "isi halaman UIR yang berhasil dibaca.\n\nSumber:\n"
                + "\n".join("- " + url for url in self.sumber)
            )

        final_records = self._relevant_records()

        for record in final_records:
            self._add_unique(self.terbaca, record["url"])

        if self.verifikasi_wajib:
            konflik = _deteksi_konflik(final_records)

            if konflik:
                answer = (
                    answer.rstrip()
                    + "\n\nCatatan: "
                    + " ".join(konflik)
                )

            dipakai = _sumber_dipakai(
                answer,
                final_records,
                self.pertanyaan,
            )
            # ``sumber`` pada hasil akhir adalah sitasi, bukan semua halaman
            # yang terbaca. ``sumber_terbaca`` tetap mencatat pembacaan.
            self.sumber = list(dipakai)
            records_dipakai = [
                record for record in final_records
                if str(record.get("url", "")) in dipakai
            ]

            if len(daftar_topik) > 1 and records_dipakai:
                answer = _sumber_per_bagian(
                    _ganti_sumber(answer, []),
                    records_dipakai,
                    self.pertanyaan,
                )
            else:
                answer = _ganti_sumber(answer, dipakai)

            self._tandai_status_akhir(answer, dipakai)

            # Setiap jawaban berstatus berhasil wajib memiliki sitasi yang
            # benar-benar terbaca. Bila atribusi sumber gagal, lebih baik
            # mengakui keterbatasan daripada menampilkan klaim tanpa tautan.
            if status == "berhasil" and not dipakai:
                for entri in self.status_sumber.values():
                    if isinstance(entri, dict):
                        entri["cited"] = False
                self.sumber = []
                return self._reject(
                    "Jawaban tidak memiliki sitasi halaman resmi UIR yang "
                    "dapat diverifikasi. Saya tidak akan menampilkan klaim "
                    "tanpa sumber."
                )

            # Guard final: walaupun klaim lolos verifikasi, jawaban tetap
            # ditolak bila tidak menjawab pokok pertanyaan. Ini mencegah
            # potongan berita yang kebetulan bersumber dianggap sebagai
            # jawaban faktual (misalnya ditanya wakil rektor, dijawab rektor).
            if status == "berhasil" and not _jawaban_menjawab_pertanyaan(
                self.pertanyaan, answer
            ):
                for entri in self.status_sumber.values():
                    if isinstance(entri, dict):
                        entri["cited"] = False
                self.sumber = []
                return self._reject(
                    _alasan_jawaban_melenceng(self.pertanyaan)
                )
        else:
            for record in final_records:
                self._add_unique(self.sumber, record["url"])

            missing = [
                url for url in self.terbaca
                if url not in answer
            ]

            if missing:
                answer = _tambah_sumber(answer, missing)

        self._append({
            "role": "assistant",
            "content": answer,
        })

        return _hasil(
            True,
            status,
            answer,
            self.sumber,
            self.terbaca,
            self.langkah,
            extra,
            status_sumber=self.status_sumber,
        )

    def _minta_klarifikasi(self):
        """Tanya topik yang dimaksud tanpa menyusun jawaban faktual."""

        jawaban = (
            "Maksud Anda salah satu dari informasi mana? "
            "Silakan sebutkan topiknya, misalnya fakultas, program studi, "
            "atau fasilitas UIR."
        )

        self._append({
            "role": "assistant",
            "content": jawaban,
        })

        return _hasil(
            False,
            "butuh_klarifikasi",
            jawaban,
            [],
            [],
            self.langkah,
            "Pertanyaan ambigu: topik yang dirujuk tidak disebutkan.",
        )

    def _pencarian_gagal(self):
        """Apakah pencarian terakhir benar-benar gagal dijalankan?"""

        hasil = self.pencarian_terakhir

        return (
            isinstance(hasil, dict)
            and (
                hasil.get("ok") is not True
                or str(hasil.get("status", "")).lower() in {
                    "situs_tidak_tersedia", "diblokir_robots",
                    "validasi_gagal",
                }
            )
        )

    def _penolakan_profesor(self, records):
        """Penolakan transparan untuk pencarian profesor/Guru Besar."""

        info = perencana.pahami_dosen(self.pertanyaan)
        halaman = []
        for record in records or []:
            if not isinstance(record, dict):
                continue
            label = str(record.get("judul", "") or "halaman UIR").strip()
            url = str(record.get("url", "") or "").strip()
            if url and url not in [item[1] for item in halaman]:
                halaman.append((label, url))

        if self._pencarian_gagal():
            awal = (
                "Pencarian pada halaman resmi UIR belum berhasil dijalankan, "
                "jadi saya belum dapat menilai apakah bukti tersedia."
            )
        elif self.langkah == 0 or self.pencarian_terakhir is None:
            awal = (
                "Pencarian belum sempat dijalankan, jadi saya belum dapat "
                "menyimpulkan apa pun dari halaman resmi UIR."
            )
        else:
            awal = (
                "Saya sudah mencari beberapa variasi kata kunci profesor/"
                "Guru Besar pada domain resmi uir.ac.id dan membaca "
                + str(len(self.terbaca))
                + " halaman."
            )

        if halaman:
            ditemukan = "Halaman yang berhasil dibaca: " + "; ".join(
                "%s (%s)" % (label, url) for label, url in halaman[:4]
            ) + "."
        else:
            ditemukan = "Tidak ada halaman yang berhasil dibaca."

        belum = (
            "Namun, saya belum menemukan teks yang menyebutkan nama lengkap "
            "beserta jabatan akademik Profesor/Guru Besar dan fakultas atau "
            "unit kerja secara eksplisit. Saya tidak dapat memastikan nama "
            "atau jumlah yang Anda minta dari bukti tersebut."
        )

        klarifikasi = ""
        if info["kategori"] == "profesor":
            klarifikasi = (
                " Jika maksud Anda berbeda, silakan klarifikasi apakah yang "
                "dimaksud Guru Besar, dosen Fakultas Teknik berjabatan "
                "Profesor, atau profesor dari lingkungan Fakultas Teknik."
            )

        return awal + " " + ditemukan + " " + belum + klarifikasi

    def _reject(self, reason, status=None, jawaban=None):
        if status is None:
            # Bedakan "tidak ada halaman yang berhasil dibaca" dari
            # "halaman terbaca, tetapi klaim tidak didukung" agar UI dan
            # pemanggil dapat memberi pesan yang tepat.
            ada_halaman = bool(self.terbaca or self.bukti)
            status = (
                "bukti_tidak_cukup" if ada_halaman
                else "sumber_tidak_tersedia"
            )

        answer = jawaban or (
            "Maaf, saya belum dapat memastikan jawaban tersebut "
            "dari isi halaman resmi UIR yang berhasil dibaca. "
            "Saya tidak akan menebak atau melengkapi daftar tanpa bukti. "
            "Coba gunakan kata kunci yang lebih spesifik atau berikan "
            "URL halaman resmi UIR."
        )

        self._append({
            "role": "assistant",
            "content": answer,
        })

        return _hasil(
            False,
            status,
            answer,
            self.sumber,
            self.terbaca,
            self.langkah,
            reason,
            status_sumber=self.status_sumber,
        )

    @staticmethod
    def _add_unique(items, value):
        if value not in items:
            items.append(value)

    def _append(self, message):
        self.riwayat.append(message)

        if len(self.riwayat) > MAX_RIWAYAT:
            # Pesan system prompt selalu dipertahankan.
            self.riwayat = (
                [self.riwayat[0]]
                + self.riwayat[-(MAX_RIWAYAT - 1):]
            )

    @staticmethod
    def _assistant_message(message):
        copy = dict(message)
        copy.setdefault("role", "assistant")
        return copy

    def _tool_message(self, name, result):
        return {
            "role": "tool",
            "tool_name": name or "unknown",
            "content": json.dumps(
                _ringkas_untuk_model(result, self.pertanyaan),
                ensure_ascii=False,
            ),
        }

    def _error(self, exc):
        # Gunakan isinstance, bukan lookup tipe persis, supaya exception
        # spesifik penyedia model dan Ollama tetap dipetakan ke status yang sama.
        mapping = (
            (
                KunciTidakTersedia,
                "kunci_tidak_tersedia",
                "Kredensial penyedia model belum dikonfigurasi dengan benar.",
            ),
            (
                ModelTidakTersedia,
                "model_tidak_tersedia",
                "Model yang diminta tidak tersedia pada penyedia.",
            ),
            (
                ModelTidakTersediaAPI,
                "model_tidak_tersedia",
                "Model atau saldo penyedia tidak tersedia.",
            ),
            (
                OllamaTidakTersedia,
                "ollama_tidak_tersedia",
                "Ollama lokal tidak dapat dihubungi. Pastikan server berjalan.",
            ),
            (
                PenyediaTidakTersedia,
                "penyedia_tidak_tersedia",
                "Layanan model tidak dapat dihubungi.",
            ),
            (
                OllamaTimeout,
                "timeout",
                "Permintaan ke penyedia model melewati batas waktu.",
            ),
            (
                ModelTimeout,
                "timeout",
                "Permintaan ke penyedia model melewati batas waktu.",
            ),
            (
                ResponsTidakValid,
                "respons_tidak_valid",
                "Respons model tidak valid.",
            ),
            (
                ResponsModelTidakValid,
                "respons_tidak_valid",
                "Respons model tidak valid.",
            ),
            (
                KonfigurasiTidakValid,
                "konfigurasi_tidak_valid",
                "Konfigurasi penyedia model tidak valid.",
            ),
            (
                KonfigurasiTidakValidAPI,
                "konfigurasi_tidak_valid",
                "Konfigurasi penyedia model tidak valid.",
            ),
        )

        status, message = next(
            (
                (stat, pesan)
                for jenis, stat, pesan in mapping
                if isinstance(exc, jenis)
            ),
            (
                "kesalahan_internal",
                "Terjadi kesalahan internal.",
            ),
        )

        return _hasil(
            False,
            status,
            sumber=self.sumber,
            terbaca=self.terbaca,
            langkah=self.langkah,
            pesan=message,
        )


def _utama():
    print("Chatbot Informasi UIR (penyedia model dikonfigurasi via MODEL_PROVIDER).")
    print("Ketik 'keluar' untuk mengakhiri.\n")

    session = Orkestrator(verifikasi_wajib=True)

    while True:
        try:
            question = input("Anda: ").strip()

        except (EOFError, KeyboardInterrupt):
            print("\nSelesai.")
            break

        if question.lower() in {"keluar", "exit", "quit"}:
            print("Selesai.")
            break

        if not question:
            continue

        result = session.tanya(question)

        print("\nAsisten:", result["jawaban"])

        if not result["ok"]:
            print(
                "[status:",
                result["status"] + "]",
                result["pesan"],
            )

        print()


if __name__ == "__main__":
    _utama()