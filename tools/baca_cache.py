"""Tool ``baca_cache``: menyimpan dan membaca hasil ``ambil_halaman`` di SQLite.

Cache bertahan setelah aplikasi ditutup karena disimpan dalam file database.
Tool ini MURNY lokal: tidak melakukan permintaan jaringan apa pun. Validasi URL
dipakai ulang dari :mod:`tools.cari_uir` agar aturan hostname konsisten.

Contoh pemakaian::

    from tools.baca_cache import baca_cache, simpan_cache
    simpan_cache(hasil_ambil_halaman)
    hasil = baca_cache("https://uir.ac.id/beasiswa/")
"""

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.cari_uir import _validasi_url

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstanta yang mudah diubah
# ---------------------------------------------------------------------------

# Masa berlaku cache dalam jam. Kedaluwarsa dihitung SAAT PEMBACAAN sehingga
# perubahan nilai di sini langsung berlaku untuk data yang sudah tersimpan.
TTL_JAM = 24

# Lokasi database relatif terhadap root proyek (induk folder ``tools``).
_PATH_DATABASE = Path(__file__).resolve().parent.parent / "database" / "cache.db"

MAX_URL = 2000  # batas panjang URL sebelum divalidasi lebih lanjut

STATUS_DITEMUKAN = "ditemukan"
STATUS_TIDAK_DITEMUKAN = "tidak_ditemukan"
STATUS_KEDALUWARSA = "kedaluwarsa"
STATUS_VALIDASI_GAGAL = "validasi_gagal"
STATUS_DATABASE_ERROR = "database_error"

STATUS_TERSIMPAN = "tersimpan"
STATUS_DATA_TIDAK_VALID = "data_tidak_valid"

_SQL_BUAT_TABEL = """
CREATE TABLE IF NOT EXISTS cache_halaman (
    url TEXT PRIMARY KEY,
    judul TEXT NOT NULL,
    teks TEXT NOT NULL,
    panjang INTEGER NOT NULL,
    waktu_pengambilan TEXT NOT NULL,
    waktu_cache TEXT NOT NULL
)
"""


# ---------------------------------------------------------------------------
# Helper privat
# ---------------------------------------------------------------------------

def _sekarang():
    """Waktu UTC saat ini (dapat di-mock dalam pengujian)."""
    return datetime.now(timezone.utc)


def _iso(waktu):
    """Format ISO 8601 UTC dengan presisi detik."""
    return waktu.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_waktu(teks):
    """Ubah string ISO menjadi datetime UTC; None bila tidak valid."""
    if not isinstance(teks, str) or not teks:
        return None
    try:
        hasil = datetime.fromisoformat(teks)
    except ValueError:
        return None
    if hasil.tzinfo is None:
        hasil = hasil.replace(tzinfo=timezone.utc)
    return hasil.astimezone(timezone.utc)


def _koneksi():
    """Buka koneksi, pastikan folder dan tabel ada. Pemanggil wajib menutupnya."""
    _PATH_DATABASE.parent.mkdir(parents=True, exist_ok=True)
    koneksi = sqlite3.connect(str(_PATH_DATABASE))
    koneksi.row_factory = sqlite3.Row  # akses kolom dengan nama, bukan indeks
    koneksi.execute(_SQL_BUAT_TABEL)
    return koneksi


def _hasil_baca(status, ok, url, data=None, waktu_cache="", kedaluwarsa=""):
    """Bangun dictionary hasil baca_cache yang aman untuk json.dumps()."""
    return {
        "ok": ok,
        "status": status,
        "url": url,
        "data": data,
        "waktu_cache": waktu_cache,
        "kedaluwarsa_pada": kedaluwarsa,
        "pesan": "",
    }


def _hasil_simpan(status, ok, url, pesan):
    """Bangun dictionary hasil simpan_cache yang aman untuk json.dumps()."""
    return {
        "ok": ok,
        "status": status,
        "url": url,
        "pesan": pesan,
    }


def _validasi(url):
    """Kembalikan (alamat_tervalidasi, pesan_gagal). Tidak pernah buat jaringan."""
    if not isinstance(url, str) or not url.strip() or len(url.strip()) > MAX_URL:
        return None, "URL harus berupa teks tidak kosong dan tidak terlalu panjang."
    alamat = _validasi_url(url)
    if alamat is None:
        return None, (
            "URL tidak valid: hanya https://uir.ac.id (tanpa www/subdomain, "
            "tanpa username/password, dan port 443/default) yang diizinkan."
        )
    return alamat, ""


# ---------------------------------------------------------------------------
# API publik
# ---------------------------------------------------------------------------

