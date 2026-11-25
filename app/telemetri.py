"""Instrumentasi waktu dan jumlah panggilan untuk satu permintaan.

Modul ini sengaja memakai ``threading.local`` supaya dua sesi yang berjalan
bersamaan tidak saling menimpa data. Hanya angka, nama tool, dan status yang
dicatat; isi pertanyaan, jawaban, serta teks halaman TIDAK pernah disimpan.

Pemakaian singkat::

    from app import telemetri

    telemetri.mulai()
    try:
        ...  # alur normal
    finally:
        ringkasan = telemetri.selesai()

Fungsi ``ukur`` dan ``tambah`` diam-diam tidak melakukan apa pun bila belum
ada sesi telemetri, sehingga aman dipanggil dari unit test dan mode CLI.
"""

import secrets
import threading
import time
from contextlib import contextmanager

_KONTEKS = threading.local()


def mulai(trace_id=None):
    """Mulai satu rekaman telemetri untuk thread saat ini.

    ``trace_id`` hanya penanda acak untuk menyatukan ukuran frontend, backend,
    dan event Hermes. Ia bukan session ID dan tidak mengandung kredensial.
    """
    data = {
        "mulai": time.perf_counter(),
        "trace_id": str(trace_id or secrets.token_hex(8)),
        "tahapan": {},
        "panggilan_model": 0,
        "jalur_cepat": 0,
        "langkah": 0,
        "polling": 0,
        "sse": 0,
        "sse_gagal": 0,
        "tool": {},
        "cache_hit": 0,
        "cache_miss": 0,
        "pencarian_cache_hit": 0,
        "halaman_dicari": 0,
        "token_prompt": 0,
        "token_completion": 0,
        "token_tersedia": False,
        "penanda": {},
        "penanda_waktu": {},
        "api": {
            "penyedia": "",
            "model": "",
            "panggilan": 0,
            "token_prompt": 0,
            "token_completion": 0,
            "biaya_estimasi_usd": 0.0,
        },
        # Rincian dibedakan per pemanggilan, bukan hanya agregat.
        "rincian": [],
        "per_tool": {},
    }
    _KONTEKS.data = data
    return data


def aktif():
    """Kembalikan data telemetri thread saat ini, atau None."""
    return getattr(_KONTEKS, "data", None)


def tandai(nama):
    """Catat penanda relatif dan absolut untuk satu permintaan.

    Relatif dipakai analisis dalam proses; absolut (epoch UTC dalam detik)
    dipakai profiler untuk menyatukan waktu klien, backend, dan event Hermes
    yang berasal dari proses berbeda pada mesin yang sama.
    """
    data = aktif()
    if data is None:
        return
    data["penanda"][nama] = round(time.perf_counter() - data["mulai"], 3)
    data["penanda_waktu"][nama] = round(time.time(), 3)


def _skalar(nilai):
    """Hanya angka, nama, dan status yang boleh masuk telemetri."""
    return isinstance(nilai, (str, int, float, bool))


@contextmanager
def ukur(nama, **detail):
    """Catat durasi satu tahapan dengan monotonic clock.

    Setiap pemanggilan menghasilkan satu entri di ``rincian`` sehingga
    pemanggilan model/tool yang berulang dapat dibedakan. Detail berupa
    dictionary akan diratakan saat blok selesai; ini dipakai untuk mencatat
    ukuran prompt yang baru diketahui di dalam blok.
    """
    data = aktif()
    if data is None:
        yield
        return

    awal = time.perf_counter()

    try:
        yield
    finally:
        durasi = time.perf_counter() - awal
        entri = data["tahapan"].setdefault(
            nama,
            {"jumlah": 0, "total_detik": 0.0, "maks_detik": 0.0},
        )
        entri["jumlah"] += 1
        entri["total_detik"] += durasi
        entri["maks_detik"] = max(entri["maks_detik"], durasi)

        rincian = {
            "tahap": nama,
            "ke": len(data["rincian"]) + 1,
            "detik": round(durasi, 3),
        }

        for kunci, nilai in detail.items():
            if isinstance(nilai, dict):
                for kunci_dalam, nilai_dalam in nilai.items():
                    if _skalar(nilai_dalam):
                        entri[kunci_dalam] = nilai_dalam
                        rincian[kunci_dalam] = nilai_dalam
            elif _skalar(nilai):
                entri[kunci] = nilai
                rincian[kunci] = nilai

        data["rincian"].append(rincian)

        if nama == "tool":
            alat = str(detail.get("tool", ""))
            if alat:
                entri_alat = data["per_tool"].setdefault(
                    alat,
                    {"jumlah": 0, "total_detik": 0.0, "maks_detik": 0.0},
                )
                entri_alat["jumlah"] += 1
                entri_alat["total_detik"] += durasi
                entri_alat["maks_detik"] = max(
                    entri_alat["maks_detik"], durasi
                )


