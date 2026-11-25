"""Muat variabel lingkungan dari file ``.env`` lokal tanpa mencetak nilainya.

Modul ini sengaja tidak memakai dependency tambahan. Nilai yang sudah ada di
environment proses lebih prioritif (``setdefault``), sehingga export shell
tetap berfungsi seperti sebelumnya. File ``.env`` tidak pernah dibaca ke log
atau dikembalikan ke pemanggil.
"""

import os
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / ".env"
_SUDAH_DIMUAT = False


def _bersihkan(nilai):
    nilai = nilai.strip()
    if len(nilai) >= 2 and nilai[0] == nilai[-1] and nilai[0] in ("'", '"'):
        return nilai[1:-1]
    for indeks, karakter in enumerate(nilai):
        if karakter == "#" and (indeks == 0 or nilai[indeks - 1].isspace()):
            return nilai[:indeks].rstrip()
    return nilai


def muat_env(path=None):
    """Isi ``os.environ`` dari file .env bila ada; jangan timpa nilai lama."""
    global _SUDAH_DIMUAT
    if _SUDAH_DIMUAT and path is None:
        return

    target = Path(path) if path is not None else _PATH
    _SUDAH_DIMUAT = True
    if not target.is_file():
        return

    try:
        baris_list = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return

    for baris in baris_list:
        baris = baris.strip()
        if not baris or baris.startswith("#"):
            continue
        if baris.startswith("export "):
            baris = baris[7:].strip()
        if "=" not in baris:
            continue

        kunci, nilai = baris.split("=", 1)
        kunci = kunci.strip()
        if not kunci or not kunci.replace("_", "").isalnum():
            continue
        os.environ.setdefault(kunci, _bersihkan(nilai))
