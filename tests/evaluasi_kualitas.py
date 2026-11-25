"""Evaluasi kualitas jawaban dan sitasi dari berkas bukti stabilisasi.

Skrip ini tidak memanggil model. Ia mengambil ``output`` final dari status run
Hermes yang sudah tercatat, lalu memeriksa apakah setiap URL di jawaban benar-
benar berasal dari pesan tool ``mcp__uir__*`` (field ``status_sumber`` pada bukti
backend). Kredensial tidak pernah dicetak.

Contoh:
    python tests/evaluasi_kualitas.py --bukti docs/bukti_stabilisasi_sebelum_dasar.json
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_AKAR = Path(__file__).resolve().parents[1]
if str(_AKAR) not in sys.path:
    sys.path.insert(0, str(_AKAR))

from app.env_loader import muat_env  # noqa: E402

_URL = re.compile(r"https://uir\.ac\.id/[^\s\"'<>)\\},\\]+", re.IGNORECASE)


def _status_run(run_id, kunci, api_url):
    if not run_id:
        return {}
    req = urllib.request.Request(
        api_url.rstrip("/") + "/v1/runs/" + urllib.parse.quote(run_id, safe=""),
        headers={"Authorization": "Bearer " + kunci},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _bersihkan(url):
    return url.rstrip(".,;\\`")


def evaluasi(bukti, kunci, api_url):
    hasil = []
    for catatan in bukti.get("data", []):
        status = _status_run(catatan.get("run_id", ""), kunci, api_url)
        jawaban = str(status.get("output") or "")
        url_jawaban = [_bersihkan(u) for u in _URL.findall(jawaban)]
        # ``sumber`` pada bukti backend sudah difilter dari status_sumber yang
        # hanya berisi URL hasil tool mcp__uir__*.
        sumber = set(catatan.get("sumber") or [])
        semua_tersumber = bool(url_jawaban) and all(
            u in sumber for u in url_jawaban
        )
        tidak_ada_url_asing = not re.search(
            r"https?://(?!uir\.ac\.id\b)[^\s\"'<>)\\},\\]+", jawaban, re.IGNORECASE
        )
        cited = int(catatan.get("cited") or 0) > 0
        cukup = len(jawaban.strip()) >= 60
        lolos = semua_tersumber and tidak_ada_url_asing and cited and cukup
        hasil.append({
            "pertanyaan": catatan.get("pertanyaan", ""),
            "run_id": catatan.get("run_id", ""),
            "panjang_jawaban": len(jawaban.strip()),
            "url_jawaban": url_jawaban,
            "url_tersumber": sorted(sumber),
            "semua_url_tersumber": semua_tersumber,
            "tanpa_url_asing": tidak_ada_url_asing,
            "ada_sitasi_terverifikasi": cited,
            "lolos": lolos,
        })
    return hasil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bukti", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8642")
    parser.add_argument("--keluaran", default="")
    args = parser.parse_args()

    muat_env()
    kunci = os.environ.get("HERMES_API_KEY", "")
    if not kunci:
        raise SystemExit("HERMES_API_KEY belum tersedia di .env/environment.")
    bukti = json.loads(Path(args.bukti).read_text(encoding="utf-8"))
    hasil = evaluasi(bukti, kunci, args.api_url)
    lolos = sum(1 for x in hasil if x["lolos"])
    ringkasan = {
        "bukti": str(args.bukti),
        "jumlah": len(hasil),
        "lolos": lolos,
        "akurasi_persentase": round(100 * lolos / len(hasil), 1) if hasil else 0,
        "rata_url_per_jawaban": round(
            sum(len(x["url_jawaban"]) for x in hasil) / len(hasil), 2
        ) if hasil else 0,
    }
    for item in hasil:
        print(
            f"{'LOLOS' if item['lolos'] else 'GAGAL'} "
            f"{item['pertanyaan'][:32]:32} url={len(item['url_jawaban'])} "
            f"tersumber={item['semua_url_tersumber']} sitasi={item['ada_sitasi_terverifikasi']}"
        )
    print("RINGKASAN", json.dumps(ringkasan, ensure_ascii=False))
    if args.keluaran:
        Path(args.keluaran).write_text(
            json.dumps({"ringkasan": ringkasan, "data": hasil}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