def tambah(kunci, jumlah=1, kembalikan=False):
    """Tambah penghitung sederhana (aman tanpa sesi aktif).

    Bila ``kembalikan=True``, nilai baru dikembalikan untuk dijadikan nomor
    urut pemanggilan model.
    """
    data = aktif()
    if data is not None and kunci in data:
        data[kunci] += jumlah
        return data[kunci]
    return 0


def nomor_tool(nama):
    """Nomor urut pemanggilan sebuah tool pada permintaan ini."""
    data = aktif()
    if data is None:
        return 0
    entri = data["tool"].setdefault(nama, {"jumlah": 0, "status": ""})
    return entri["jumlah"] + 1


def catat_tool(nama, status=""):
    """Catat satu pemanggilan tool dan statusnya."""
    data = aktif()
    if data is None:
        return

    entri = data["tool"].setdefault(nama, {"jumlah": 0, "status": status})
    entri["jumlah"] += 1
    if status:
        entri["status"] = status


def catat_cache(status):
    """Catat cache hit/miss berdasarkan status tool baca_cache."""
    data = aktif()
    if data is None:
        return

    if status in ("ditemukan", "berhasil"):
        data["cache_hit"] += 1
    elif status:
        data["cache_miss"] += 1


def catat_token(prompt=None, completion=None):
    """Catat jumlah token bila penyedia menyediakannya."""
    data = aktif()
    if data is None:
        return

    try:
        if prompt is not None:
            data["token_prompt"] += int(prompt)
            data["token_tersedia"] = True
        if completion is not None:
            data["token_completion"] += int(completion)
            data["token_tersedia"] = True
    except (TypeError, ValueError):
        return


def catat_api(penyedia="", model="", usage=None, biaya=0.0):
    """Catat pemakaian API cloud: penyedia, model, token, dan estimasi biaya.

    Hanya angka dan nama model yang disimpan. Kredensial tidak pernah
    diterima maupun dicatat oleh fungsi ini.
    """
    data = aktif()
    if data is None:
        return

    api = data["api"]
    api["penyedia"] = str(penyedia or api["penyedia"])
    api["model"] = str(model or api["model"])
    api["panggilan"] += 1

    if isinstance(usage, dict):
        try:
            api["token_prompt"] += int(usage.get("prompt_tokens", 0))
            api["token_completion"] += int(usage.get("completion_tokens", 0))
        except (TypeError, ValueError):
            pass

    try:
        api["biaya_estimasi_usd"] = round(
            api["biaya_estimasi_usd"] + float(biaya or 0), 6
        )
    except (TypeError, ValueError):
        pass


def selesai():
    """Akhiri rekaman dan kembalikan ringkasan yang aman untuk API."""
    data = aktif()
    if data is None:
        return {}

    total = time.perf_counter() - data["mulai"]

    tahapan = {}
    for nama, entri in data["tahapan"].items():
        tahapan[nama] = {
            "jumlah": entri["jumlah"],
            "total_detik": round(entri["total_detik"], 3),
            "maks_detik": round(entri["maks_detik"], 3),
            "rata_detik": round(
                entri["total_detik"] / max(1, entri["jumlah"]), 3
            ),
        }

    paling_lambat = sorted(
        tahapan.items(),
        key=lambda pasangan: pasangan[1]["total_detik"],
        reverse=True,
    )[:3]

    token = None
    if data["token_tersedia"]:
        token = {
            "prompt": data["token_prompt"],
            "completion": data["token_completion"],
            "total": data["token_prompt"] + data["token_completion"],
        }

    per_tool = {}
    for nama, entri in data["per_tool"].items():
        per_tool[nama] = {
            "jumlah": entri["jumlah"],
            "total_detik": round(entri["total_detik"], 3),
            "maks_detik": round(entri["maks_detik"], 3),
            "rata_detik": round(
                entri["total_detik"] / max(1, entri["jumlah"]), 3
            ),
        }

    ringkasan = {
        "total_detik": round(total, 3),
        # handler_detik diukur sampai ringkasan dibuat, sebelum respons dikirim.
        "handler_detik": round(total, 3),
        "trace_id": data["trace_id"],
        "penanda_waktu": dict(data["penanda_waktu"]),
        "polling": data["polling"],
        "sse": data["sse"],
        "sse_gagal": data["sse_gagal"],
        "tahapan": tahapan,
        "paling_lambat": [
            {"nama": nama, **nilai} for nama, nilai in paling_lambat
        ],
        "rincian": data["rincian"],
        "per_tool": per_tool,
        "penanda": data["penanda"],
        "panggilan_model": data["panggilan_model"],
        "jalur_cepat": data["jalur_cepat"],
        "langkah": data["langkah"],
        "halaman_dicari": data["halaman_dicari"],
        "tool": data["tool"],
        "cache": {
            "hit": data["cache_hit"],
            "miss": data["cache_miss"],
            "pencarian_hit": data["pencarian_cache_hit"],
        },
        "token": token,
        "api": dict(data["api"]),
    }

    _KONTEKS.data = None
    return ringkasan
