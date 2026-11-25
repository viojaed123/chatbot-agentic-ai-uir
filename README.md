# UIR AI Assistant

Chatbot informasi internal Universitas Islam Riau berbasis **Agentic AI**.
Jawaban faktual hanya diambil dari halaman publik `https://uir.ac.id/`,
diverifikasi terhadap teks yang benar-benar terbaca, dan disertai tautan sumber.

**Runtime utama saat ini adalah Hermes Agent resmi (NousResearch)**, bukan
orkestrator internal. Reasoning, planning, tool calling, dan execution loop
berjalan di proses Hermes tersendiri. Model aktifnya adalah
**`deepseek-v4.1-flash:free`** melalui TokenHarbor (`https://tokenharbor.ai/v1`).

## Arsitektur singkat

```text
frontend/ (HTML/CSS/JS; Streamlit lama tetap ada)
        -> web/api.py (FastAPI: sesi, guardrail adapter, endpoint)
                -> app/hermes_runtime.py (klien API Server; BUKAN agent loop)
                        -> Hermes Agent API Server (127.0.0.1:8642)
                                -> Hermes AIAgent: reasoning + planning + tool loop
                                        -> MCP toolset "uir"
                                                -> mcp_uir_server.py
                                                        -> tools/cari_uir.py
                                                        -> tools/ambil_halaman.py
                                                        -> tools/baca_cache.py (cache 24 jam)

Rollback sementara:
        web/api.py -> app/orchestrator.py -> app/model_client.py
                                      -> TokenHarbor / Ollama
```

`AGENT_RUNTIME=hermes` adalah default. Set `AGENT_RUNTIME=internal` untuk
memakai Orkestrator lama tanpa menghapus file atau fitur apa pun. ID sesi
frontend dipetakan ke header Hermes `X-Hermes-Session-Id` dan
`X-Hermes-Session-Key`; ID sesi internal Hermes tidak pernah dikirim ke browser.

### Pembuktian model dan sitasi

- `GET /api/health` **tidak pernah** mengklaim model aktif hanya dari
  konfigurasi. Sebelum ada inferensi live, `model_tersedia=false` dan
  `model_api=""`, walaupun gateway Hermes sudah siap.
- Setelah `POST /api/chat` berhasil, `detail_model` diambil dari field `runtime`
  status run Hermes. Jika modelnya cocok dengan konfigurasi, status terverifikasi
  disimpan bersama di `app/hermes_runtime.py` sehingga health pada proses yang
  sama ikut berubah menjadi `model_tersedia=true`.
- Frontend juga hanya menaikkan status menjadi `Siap ... Hermes Agent` setelah
  menerima `detail_model` dengan `agent=hermes`, `status=completed`, dan model
  DeepSeek yang valid.
- Sitasi dibangun hanya dari pesan tool `mcp__uir__*` pada transkrip sesi
  Hermes. URL dari teks jawaban model saja tidak cukup untuk dijadikan bukti.

## Menjalankan

Prasyarat: Python 3.10+, dependensi di `requirements.txt`, source Hermes Agent,
dan `DEEPSEEK_API_KEY` serta `HERMES_API_KEY` di `.env`/environment. Kunci tidak
boleh ditulis di source code, frontend, log, atau laporan.

```bash
pip install -r requirements.txt
# Salin .env.example menjadi .env, lalu isi kredensial tanpa mencetaknya.
python scripts/jalankan_hermes.py
python -m uvicorn web.api:app --host 127.0.0.1 --port 8000
```

Buka <http://127.0.0.1:8000/>. `scripts/jalankan_hermes.py` meluncurkan gateway
Hermes sebagai proses latar, menulis PID/log ke direktori log sementara, dan
tidak mencetak kredensial. Untuk lingkungan pengujian ini, source berada di
`%LOCALAPPDATA%\hermes\hermes-agent` dan data root di
`%LOCALAPPDATA%\hermes-uir`; keduanya dapat diubah lewat `HERMES_SOURCE` dan
`HERMES_HOME`.

Konfigurasi data root Hermes yang diverifikasi: `model.provider=custom`,
`model.default=deepseek-v4.1-flash:free`, `model.base_url=https://tokenharbor.ai/v1`,
`model.key_env=DEEPSEEK_API_KEY`, `agent.max_turns=5`, MCP server `uir` aktif,
`platform_toolsets.api_server=[uir]`, serta `terminal`, `file`,
`code_execution`, dan `browser` dalam keadaan nonaktif.

Rollback sementara:

```bash
$env:AGENT_RUNTIME='internal'          # PowerShell
# AGENT_RUNTIME=internal               # sh/bash
python -m uvicorn web.api:app --host 127.0.0.1 --port 8000
```

`MODEL_PROVIDER=deepseek` memakai jalur internal TokenHarbor;
`MODEL_PROVIDER=ollama` memakai `hermes3:8b` lokal.

## Konfigurasi

| Variable | Default | Fungsi |
|---|---|---|
| `AGENT_RUNTIME` | `hermes` | `hermes` (utama) atau `internal` (rollback) |
| `HERMES_API_URL` | `http://127.0.0.1:8642` | API Server Hermes |
| `HERMES_API_KEY` | — | Kredensial gateway Hermes lokal |
| `HERMES_TIMEOUT` | `180` | Batas tunggu satu run Hermes (detik) |
| `HERMES_POLL` | `0.3` | Interval polling status run (detik) |
| `HERMES_MAX_RETRY` | `2` | Retry maksimal untuk permintaan GET (429/5xx/timeout); POST tidak diulang |
| `HERMES_WAIT_MODE` | `sse` | `sse` menunggu event terminal via stream; `polling` mengembalikan perilaku lama (rollback/A-B) |
| `DEEPSEEK_API_KEY` | — | Kredensial TokenHarbor; dipakai Hermes dan jalur internal |
| `DEEPSEEK_BASE_URL` | `https://tokenharbor.ai/v1` | Base URL TokenHarbor |
| `DEEPSEEK_MODEL` | `deepseek-v4.1-flash:free` | ID model yang diverifikasi |
| `MODEL_PROVIDER` | `deepseek` | Hanya untuk runtime internal |
| `OLLAMA_*` | nilai lama | Hanya rollback internal |

