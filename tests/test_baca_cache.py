"""Penyimpanan dan pembacaan cache halaman publik UIR menggunakan SQLite."""
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.cari_uir import _validasi_url

LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_DIR = BASE_DIR / "database"
DATABASE_PATH = DATABASE_DIR / "cache.db"

TTL_JAM = 24
MAX_URL = 2000
MAX_JUDUL = 300
MAX_TEKS = 20000

STATUS_DITEMUKAN = "ditemukan"
STATUS_TIDAK_DITEMUKAN = "tidak_ditemukan"
STATUS_KEDALUWARSA = "kedaluwarsa"
STATUS_VALIDASI_GAGAL = "validasi_gagal"
STATUS_GAGAL = "gagal"


def _sekarang():
    return datetime.now(timezone.utc)


def _format_waktu(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_waktu(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _koneksi():
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DATABASE_PATH), timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _buat_tabel():
    connection = _koneksi()
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS cache_halaman (
                url TEXT PRIMARY KEY,
                judul TEXT NOT NULL DEFAULT '',
                teks TEXT NOT NULL,
                waktu_simpan TEXT NOT NULL,
                waktu_kedaluwarsa TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_cache_kedaluwarsa
            ON cache_halaman(waktu_kedaluwarsa)
        """)
        connection.commit()
    finally:
        connection.close()


def _hasil(status, ok, url, judul="", teks="", pesan=""):
    return {
        "ok": ok,
        "status": status,
        "url": url,
        "judul": judul,
        "teks": teks,
        "panjang": len(teks),
        "pesan": pesan,
        "waktu": _format_waktu(_sekarang()),
    }


def _validasi_data(data):
    if not isinstance(data, dict):
        return None

    url = _validasi_url(data.get("url"))
    judul = data.get("judul", "")
    teks = data.get("teks", "")

    if not url or len(url) > MAX_URL:
        return None
    if not isinstance(judul, str) or len(judul) > MAX_JUDUL:
        return None
    if not isinstance(teks, str) or not teks.strip() or len(teks) > MAX_TEKS:
        return None

    return url, judul.strip(), teks.strip()


def simpan_cache(data):
    """Simpan halaman yang berhasil diambil ke cache SQLite."""
    validated = _validasi_data(data)
    if not validated:
        return {
            "ok": False,
            "status": STATUS_VALIDASI_GAGAL,
            "pesan": "Data halaman tidak valid untuk disimpan ke cache.",
        }

    url, judul, teks = validated
    now = _sekarang()
    saved = _format_waktu(now)
    expires = _format_waktu(now + timedelta(hours=TTL_JAM))

    connection = None
    try:
        _buat_tabel()
        connection = _koneksi()
        connection.execute("""
            INSERT INTO cache_halaman
                (url, judul, teks, waktu_simpan, waktu_kedaluwarsa)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                judul = excluded.judul,
                teks = excluded.teks,
                waktu_simpan = excluded.waktu_simpan,
                waktu_kedaluwarsa = excluded.waktu_kedaluwarsa
        """, (url, judul, teks, saved, expires))
        connection.commit()
        return {
            "ok": True,
            "status": "tersimpan",
            "url": url,
            "waktu_simpan": saved,
            "waktu_kedaluwarsa": expires,
            "ttl_jam": TTL_JAM,
        }
    except sqlite3.Error:
        LOGGER.exception("Gagal menyimpan cache halaman.")
        return {
            "ok": False,
            "status": STATUS_GAGAL,
            "pesan": "Cache gagal disimpan.",
        }
    finally:
        if connection is not None:
            connection.close()


def baca_cache(url):
    """Baca halaman dari cache; fungsi ini tidak mengakses internet."""
    if not isinstance(url, str) or not url.strip() or len(url.strip()) > MAX_URL:
        return _hasil(
            STATUS_VALIDASI_GAGAL, False, str(url),
            pesan="URL kosong atau terlalu panjang."
        )

    address = _validasi_url(url)
    if not address:
        return _hasil(
            STATUS_VALIDASI_GAGAL, False, url,
            pesan="Hanya URL HTTPS dengan hostname tepat uir.ac.id yang diizinkan."
        )

    connection = None
    try:
        _buat_tabel()
        connection = _koneksi()
        row = connection.execute(
            """
            SELECT url, judul, teks, waktu_simpan, waktu_kedaluwarsa
            FROM cache_halaman
            WHERE url = ?
            """,
            (address,),
        ).fetchone()

        if row is None:
            return _hasil(
                STATUS_TIDAK_DITEMUKAN, True, address,
                pesan="Halaman belum tersedia di cache."
            )

        expires = _parse_waktu(row["waktu_kedaluwarsa"])
        if expires is None or _sekarang() >= expires:
            return _hasil(
                STATUS_KEDALUWARSA, True, address,
                judul=row["judul"],
                pesan="Cache sudah kedaluwarsa dan perlu diperbarui."
            )

        return _hasil(
            STATUS_DITEMUKAN, True, address,
            judul=row["judul"],
            teks=row["teks"],
            pesan="Isi halaman ditemukan di cache."
        )

    except sqlite3.Error:
        LOGGER.exception("Gagal membaca cache halaman.")
        return _hasil(
            STATUS_GAGAL, False, address,
            pesan="Cache gagal dibaca."
        )
    finally:
        if connection is not None:
            connection.close()


def hapus_cache_kedaluwarsa():
    """Hapus baris cache yang sudah melewati waktu kedaluwarsa."""
    connection = None
    try:
        _buat_tabel()
        connection = _koneksi()
        cursor = connection.execute(
            "DELETE FROM cache_halaman WHERE waktu_kedaluwarsa <= ?",
            (_format_waktu(_sekarang()),),
        )
        connection.commit()
        return {
            "ok": True,
            "status": "berhasil",
            "jumlah_dihapus": cursor.rowcount,
        }
    except sqlite3.Error:
        LOGGER.exception("Gagal membersihkan cache kedaluwarsa.")
        return {
            "ok": False,
            "status": STATUS_GAGAL,
            "jumlah_dihapus": 0,
        }
    finally:
        if connection is not None:
            connection.close()


# Implementasi lama dipertahankan sebagai referensi. DDL tidak dijalankan saat
# import agar skema lama tidak dibuat di database cache yang aktif.
if __name__ == "__main__":
    try:
        _buat_tabel()
    except Exception:
        LOGGER.exception("Tabel cache belum dapat dibuat saat import.")