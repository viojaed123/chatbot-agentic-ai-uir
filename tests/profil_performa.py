"""Profiler latency 9 tahap untuk Chatbot Agentic AI UIR.

Skrip ini mengukur alur nyata lewat backend UIR, lalu membaca replay event SSE
Hermes untuk run yang bersangkutan. Ia TIDAK menjalankan agent loop sendiri,
TIDAK mengubah jawaban, dan TIDAK pernah menulis kredensial ke file keluaran.

Sembilan tahap yang dicatat:

1. klien/frontend -> backend menerima request
2. backend -> Hermes menerima run (POST /v1/runs selesai)
3. menunggu model sampai event Hermes pertama teramati
4. reasoning model yang terlihat lewat event ``reasoning.available``
5. setiap pemanggilan MCP (dari event tool.started/tool.completed)
6. retrieval/pengambilan halaman (total tool UIR)
7. polling status Hermes
8. penyusunan jawaban akhir dan verifikasi sitasi
9. backend -> klien/frontend menerima jawaban

Catatan: tahap 3-7 terjadi di dalam proses Hermes dan sebagian saling
menimpa dengan polling. Jumlah kedua belas angka di bawah bukan latency total;
latency total diambil dari waktu klien.

Contoh:
    python tests/profil_performa.py --fasa sebelum --ulang 6
    python tests/profil_performa.py --fasa sesudah --mode konkurensi --pekerja 4
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
    ("Apa visi UIR?", "visi"),
    ("Siapa rektor UIR saat ini?", "rektor"),
    ("Di mana lokasi kampus utama UIR?", "kampus"),
    ("Apa saja program studi di UIR?", "program"),
]

_EVENT_PERTAMA = ("reasoning.available", "tool.started", "message.interim", "run.completed")
_ALAT_UIR = ("cari_uir", "ambil_halaman", "baca_cache")


def _sesaat():
    return round(time.time(), 3)


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
        return resp.read().decode("utf-8", "replace")


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
    nilai = [float(x) for x in nilai if isinstance(x, (int, float))]
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


def _selisih(akhir, awal):
    if not isinstance(akhir, (int, float)) or not isinstance(awal, (int, float)):
        return None
    return round(max(0.0, float(akhir) - float(awal)), 3)


def _peristiwa(run_id, kunci, api_url):
    """Ambil ringkasan event SSE tanpa menyimpan isi teks/halaman."""
    kosong = {
        "event": [], "alat": [], "jeda_model": [], "reasoning": 0,
        "reasoning_detik": 0, "run_status": "", "token": 0,
    }
    if not run_id:
        return kosong
    try:
        badan = _get(
            api_url + "/v1/runs/" + urllib.parse.quote(run_id, safe="") + "/events",
            timeout=30,
            headers={"Authorization": "Bearer " + kunci, "Accept": "text/event-stream"},
        )
    except Exception as exc:
        kosong["event"].append({"event": "gagal_baca_sse", "jenis": type(exc).__name__})
        return kosong

    mentah = []
    for baris in badan.splitlines():
        if baris.startswith("data: "):
            try:
                mentah.append(json.loads(baris[6:]))
            except (ValueError, TypeError):
                continue

    for item in mentah:
        if not isinstance(item, dict):
            continue
        nama = str(item.get("event") or "")
        if not nama:
            continue
        rekam = {
            "event": nama,
            "timestamp": round(float(item.get("timestamp") or 0), 3),
            "seq": item.get("seq"),
        }
        if "tool" in item:
            rekam["tool"] = str(item.get("tool") or "")
        if "tool_call_id" in item:
            rekam["tool_call_id"] = str(item.get("tool_call_id") or "")
        if "duration" in item:
            rekam["duration"] = round(float(item.get("duration") or 0), 3)
        if "error" in item:
            rekam["error"] = bool(item.get("error"))
        for kunci_panjang in ("text", "preview", "output"):
            if kunci_panjang in item:
                rekam[kunci_panjang + "_panjang"] = len(str(item.get(kunci_panjang) or ""))
        kosong["event"].append(rekam)
        if nama == "run.completed":
            kosong["run_status"] = "completed"
        elif nama == "run.failed":
            kosong["run_status"] = "failed"

    alasan = [e for e in kosong["event"] if e["event"] == "reasoning.available"]
    kosong["reasoning"] = len(alasan)
    if len(alasan) >= 2:
        kosong["reasoning_detik"] = round(
            max(0.0, alasan[-1]["timestamp"] - alasan[0]["timestamp"]), 3
        )

    # Event MCP Hermes tidak mengirim ``duration`` untuk tool UIR. Pasangkan
    # started -> completed berdasarkan urutan dan hitung dari timestamp SSE.
    menunggu = {}
    for e in kosong["event"]:
        if e["event"] == "tool.started":
            kunci_pasang = e.get("tool_call_id") or e.get("tool", "")
            menunggu.setdefault(kunci_pasang, []).append(e["timestamp"])
        elif e["event"] == "tool.completed":
            alat = e.get("tool", "")
            kunci_pasang = e.get("tool_call_id") or alat
            awal = menunggu.get(kunci_pasang) or []
            if not awal and kunci_pasang != alat:
                awal = menunggu.get(alat) or []
            if awal:
                t0 = awal.pop(0)
                if any(uk in alat for uk in _ALAT_UIR):
                    kosong["alat"].append({
                        "tool": alat,
                        "detik": _selisih(e["timestamp"], t0),
                        "gagal": bool(e.get("error")),
                    })

    # Jeda dari tool selesai ke tool berikutnya adalah waktu reasoning model
    # yang tidak diekspos Hermes sebagai event reasoning.available.
    selesai_sementara = None
    untuk_model = []
    for e in kosong["event"]:
        if e["event"] == "tool.completed":
            selesai_sementara = e["timestamp"]
        elif e["event"] == "tool.started" and selesai_sementara is not None:
            untuk_model.append({
                "dari": "tool.completed", "ke": "tool.started",
                "detik": _selisih(e["timestamp"], selesai_sementara),
            })
            selesai_sementara = None
        elif e["event"] in ("run.completed", "run.failed"):
            if selesai_sementara is not None:
                untuk_model.append({
                    "dari": "tool.completed", "ke": e["event"],
                    "detik": _selisih(e["timestamp"], selesai_sementara),
                })
            selesai_sementara = None
    kosong["jeda_model"] = untuk_model

    usage = None
    for item in mentah:
        if isinstance(item, dict) and item.get("event") in ("run.completed", "run.failed"):
            cal = item.get("usage")
            if isinstance(cal, dict):
                usage = cal
    if usage:
        kosong["token"] = _token(usage)
    return kosong


def _satu_chat(port, pertanyaan, kunci, api_url, session_id=None):
    data = {"question": pertanyaan}
    if session_id:
        data["session_id"] = session_id
    awal = _sesaat()
    awal_m = time.monotonic()
    try:
        hasil = _post("http://127.0.0.1:%d/api/chat" % port, data)
        galat = ""
    except urllib.error.HTTPError as exc:
        hasil = json.loads(exc.read().decode("utf-8", "replace"))
        galat = "HTTP " + str(exc.code)
    except Exception as exc:
        hasil = {"status": "gagal_koneksi", "pesan": type(exc).__name__}
        galat = "gagal_koneksi"
    akhir = _sesaat()
    latency = round(time.monotonic() - awal_m, 3)

    perf = hasil.get("performansi") if isinstance(hasil.get("performansi"), dict) else {}
    penanda = perf.get("penanda_waktu") if isinstance(perf.get("penanda_waktu"), dict) else {}
    rinci = hasil.get("detail_model") if isinstance(hasil.get("detail_model"), dict) else {}
    run_id = str(rinci.get("run_id") or "")
    events = _peristiwa(run_id, kunci, api_url)

    post_selesai = penanda.get("hermes_run_post_selesai")
    event_pertama = None
    for item in events["event"]:
        if item["event"] in _EVENT_PERTAMA:
            event_pertama = item["timestamp"]
            break

    jeda = events["jeda_model"]
    model_jeda = round(sum(float(x["detik"] or 0) for x in jeda), 3)
    model_akhir = round(sum(
        float(x["detik"] or 0) for x in jeda
        if str(x["ke"]).startswith("run.")
    ), 3)

    status_sumber = hasil.get("status_sumber") if isinstance(hasil.get("status_sumber"), dict) else {}
    cited = sum(
        1 for nilai in status_sumber.values()
        if isinstance(nilai, dict) and nilai.get("cited")
    )
    sumber = [s.get("url") for s in (hasil.get("sumber") or []) if isinstance(s, dict)]
    jawaban = str(hasil.get("jawaban") or "")
    tahapan = perf.get("tahapan") if isinstance(perf.get("tahapan"), dict) else {}
    polling_http = tahapan.get("polling_status") or {}

    ukuran = {
        "tahap1_frontend_ke_backend": _selisih(penanda.get("api_terima"), awal),
        "tahap2_backend_ke_run": _selisih(post_selesai, penanda.get("api_terima")),
        "tahap3_tunggu_model": _selisih(event_pertama, post_selesai),
        "tahap4_reasoning": events["reasoning_detik"] if events["reasoning"] else 0,
        "tahap4_model_jeda": model_jeda,
        "tahap5_mcp_total": round(sum(a["detik"] for a in events["alat"]), 3),
        "tahap6_retrieval": round(sum(
            a["detik"] for a in events["alat"]
            if any(uk in a["tool"] for uk in _ALAT_UIR)
        ), 3),
        "tahap7_polling": _selisih(
            penanda.get("hermes_polling_selesai"), penanda.get("hermes_polling_mulai")
        ),
        "tahap7_polling_http": round(float(polling_http.get("total_detik") or 0), 3),
        "tahap8a_model_akhir": model_akhir,
        "tahap8b_backend": _selisih(
            penanda.get("hermes_jawaban_siap"), penanda.get("hermes_status_akhir")
        ),
        "tahap9_backend_ke_frontend": _selisih(akhir, penanda.get("respons_kirim")),
    }

    return {
        "pertanyaan": pertanyaan,
        "klien_mulai": awal,
        "klien_selesai": akhir,
        "latency": latency,
        "status": str(hasil.get("status") or ""),
        "ok": bool(hasil.get("ok")),
        "galat_http": galat,
        "trace_id": str(hasil.get("trace_id") or perf.get("trace_id") or ""),
        "session_id": str(hasil.get("session_id") or ""),
        "run_id": run_id,
        "token": _token(rinci.get("usage")),
        "langkah": hasil.get("langkah", 0),
        "model": str(rinci.get("model") or ""),
        "jawaban_panjang": len(jawaban),
        "sumber": sumber,
        "cited": cited,
        "status_sumber": status_sumber,
        "polling_count": perf.get("polling", 0),
        "tahapan": ukuran,
        "telemetri_ringkas": {
            "paling_lambat": perf.get("paling_lambat", []),
            "penanda": perf.get("penanda", {}),
            "sse": int(perf.get("sse") or 0),
            "sse_gagal": int(perf.get("sse_gagal") or 0),
            "polling": int(perf.get("polling") or 0),
        },
        "event": events,
        "pesan": str(hasil.get("pesan") or "")[:200],
    }


def _ringkas(catatan, kunci_akar):
    lat = [x["latency"] for x in catatan]
    hasil = {
        "n": len(catatan),
        "berhasil": sum(1 for x in catatan if x["ok"]),
        "latency": _statistik(lat),
        "token": _statistik([x["token"] for x in catatan]),
        "rata_cited": round(statistics.fmean([x["cited"] for x in catatan]), 2) if catatan else 0,
        "rata_langkah": round(statistics.fmean([x["langkah"] for x in catatan]), 2) if catatan else 0,
        "trace_unik": len({x["trace_id"] for x in catatan if x["trace_id"]}),
    }
    for nama in (
        "tahap1_frontend_ke_backend", "tahap2_backend_ke_run", "tahap3_tunggu_model",
        "tahap4_reasoning", "tahap4_model_jeda", "tahap5_mcp_total", "tahap6_retrieval",
        "tahap7_polling", "tahap7_polling_http", "tahap8a_model_akhir",
        "tahap8b_backend", "tahap9_backend_ke_frontend",
    ):
        hasil[nama] = _statistik([x["tahapan"].get(nama) for x in catatan])
    hasil["tahap5_jumlah_panggilan"] = _statistik([
        len(x["event"]["alat"]) for x in catatan
    ])
    hasil["tahap5_gagal"] = sum(
        1 for x in catatan for alat in x["event"]["alat"] if alat["gagal"]
    )
    hasil["tahap3_nama"] = "run diterima -> event Hermes pertama (proxy tunggu model)"
    hasil["tahap4_nama"] = "reasoning: event reasoning.available atau jeda antar tool"
    hasil["tahap7_nama"] = "polling wall-clock (berimbrikan dengan tahap 3-6)"
    hasil["tahap7_http_nama"] = "overhead HTTP GET polling saja"
    hasil["tahap8_nama"] = "model menyusun jawaban akhir + backend verifikasi sitasi"
    return hasil


def _simpan(nama, isi):
    tujuan = _AKAR / "docs" / nama
    tujuan.parent.mkdir(parents=True, exist_ok=True)
    tujuan.write_text(
        json.dumps(isi, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("TERSIMPAN", tujuan.relative_to(_AKAR))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fasa", default="sebelum", choices=["sebelum", "sesudah"])
    parser.add_argument("--mode", default="dasar", choices=["dasar", "konkurensi", "isolasi"])
    parser.add_argument("--port", type=int, default=8013)
    parser.add_argument("--api-url", default="http://127.0.0.1:8642")
    parser.add_argument("--ulang", type=int, default=6)
    parser.add_argument("--pekerja", type=int, default=4)
    args = parser.parse_args()

    muat_env()
    kunci = os.environ.get("HERMES_API_KEY", "")
    if not kunci:
        raise SystemExit("HERMES_API_KEY belum tersedia di .env/environment.")
    api_url = args.api_url.rstrip("/")

    if args.mode == "dasar":
        catatan = []
        for i in range(args.ulang):
            pertanyaan, _ = PERTANYAAN[i % len(PERTANYAAN)]
            hasil = _satu_chat(args.port, pertanyaan, kunci, api_url)
            catatan.append(hasil)
            print(
                "SAMPEL %02d %-34s %6.2fs trace=%s token=%d tool=%d cited=%d"
                % (
                    i + 1, pertanyaan[:34], hasil["latency"], hasil["trace_id"],
                    hasil["token"], len(hasil["event"]["alat"]), hasil["cited"],
                )
            )
    elif args.mode == "konkurensi":
        def kerja(i):
            pertanyaan, _ = PERTANYAAN[i % len(PERTANYAAN)]
            return _satu_chat(args.port, pertanyaan, kunci, api_url)

        awal = _sesaat()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.pekerja) as kolam:
            catatan = list(kolam.map(kerja, range(args.pekerja)))
        print(
            "KONKURENSI total=%.2fs berhasil=%d/%d id_unik=%d"
            % (
                _sesaat() - awal, sum(1 for x in catatan if x["ok"]), len(catatan),
                len({x["session_id"] for x in catatan}),
            )
        )
    else:
        a = _satu_chat(args.port, PERTANYAAN[0][0], kunci, api_url)
        lanjutan = _satu_chat(
            args.port, "Apa targets tahun visi itu?", kunci, api_url, a["session_id"]
        )
        b = _satu_chat(args.port, PERTANYAAN[1][0], kunci, api_url)
        kembali = _satu_chat(
            args.port, PERTANYAAN[0][0], kunci, api_url, a["session_id"]
        )
        catatan = [a, lanjutan, b, kembali]
        print(
            "ISOLASI id_berbeda=%s id_sama=%s semua_berhasil=%s"
            % (
                a["session_id"] != b["session_id"],
                a["session_id"] == lanjutan["session_id"] == kembali["session_id"],
                all(x["ok"] for x in catatan),
            )
        )

    ringkas = _ringkas(catatan, "catatan")
    ringkas["waktu"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ringkas["fasa"] = args.fasa
    ringkas["mode"] = args.mode
    ringkas["port"] = args.port
    print("RINGKASAN", json.dumps({
        "latency": ringkas["latency"],
        "token": ringkas["token"],
        "berhasil": ringkas["berhasil"],
        "tahap3": ringkas["tahap3_tunggu_model"],
        "tahap7": ringkas["tahap7_polling"],
    }, ensure_ascii=False))

    cap = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _simpan(
        "profil_performa_%s_%s_%s.json" % (args.fasa, args.mode, cap),
        {"ringkasan": ringkas, "data": catatan},
    )
    _simpan("profil_ringkasan_%s_%s.json" % (args.fasa, args.mode), ringkas)


if __name__ == "__main__":
    main()
