"""Alat ukur stabilisasi runtime Hermes lewat API backend UIR.

Skrip ini HANYA mengukur: mengirim pertanyaan ke `/api/chat`, mencatat latency,
token, jumlah langkah, sitasi, dan jejak tool dari API sesi Hermes. Ia tidak
menjalankan agent loop dan tidak pernah mencetak kredensial.

Contoh:
    python tests/ukur_stabilisasi.py --fasa sebelum --mode dasar
    python tests/ukur_stabilisasi.py --fasa setelah --mode konkurensi --pekerja 4
    python tests/ukur_stabilisasi.py --fasa setelah --mode isolasi
"""

import argparse
import concurrent.futures
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_AKAR = Path(__file__).resolve().parents[1]
if str(_AKAR) not in sys.path:
    sys.path.insert(0, str(_AKAR))

from app.env_loader import muat_env  # noqa: E402

PERTANYAAN = [
    "Apa visi UIR?",
    "Siapa rektor UIR saat ini?",
    "Di mana lokasi kampus utama UIR?",
    "Apa saja program studi di UIR?",
]


def _post(url, data, timeout=240):
    badan = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=badan, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _get(url, timeout=60, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _token(usage):
    if not isinstance(usage, dict):
        return 0
    for kunci in ("total_tokens", "total"):
        if isinstance(usage.get(kunci), (int, float)):
            return int(usage[kunci])
    return int(sum(
        usage.get(k) or 0
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        if isinstance(usage.get(k), (int, float))
    ))


def _statistik(nilai):
    if not nilai:
        return {"n": 0, "rata": 0, "median": 0, "p95": 0, "min": 0, "maks": 0}
    urut = sorted(nilai)
    indeks = min(len(urut) - 1, int((0.95 * (len(urut) - 1)) + 0.999999))
    return {
        "n": len(urut),
        "rata": round(statistics.fmean(urut), 2),
        "median": round(statistics.median(urut), 2),
        "p95": round(urut[indeks], 2),
        "min": round(urut[0], 2),
        "maks": round(urut[-1], 2),
    }


def _sesi_hermes(kunci, api_url="http://127.0.0.1:8642"):
    """Ambil daftar ringkas sesi Hermes untuk pencocokan sebelum/sesudah."""
    try:
        data = _get(
            api_url + "/api/sessions",
            headers={"Authorization": "Bearer " + kunci},
        )
    except Exception:
        return {}
    return {
        str(item.get("id")): item
        for item in (data.get("data") or [])
        if isinstance(item, dict)
    }


def _jejak_tool(sesi_id, kunci, api_url="http://127.0.0.1:8642"):
    """Hitung tool asli dan kesalahan argumen dari transkrip satu sesi."""
    kosong = {"tool": [], "deskripsi": 0, "argumen_gagal": 0, "pesan": 0}
    try:
        data = _get(
            api_url + "/api/sessions/" + urllib.parse.quote(sesi_id, safe="")
            + "/messages",
            headers={"Authorization": "Bearer " + kunci},
        )
    except Exception:
        return kosong
    pesan = data.get("data") if isinstance(data, dict) else data
    if not isinstance(pesan, list):
        return kosong
    kosong["pesan"] = len(pesan)
    for item in pesan:
        if not isinstance(item, dict):
            continue
        nama = str(item.get("tool_name") or "")
        if nama == "tool_describe":
            kosong["deskripsi"] += 1
        if nama.startswith("mcp__uir__") and nama not in kosong["tool"]:
            kosong["tool"].append(nama)
        isi = str(item.get("content") or "")
        if "missing required argument" in isi.lower():
            kosong["argumen_gagal"] += 1
    return kosong


def _satu_chat(port, pertanyaan, session_id=None):
    mulai = time.monotonic()
    data = {"question": pertanyaan}
    if session_id:
        data["session_id"] = session_id
    try:
        hasil = _post(f"http://127.0.0.1:{port}/api/chat", data)
        http_gagal = ""
    except urllib.error.HTTPError as exc:
        hasil = json.loads(exc.read().decode("utf-8", "replace"))
        http_gagal = "HTTP " + str(exc.code)
    except Exception as exc:
        hasil = {"status": "gagal_koneksi", "pesan": str(exc)[:160]}
        http_gagal = "gagal_koneksi"
    return {
        "pertanyaan": pertanyaan,
        "latency": round(time.monotonic() - mulai, 2),
        "status": hasil.get("status", ""),
        "ok": bool(hasil.get("ok")),
        "http_gagal": http_gagal,
        "langkah": hasil.get("langkah", 0),
        "jawaban_panjang": len(str(hasil.get("jawaban") or "")),
        "sumber": [s.get("url") for s in (hasil.get("sumber") or []) if isinstance(s, dict)],
        "cited": sum(
            1 for d in (hasil.get("status_sumber") or {}).values()
            if isinstance(d, dict) and d.get("cited")
        ),
        "token": _token((hasil.get("detail_model") or {}).get("usage")),
        "model": (hasil.get("detail_model") or {}).get("model", ""),
        "run_id": (hasil.get("detail_model") or {}).get("run_id", ""),
        "session_id": hasil.get("session_id", ""),
        "pesan": str(hasil.get("pesan") or "")[:300],
    }


def _jalan_dasar(port, kunci, api_url):
    catatan = []
    for pertanyaan in PERTANYAAN:
        sebelum = _sesi_hermes(kunci, api_url)
        hasil = _satu_chat(port, pertanyaan)
        sesudah = _sesi_hermes(kunci, api_url)
        baru = [k for k in sesudah if k not in sebelum]
        hasil["jejak"] = _jejak_tool(baru[0], kunci) if baru else {}
        catatan.append(hasil)
        print(
            f"DASAR {pertanyaan[:28]:28} {hasil['latency']:6.2f}s "
            f"token={hasil['token']:6} langkah={hasil['langkah']} "
            f"cited={hasil['cited']} tool={','.join(hasil['jejak'].get('tool', []))}"
        )
    return catatan


def _jalan_konkurensi(port, kunci, pekerja):
    def kerja(indeks):
        return _satu_chat(port, PERTANYAAN[indeks % len(PERTANYAAN)])

    dengan = concurrent.futures.ThreadPoolExecutor(max_workers=pekerja)
    mulai = time.monotonic()
    with dengan:
        catatan = list(dengan.map(kerja, range(pekerja)))
    total = round(time.monotonic() - mulai, 2)
    print(
        f"KONKURENSI pekerja={pekerja} total={total}s "
        f"lulus={sum(1 for x in catatan if x['ok'])}/{pekerja} "
        f"id_unik={len({x['session_id'] for x in catatan})} "
        f"token_total={sum(x['token'] for x in catatan)}"
    )
    return {"pekerja": pekerja, "total": total, "catatan": catatan}


def _jalan_isolasi(port):
    a = _satu_chat(port, "Apa visi UIR?")
    lanjutan = _satu_chat(port, "Apa targets tahun visi itu?", a["session_id"])
    b = _satu_chat(port, "Siapa rektor UIR saat ini?")
    kembali = _satu_chat(port, "Apa visi UIR?", a["session_id"])
    hasil = {
        "a": a, "lanjutan": lanjutan, "b": b, "kembali": kembali,
        "id_berbeda": a["session_id"] != b["session_id"],
        "id_sama": a["session_id"] == lanjutan["session_id"] == kembali["session_id"],
        "semua_berhasil": all(x["ok"] for x in (a, lanjutan, b, kembali)),
    }
    print(
        f"ISOLASI id_berbeda={hasil['id_berbeda']} id_sama={hasil['id_sama']} "
        f"semua_berhasil={hasil['semua_berhasil']}"
    )
    return hasil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8013)
    parser.add_argument("--api-url", default="http://127.0.0.1:8642")
    parser.add_argument("--fasa", default="sebelum")
    parser.add_argument("--mode", default="dasar", choices=["dasar", "konkurensi", "isolasi"])
    parser.add_argument("--pekerja", type=int, default=4)
    args = parser.parse_args()

    muat_env()
    kunci = os.environ.get("HERMES_API_KEY", "")
    if not kunci:
        raise SystemExit("HERMES_API_KEY belum tersedia di .env/environment.")

    if args.mode == "dasar":
        isi = _jalan_dasar(args.port, kunci, args.api_url.rstrip("/"))
    elif args.mode == "konkurensi":
        isi = _jalan_konkurensi(args.port, kunci, args.pekerja)
    else:
        isi = _jalan_isolasi(args.port)

    if args.mode in ("dasar", "konkurensi"):
        catatan = isi if isinstance(isi, list) else isi["catatan"]
        ringkasan = {
            "latency": _statistik([x["latency"] for x in catatan]),
            "token": _statistik([x["token"] for x in catatan]),
            "berhasil": sum(1 for x in catatan if x["ok"]),
            "total": len(catatan),
            "rata_cited": round(
                statistics.fmean([x["cited"] for x in catatan]), 2
            ) if catatan else 0,
            "rata_langkah": round(
                statistics.fmean([x["langkah"] for x in catatan]), 2
            ) if catatan else 0,
        }
        print("RINGKASAN", json.dumps(ringkasan, ensure_ascii=False))

    keluaran = {
        "waktu": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fasa": args.fasa,
        "mode": args.mode,
        "port": args.port,
        "data": isi,
    }
    tujuan = _AKAR / "docs" / f"bukti_stabilisasi_{args.fasa}_{args.mode}.json"
    tujuan.write_text(
        json.dumps(keluaran, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("BUKTI", tujuan.name)


if __name__ == "__main__":
    main()