def baca_cache(url):
    """Baca halaman dari cache bila masih berlaku.

    Tidak ada permintaan internet di dalam fungsi ini.

    Returns
    -------
    dict
        Kunci: ``ok``, ``status``, ``url``, ``data``, ``waktu_cache``,
        ``kedaluwarsa_pada``, dan ``pesan``. ``data`` berisi ``url``, ``judul``,
        ``teks``, ``panjang``, dan ``waktu`` (waktu asli dari ``ambil_halaman``).
    """
    tampil = url if isinstance(url, str) else str(url)
    alamat, galat = _validasi(url)
    if alamat is None:
        hasil = _hasil_baca(STATUS_VALIDASI_GAGAL, False, tampil)
        hasil["pesan"] = galat
        return hasil

    try:
        with closing(_koneksi()) as koneksi:
            baris = koneksi.execute(
                """SELECT url, judul, teks, panjang, waktu_pengambilan, waktu_cache
                   FROM cache_halaman WHERE url = ?""",
                (alamat,),
            ).fetchone()
    except (sqlite3.Error, OSError) as exc:
        LOGGER.warning("Gagal membaca cache: %s", exc)
        hasil = _hasil_baca(STATUS_DATABASE_ERROR, False, alamat)
        hasil["pesan"] = "Database cache tidak dapat dibaca: %s" % exc
        return hasil
    except Exception:
        LOGGER.exception("Kesalahan tak terduga saat membaca cache")
        hasil = _hasil_baca(STATUS_DATABASE_ERROR, False, alamat)
        hasil["pesan"] = "Terjadi kesalahan saat mengakses database cache."
        return hasil

    if baris is None:
        hasil = _hasil_baca(STATUS_TIDAK_DITEMUKAN, False, alamat)
        hasil["pesan"] = "Tidak ada cache untuk URL tersebut."
        return hasil

    dibuat = _parse_waktu(baris["waktu_cache"])
    if dibuat is None:
        hasil = _hasil_baca(STATUS_DATABASE_ERROR, False, alamat)
        hasil["pesan"] = "Data cache memiliki waktu yang tidak valid."
        return hasil

    habis = dibuat + timedelta(hours=TTL_JAM)
    hasil = _hasil_baca(
        STATUS_DITEMUKAN, True, alamat,
        data={
            "url": baris["url"],
            "judul": baris["judul"],
            "teks": baris["teks"],
            "panjang": baris["panjang"],
            "waktu": baris["waktu_pengambilan"],
        },
        waktu_cache=_iso(dibuat),
        kedaluwarsa=_iso(habis),
    )
    hasil["pesan"] = "Cache ditemukan dan masih berlaku."

    if _sekarang() >= habis:
        hasil = _hasil_baca(
            STATUS_KEDALUWARSA, False, alamat,
            waktu_cache=_iso(dibuat),
            kedaluwarsa=_iso(habis),
        )
        hasil["pesan"] = "Cache sudah melewati masa berlaku %d jam." % TTL_JAM
    return hasil


def simpan_cache(hasil):
    """Simpan/memperbarui cache dari dictionary hasil ``ambil_halaman``.

    Hanya hasil dengan ``ok=True`` dan ``status='berhasil'`` yang disimpan.

    Returns
    -------
    dict
        Kunci: ``ok``, ``status``, ``url``, dan ``pesan``.
    """
    tampil = ""
    if isinstance(hasil, dict):
        tampil = hasil.get("url") if isinstance(hasil.get("url"), str) else str(hasil.get("url"))
    else:
        tampil = str(hasil)

    if not isinstance(hasil, dict):
        return _hasil_simpan(
            STATUS_DATA_TIDAK_VALID, False, tampil, "Hasil harus berupa dictionary."
        )

    alamat, galat = _validasi(hasil.get("url"))
    if alamat is None:
        return _hasil_simpan(STATUS_DATA_TIDAK_VALID, False, tampil, galat)

    if hasil.get("ok") is not True or hasil.get("status") != "berhasil":
        return _hasil_simpan(
            STATUS_DATA_TIDAK_VALID, False, alamat,
            "Hanya hasil dengan ok=True dan status='berhasil' yang disimpan.",
        )

    judul = hasil.get("judul", "")
    teks = hasil.get("teks")
    if not isinstance(judul, str) or not isinstance(teks, str) or not teks.strip():
        return _hasil_simpan(
            STATUS_DATA_TIDAK_VALID, False, alamat,
            "Judul harus teks dan isi halaman tidak boleh kosong.",
        )

    panjang = hasil.get("panjang")
    if isinstance(panjang, bool) or not isinstance(panjang, int) or panjang < 0:
        panjang = len(teks)

    waktu_pengambilan = hasil.get("waktu")
    if _parse_waktu(waktu_pengambilan) is None:
        # Bukan mengarang metadata: ini waktu pencatatan lokal bila sumber
        # tidak menyediakan waktu pengambilan yang valid.
        waktu_pengambilan = _iso(_sekarang())
    else:
        waktu_pengambilan = _iso(_parse_waktu(waktu_pengambilan))

    try:
        with closing(_koneksi()) as koneksi:
            # url adalah PRIMARY KEY sehingga ON CONFLICT mencegah duplikat.
            koneksi.execute(
                """INSERT INTO cache_halaman
                       (url, judul, teks, panjang, waktu_pengambilan, waktu_cache)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                       judul = excluded.judul,
                       teks = excluded.teks,
                       panjang = excluded.panjang,
                       waktu_pengambilan = excluded.waktu_pengambilan,
                       waktu_cache = excluded.waktu_cache""",
                (alamat, judul, teks, panjang, waktu_pengambilan, _iso(_sekarang())),
            )
            koneksi.commit()
    except (sqlite3.Error, OSError) as exc:
        LOGGER.warning("Gagal menyimpan cache: %s", exc)
        return _hasil_simpan(
            STATUS_DATABASE_ERROR, False, alamat,
            "Database cache tidak dapat ditulis: %s" % exc,
        )
    except Exception:
        LOGGER.exception("Kesalahan tak terduga saat menyimpan cache")
        return _hasil_simpan(
            STATUS_DATABASE_ERROR, False, alamat,
            "Terjadi kesalahan saat menulis database cache.",
        )

    return _hasil_simpan(
        STATUS_TERSIMPAN, True, alamat, "Hasil halaman berhasil disimpan ke cache."
    )
