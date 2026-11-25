# UIR AI Assistant — Antarmuka Custom

Antarmuka web HTML/CSS/JS yang disajikan oleh FastAPI. Frontend **hanya**
berbicara ke `/api/*` pada origin yang sama; ia tidak pernah menghubungi
Hermes, TokenHarbor, tool MCP, atau situs eksternal secara langsung.

Runtime default adalah **Hermes Agent resmi**. `web/api.py` meneruskannya ke
`app/hermes_runtime.py`, lalu ke Hermes API Server. Orkestrator internal tetap
tersedia sebagai rollback `AGENT_RUNTIME=internal`.

## Struktur

```text
frontend/
  index.html                Struktur shell, Beranda, Percakapan, Tentang Sistem
  favicon.svg               Ikon tab lokal, mencegah permintaan favicon.ico 404
  css/tokens.css            Warna (OKLCH), font, spacing, radius
  css/base.css              Reset dan aturan dasar Hallmark
  css/style.css             Tata letak app screen, state, responsive
  js/app.js                 Chat, sesi sementara, render aman, status runtime
  .hallmark/shots/          Screenshot render dan state berjawaban
web/api.py                  Endpoint /api/health, /api/chat, /api/session/reset
app/hermes_runtime.py       Klien API Server Hermes + verifikasi sitasi tool
mcp_uir_server.py           Tiga tool UIR yang dipanggil Hermes lewat MCP
scripts/jalankan_hermes.py  Peluncur gateway Hermes (latar, tanpa kredensial tercetak)
tests/uji_frontend_cdp.js   Uji browser dengan API mock
tests/uji_frontend_cdp_hermes.js Uji browser live tanpa mock
web/app.py                  Antarmuka Streamlit lama (tetap dipertahankan)
```

## Prasyarat

1. Python 3.10 atau lebih baru.
2. `DEEPSEEK_API_KEY` dan `HERMES_API_KEY` tersedia di environment atau `.env`.
   Ollama lokal hanya diperlukan bila memakai rollback internal `MODEL_PROVIDER=ollama`.
3. Source Hermes Agent dan data root dengan `config.yaml` sudah tersedia.

```bash
pip install -r requirements.txt
python scripts/jalankan_hermes.py
python -m uvicorn web.api:app --host 127.0.0.1 --port 8000
```

Jangan menempelkan kunci ke chat, source code, log, atau laporan.

## Konfigurasi

| Variable | Default | Fungsi |
|---|---|---|
| `AGENT_RUNTIME` | `hermes` | `hermes` (utama) atau `internal` (rollback) |
| `HERMES_API_URL` | `http://127.0.0.1:8642` | API Server Hermes |
| `HERMES_API_KEY` | — | Kredensial gateway Hermes lokal |
| `HERMES_TIMEOUT` | `180` | Batas tunggu run (detik) |
| `HERMES_POLL` | `0.3` | Interval polling status run |
| `HERMES_MAX_RETRY` | `2` | Retry GET saat 429/5xx/timeout; POST tidak diulang |
| `DEEPSEEK_API_KEY` | — | Kredensial TokenHarbor |
| `DEEPSEEK_BASE_URL` | `https://tokenharbor.ai/v1` | Base URL OpenAI-compatible |
| `DEEPSEEK_MODEL` | `deepseek-v4.1-flash:free` | Model yang diverifikasi |
| `MODEL_PROVIDER` | `deepseek` | Hanya untuk rollback internal |
| `OLLAMA_*` | nilai lama | Hanya rollback internal |

`.env` dimuat otomatis; export shell lebih prioritas. `.env.example` tidak
berisi nilai rahasia.

## Indikator Status AI

`GET /api/health` sengaja **jujur**:

- gateway Hermes belum terhubung -> `Hermes Agent tidak terhubung`;
- gateway terhubung tetapi belum ada inferensi live ->
  `Model belum tersedia`, `model_api=""`, `model_tersedia=false`;
- setelah satu `POST /api/chat` berhasil dengan `detail_model` terverifikasi ->
  `Siap: deepseek-v4.1-flash:free · Hermes Agent`.