`.env` dimuat otomatis saat paket `app` diimpor, tetapi export shell tetap
lebih prioritas (`os.environ.setdefault`).

## Endpoint API

| Method | Path | Fungsi |
|---|---|---|
| `GET` | `/api/health` | Status gateway, runtime, dan bukti model bila sudah ada |
| `POST` | `/api/chat` | Satu giliran percakapan; mendukung `session_id` opsional |
| `POST` | `/api/session/reset` | Mengganti ID dan instance sesi |

`POST /api/chat` mempertahankan field lama (`ok`, `status`, `jawaban`, `sumber`,
`langkah`, `pesan`, `session_id`, `waktu`, `performansi`) dan menambahkan
`agent_runtime` serta `detail_model` (`agent`, `run_id`, `model`, `provider`,
`status`, `usage`). `performansi` berisi waktu, tool, cache, dan token; tidak
berisi pertanyaan, jawaban, riwayat, atau kredensial.

## Pengujian

```bash
python -m unittest discover -s tests
python -m unittest tests.test_inferensi_tokenharbor_live -v
node --check frontend/js/app.js
node tests/uji_frontend_cdp.js
node tests/uji_frontend_cdp_hermes.js   # butuh backend AGENT_RUNTIME=hermes
```

Hasil terkini (25 Nov 2026, setelah stabilisasi):

- **334/334** test Python lulus (323 baseline + 11 uji stabilisasi/error handling).
- **12/12** run terukur memakai tool MCP tanpa argumen gagal; `tool_describe` tidak lagi dipanggil.
- **14/14** pemeriksaan browser headless dengan API mock.
- **7/7** pemeriksaan browser headless live terhadap backend Hermes port 8012:
  health awal jujur, inferensi DeepSeek terverifikasi, jawaban visi UIR, sitasi
  `uir.ac.id`, status UI berubah setelah inferensi, session ID diterbitkan server,
  serta tanpa error konsol/jaringan.
- Integrasi backend port 8013: dua chat bersamaan sukses (16,19 dtk / 20,21 dtk),
  pertanyaan lanjutan memakai `session_id` yang sama (3,07 dtk / 4,12 dtk),
  reset menghasilkan ID baru dan chat setelah reset sukses (10,11 dtk). Health
  awal `model_tersedia=false`, health setelah chat `true`.
- Satu run CDP live menggunakan 30.205 token total; run integrasi memakai
  24.977, 29.798, 9.212, 11.892, dan 24.812 token total.
- Stabilasi: 4/4 konkurensi sukses dengan ID unik; isolasi sesi terverifikasi;
  akurasi sitasi 4/4; HTTP 401/403, 429, timeout, dan 5xx diuji unit serta
  diteruskan sebagai status API yang jujur. Latency rata-rata tidak dapat
  diklaim membaik; detail pengukuran ada di laporan stabilisasi.

Rincian bukti, endpoint, tool, performa, warning, dan rollback ada di
[`LAPORAN_MIGRASI.md`](LAPORAN_MIGRASI.md),
[`LAPORAN_STABILISASI.md`](LAPORAN_STABILISASI.md), dan
[`docs/ARSITEKTUR_HERMES.md`](docs/ARSITEKTUR_HERMES.md).
Baseline pra-migrasi (268 test, 13/13 CDP, 8/8 galat API, 9/9 konkurensi, audit
36 dengan 30 PASS / 6 SKIPPED, rata-rata 39,82 detik) tercatat di
`LAPORAN_FINAL.md` dan tidak dipakai sebagai bukti runtime Hermes.

Catatan port: 8010 dan 8011 adalah demo lama yang sengaja tidak diganggu;
pengujian memakai 8012/8013, gateway Hermes memakai 8642, Ollama rollback 11434.

## Batasan

- Hanya halaman publik `uir.ac.id`; HTTPS eksak, robots, timeout, dan rate limit divalidasi.
- Sesi hanya di memori proses dan hilang saat server dimatikan.
- Jika bukti kurang, chatbot menjawab jujur `bukti_tidak_cukup` atau
  `sumber_tidak_tersedia`, bukan menebak.
- `agent.max_turns=5`. Argumen tool kini dijelaskan eksplisit di prompt dan
  deskripsi MCP; tidak ada argumen gagal pada 12 run terukur. Bila batas iterasi
  tetap tercapai, API memberi pesan untuk menyederhanakan pertanyaan.
- Gateway masih mencatat warning non-fatal tentang source-update `WinError 5`,
  automatic dependency repair, dan SQLite 3.49.1 journal_mode=DELETE. WAL sengaja
  tidak dipaksa karena Hermes menghindari bug korupsi pada SQLite 3.49.1.
- Streaming SSE tersedia di level Hermes (`/v1/runs/{id}/events`) dan sudah
  diuji, tetapi tidak diaktifkan end-to-end agar kontrak frontend tetap utuh.
- Kredensial wajib disediakan lewat environment/.env dan tidak boleh muncul di
  chat, source code, log, telemetri, atau laporan.

Rincian antarmuka ada di [`README_CUSTOM_UI.md`](README_CUSTOM_UI.md).
