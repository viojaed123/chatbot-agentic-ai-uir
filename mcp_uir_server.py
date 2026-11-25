"""Server MCP untuk tool UIR yang dijalankan oleh Hermes Agent.

Server ini tidak menjalankan agent loop. Ia hanya membungkus tiga tool yang
sudah ada di proyek sebagai tool MCP, sehingga reasoning, planning, tool
calling, dan eksekusi tetap dilakukan oleh Hermes Agent.

Semua tool bersifat baca/baca-cache dan memakai validasi URL yang sama dengan
implementasi lama: hanya HTTPS hostname tepat ``uir.ac.id`` yang diizinkan.
"""

import json
import sys
from pathlib import Path

_AKAR = Path(__file__).resolve().parent
if str(_AKAR) not in sys.path:
    sys.path.insert(0, str(_AKAR))

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    # MCP SDK 2.x mengganti FastMCP dengan MCPServer; API dekorator tool tetap sama.
    from mcp.server.mcpserver import MCPServer as FastMCP

from tools.ambil_halaman import ambil_halaman
from tools.baca_cache import baca_cache
from tools.cari_uir import cari_uir

mcp = FastMCP("uir")


def _json(data):
    """Kembalikan tool result sebagai JSON yang aman dan ringkas."""
    return json.dumps(data, ensure_ascii=False, default=str)


def _potong(teks, batas=3000):
    if not isinstance(teks, str):
        return ""
    if len(teks) <= batas:
        return teks
    return teks[:batas].rstrip() + "… [dipotong]"


@mcp.tool(
    name="cari_uir",
    description=(
        "Cari halaman publik resmi Universitas Islam Riau di domain uir.ac.id. "
        "Argumen WAJIB: query (string kata kunci singkat), jangan kosong. "
        "Contoh: {\"query\": \"visi UIR\"}. "
        "Satu pencarian mencakup REST dan sitemap; jangan ulangi dengan kata kunci serupa. "
        "Hasil hanya berupa petunjuk URL dan cuplikan; belum merupakan bukti isi. "
        "Gunakan baca_cache atau ambil_halaman sebelum menyatakan fakta."
    ),
)
def cari_uir_tool(query: str) -> str:
    hasil = cari_uir(query)
    if not isinstance(hasil, dict):
        return _json({"ok": False, "status": "respons_tidak_valid", "url": "", "pesan": "Hasil pencarian tidak valid."})

    kandidat = []
    for item in hasil.get("results") or []:
        if not isinstance(item, dict):
            continue
        kandidat.append({
            "judul": str(item.get("judul") or ""),
            "url": str(item.get("url") or ""),
            "cuplikan": _potong(item.get("cuplikan"), 400),
            "jenis": str(item.get("jenis") or "halaman"),
            "tanggal": str(item.get("tanggal") or ""),
        })

    # URL sengaja diletakkan lebih awal agar mudah diverifikasi dari event tool.
    return _json({
        "url": kandidat[0]["url"] if kandidat else "",
        "ok": bool(hasil.get("ok")),
        "status": str(hasil.get("status") or ""),
        "query": str(hasil.get("query") or ""),
        "count": int(hasil.get("count") or 0),
        "pesan": str(hasil.get("pesan") or ""),
        "results": kandidat[:8],
    })


@mcp.tool(
    name="ambil_halaman",
    description=(
        "Ambil dan ekstrak teks bermakna dari satu halaman publik uir.ac.id. "
        "Argumen WAJIB: url (URL HTTPS https://uir.ac.id/...), jangan kosong. "
        "Contoh: {\"url\": \"https://uir.ac.id/profil/\"}. "
        "Patuhi robots.txt dan hanya terima HTTPS hostname tepat uir.ac.id."
    ),
)
def ambil_halaman_tool(url: str) -> str:
    hasil = ambil_halaman(url)
    if not isinstance(hasil, dict):
        return _json({"ok": False, "status": "respons_tidak_valid", "url": str(url), "pesan": "Hasil halaman tidak valid."})

    return _json({
        "url": str(hasil.get("url") or ""),
        "ok": bool(hasil.get("ok")),
        "status": str(hasil.get("status") or ""),
        "judul": str(hasil.get("judul") or ""),
        "potongan": _potong(hasil.get("teks")),
        "panjang": int(hasil.get("panjang") or 0),
        "pesan": str(hasil.get("pesan") or ""),
    })


@mcp.tool(
    name="baca_cache",
    description=(
        "Baca halaman uir.ac.id dari cache lokal bila masih berlaku. "
        "Argumen WAJIB: url (URL HTTPS https://uir.ac.id/...), jangan kosong. "
        "Contoh: {\"url\": \"https://uir.ac.id/profil/\"}. "
        "Tidak mengakses internet. Gunakan sebelum ambil_halaman bila memungkinkan."
    ),
)
def baca_cache_tool(url: str) -> str:
    hasil = baca_cache(url)
    if not isinstance(hasil, dict):
        return _json({"ok": False, "status": "respons_tidak_valid", "url": str(url), "pesan": "Hasil cache tidak valid."})

    data = hasil.get("data")
    if not isinstance(data, dict):
        data = {}
    return _json({
        "url": str(hasil.get("url") or data.get("url") or ""),
        "ok": bool(hasil.get("ok")),
        "status": str(hasil.get("status") or ""),
        "judul": str(data.get("judul") or ""),
        "potongan": _potong(data.get("teks")),
        "panjang": int(data.get("panjang") or 0),
        "waktu_cache": str(hasil.get("waktu_cache") or ""),
        "kedaluwarsa": str(hasil.get("kedaluwarsa_pada") or ""),
        "pesan": str(hasil.get("pesan") or ""),
    })


if __name__ == "__main__":
    mcp.run(transport="stdio")