Frontend hanya mengubah status menjadi `Siap` setelah menerima `detail_model`
dari run Hermes yang benar-benar `completed`. Label Model dan Mode diambil dari
data API, bukan teks statis. Saat health menyertakan `agent_runtime=hermes`,
Mode menampilkan `TokenHarbor · Hermes`; bila tidak ada field tersebut (klien
lama/mock), Mode tetap `TokenHarbor` seperti kontrak lama.

## Endpoint

| Method | Path | Fungsi |
|---|---|---|
| `GET` | `/api/health` | Cek gateway Hermes, runtime, dan bukti model |
| `POST` | `/api/chat` | Kirim pertanyaan ke runtime aktif |
| `POST` | `/api/session/reset` | Buang sesi lama, buat ID dan instance baru |

Contoh body `/api/chat`:

```json
{ "session_id": null, "question": "Apa saja fakultas di UIR?" }
```

Field respons penting: `ok`, `status`, `pesan`, `session_id`, `jawaban`,
`sumber` (`{url, judul, status}`), `langkah`, `waktu`, `performansi`,
`agent_runtime`, dan `detail_model`. Nilai `ok` mengikuti hasil runtime, bukan
sekadar HTTP 200.

`detail_model` memuat `agent=hermes`, `run_id`, `model`, `provider`, `status`,
dan `usage`. Itu berasal dari status run Hermes, bukan dari label frontend.
`status_sumber` menandai apakah URL benar-benar terbaca dan dikutip dari
keluaran tool MCP UIR.

### Telemetri performansi

`performansi` memakai `threading.local`, sehingga dua permintaan bersamaan
tidak saling menimpa. Isinya hanya angka dan nama tool:

- `total_detik`, `tahapan`, `paling_lambat`;
- `panggilan_model`, `langkah`, `tool`;
- `cache` (`hit`/`miss`), `token` bila tersedia;
- ringkasan API pada jalur rollback internal.

Telemetri **tidak** mengirim isi pertanyaan, jawaban, riwayat, teks halaman,
atau kredensial ke frontend. Detail proses tampil pada `<details>` di bawah
jawaban; indikator memuat menunjukkan waktu berjalan.

## Sesi pengguna

- ID sesi dibuat server dengan `secrets.token_urlsafe(24)` dan divalidasi backend.
- Satu sesi memiliki satu instance runtime dan satu `RLock`.
- ID asing atau tidak dikenal ditolak dan diganti dengan ID baru.
- Untuk Hermes, ID aplikasi dipetakan ke `X-Hermes-Session-Id` dan
  `X-Hermes-Session-Key`; percakapan lanjutan memakai sesi Hermes yang sama.
- Maksimal 100 sesi di memori; sesi paling lama dibuang saat penuh.
- Tombol riwayat memakai konfirmasi dua langkah sebelum reset.
- Sesi dan bukti hilang saat server dimatikan.

## Keamanan dan batasan sumber

- Frontend disajikan dari origin backend yang sama; tidak ada CORS wildcard.
- Pertanyaan dibatasi 2000 karakter; kosong, non-teks, `null`, dan null byte ditolak.
- Detail error internal tidak dikirim ke browser; hanya masuk log server.
- URL sumber diverifikasi ulang di backend, lalu divalidasi lagi di JS: HTTPS
  eksak, hostname `uir.ac.id`, tanpa userinfo dan port selain 443.
- Jawaban dan judul sumber dirender dengan `textContent`/DOM creation. Tidak ada
  `innerHTML` untuk data dinamis.
- Tool berbahaya (`terminal`, `file`, `code_execution`, `browser`) nonaktif di
  konfigurasi Hermes; yang diizinkan hanya `cari_uir`, `ambil_halaman`, `baca_cache`.
- Isi halaman web diperlakukan sebagai data, bukan instruksi.

## Antarmuka lama

```bash
streamlit run web/app.py
```

## Screenshot

- `frontend/.hallmark/shots/index-1440x900.png` — Beranda desktop
- `frontend/.hallmark/shots/index-375.png` — Beranda ponsel
- `frontend/.hallmark/shots/percakapan-1440x900.png` — Jawaban nyata + rel sumber
- `frontend/.hallmark/shots/percakapan-375.png` — Jawaban nyata + rel sumber, ponsel

## Pengujian

```bash
node --check frontend/js/app.js
node --check tests/uji_frontend_cdp.js
node --check tests/uji_frontend_cdp_hermes.js
python -m unittest discover -s tests
python -m unittest tests.test_inferensi_tokenharbor_live -v
```

Uji mock (cepat, deterministik):

```powershell
$env:APP_URL='http://127.0.0.1:8003/'
node tests/uji_frontend_cdp.js
```

Uji live Hermes (tanpa mock; jalankan backend dengan `AGENT_RUNTIME=hermes`):

```powershell
$env:AGENT_RUNTIME='hermes'
python -m uvicorn web.api:app --host 127.0.0.1 --port 8012
# di terminal lain:
$env:APP_URL='http://127.0.0.1:8012/'
node tests/uji_frontend_cdp_hermes.js
```

Hasil terkini setelah stabilisasi: **334/334** test Python, **14/14** CDP mock,
dan **7/7** CDP live Hermes lulus. Uji live memverifikasi health awal yang belum
mengklaim model (dengan backend yang baru dimulai), inferensi
`deepseek-v4.1-flash:free` dengan token nyata, jawaban visi UIR, sitasi
`https://uir.ac.id/profil/`, status UI setelah inferensi, session ID, serta tidak
adanya error konsol/jaringan. Label baru `rate_limit` dan `hermes_5xx` membuat
pesan galat 429/5xx lebih spesifik tanpa mengubah alur render.

## Keterbatasan

- Belum ada autentikasi pengguna; isolasi hanya berdasarkan ID sesi acak.
- Sesi hanya di memori proses tunggal dan tidak bertahan setelah restart.
- Jawaban bergantung pada Hermes Agent, TokenHarbor, dan situs `uir.ac.id`.
- Batas 5 langkah Hermes tetap dapat tercapai, tetapi argumen tool kini dijelaskan
  eksplisit di prompt dan deskripsi MCP; tidak ada argumen gagal pada 12 run
  terukur. Status `batas_langkah_tercapai` kini disertai saran penyederhanaan
  pertanyaan.
- Health baru melaporkan model tersedia setelah inferensi live di proses yang
  sama. Setelah backend direstart, status kembali menunggu bukti.
- Streaming SSE tersedia di Hermes (`/v1/runs/{id}/events`) dan sudah diuji,
  tetapi belum dipakai frontend agar kontrak `POST /api/chat` tidak berubah.
- Status `bukti_tidak_cukup` / `sumber_tidak_tersedia` adalah hasil jujur agen,
  bukan kegagalan teknis.
- Tidak memakai logo resmi UIR, foto rekaan, metrik buatan, atau testimoni.
- Tombol Salin memakai Clipboard API dengan fallback `execCommand`.

## Memecahkan masalah

| Gejala | Penanganan |
|---|---|
| `Hermes Agent tidak terhubung` | Pastikan gateway berjalan di `HERMES_API_URL` dan `HERMES_API_KEY` benar |
| `Model belum tersedia` | Wajar sebelum chat pertama; setelah inferensi live status berubah |
| `Batas langkah tercapai` | Ulangi pertanyaan atau gunakan kalimat lebih spesifik |
| `TokenHarbor tidak terhubung` | Hanya terjadi pada rollback internal; periksa `DEEPSEEK_API_KEY` |
| Port 8000/8012 sibuk | Pakai port lain; frontend memakai path relatif `/api/...` |
| `Bukti tidak cukup` | Ajukan pertanyaan lebih spesifik; agen sengaja tidak menebak |
| Riwayat hilang | Wajar setelah server dimatikan atau sesi di-reset |

Bukti pengukuran stabilisasi, performa, dan warning lingkungan ada di
[`LAPORAN_STABILISASI.md`](LAPORAN_STABILISASI.md).
