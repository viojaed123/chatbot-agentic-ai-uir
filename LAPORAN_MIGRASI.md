# LAPORAN MIGRASI — ORKESTRATOR INTERNAL → HERMES AGENT RESMI

Laporan ini mencatat migrasi runtime chatbot UIR dari Orkestrator internal ke
**Hermes Agent resmi (NousResearch)** dengan model
**`deepseek-v4.1-flash:free`** melalui TokenHarbor. Tool UIR dijangkau Hermes
lewat MCP, sedangkan FastAPI hanya menjadi adapter sesi dan HTTP.

**Status akhir: migrasi dan verifikasi live selesai.** Reasoning, planning, tool
calling, dan execution loop dijalankan oleh Hermes Agent. Tidak ada label
"Hermes" yang dipakai saat eksekusi masih dilakukan Orkestrator. Semua perubahan
masih **uncommitted**; tidak ada commit, push, reset, atau penghapusan file.

Lanjutan tahap stabilisasi dan optimalisasi produksi (tanpa mengganti runtime,
model, atau tool) tercatat di
[`LAPORAN_STABILISASI.md`](LAPORAN_STABILISASI.md).

## 1. Keputusan dan ruang lingkup

| Keputusan | Nilai / alasan |
|---|---|
| Runtime utama | `AGENT_RUNTIME=hermes` (default) |
| Rollback sementara | `AGENT_RUNTIME=internal`, file `app/orchestrator.py` tetap ada |
| Integrasi | Hermes API Server (`/v1/runs`, `/api/sessions/*`) + custom OpenAI-compatible provider + MCP |
| Model | `deepseek-v4.1-flash:free`, base URL `https://tokenharbor.ai/v1`, `key_env=DEEPSEEK_API_KEY` |
| Batas agen | `agent.max_turns=5` |
| Toolset | `platform_toolsets.api_server: [uir]` |
| Tool MCP | `cari_uir`, `ambil_halaman`, `baca_cache` |
| Tool dimatikan | `terminal`, `file`, `code_execution`, `browser` |
| Session mapping | `X-Hermes-Session-Id` + `X-Hermes-Session-Key`; ID Hermes tidak dikirim ke frontend |
| Data root Hermes | `HERMES_HOME=%LOCALAPPDATA%\hermes-uir` (root lama menolak akses `WinError 5`) |

Tidak ada perubahan pada tujuan, ruang lingkup publik, atau batasan keamanan
proyek: hanya halaman publik `uir.ac.id`, read-only, tanpa data pribadi.

## 2. Arsitektur setelah migrasi

```text
frontend/js/app.js
  -> web/api.py
       -> app/hermes_runtime.py        (adapter HTTP, verifikasi bukti; bukan agent loop)
            -> Hermes API Server :8642
                 -> Hermes AIAgent     (reasoning, planning, tool loop, max 5)
                      -> MCP server "uir" (mcp_uir_server.py)
                           -> tools/cari_uir.py
                           -> tools/ambil_halaman.py
                           -> tools/baca_cache.py
       -> app/orchestrator.py          (rollback AGENT_RUNTIME=internal)
            -> app/model_client.py     (TokenHarbor / Ollama)
```

`app/hermes_runtime.py` hanya mengirim satu `POST /v1/runs`, melakukan polling
status run, membaca transkrip tool dari `/api/sessions/{id}/messages`, dan
mengubahnya menjadi kontrak response lama. Ia tidak memilih tool, tidak
merencanakan pencarian, dan tidak membuat loop agen baru.

## 3. Versi dan endpoint yang diverifikasi

Versi Hermes yang dipakai:

```text
Hermes Agent v0.22.1+72.g450ad50 (2026.9.24) · stable · local 450ad5055eb3
commit 450ad5055eb3c4852d96676ee99287d618bd283a
Python 3.13.5 · OpenAI SDK 2.24.0
```

Saat pengujian, gateway aktif di `127.0.0.1:8642` PID `27356`; log di
`%TEMP%\opencode\hermes_gateway_uir7.log`. Endpoint berikut mengembalikan HTTP 200:

| Endpoint | Bukti ringkas |
|---|---|
| `GET /health` | `platform=hermes-agent`, `status=ok`, `version=0.22.1` |
| `GET /v1/capabilities` | `runtime.mode=server_agent`, `tool_execution=server`, `tool_progress_events=true` |
| `GET /v1/toolsets` | 27 toolset; `terminal`, `file`, `code_execution`, `browser` semuanya `enabled=false` |
| `GET /v1/models` | Daftar model tersedia |
| `GET /api/model/options` | Provider `custom`, model default DeepSeek |
| `GET /api/sessions` | Sesi API server dengan `model=deepseek-v4.1-flash:free` |

`/v1/capabilities` juga mengonfirmasi header sesi resmi:
`X-Hermes-Session-Id` dan `X-Hermes-Session-Key`. Tidak ada warning MCP/toolset
pada log gateway setelah perbaikan konfigurasi dan ABI.

## 4. Konfigurasi Hermes dan perbaikan lingkungan

Konfigurasi aktif di `%LOCALAPPDATA%\hermes-uir\config.yaml` (backup lama:
suffix `bak-migrasi-20261125-160344`) memuat:

```yaml
model:
  provider: custom
  default: deepseek-v4.1-flash:free
  base_url: https://tokenharbor.ai/v1
  key_env: DEEPSEEK_API_KEY
agent:
  max_turns: 5
gateway:
  api_server:
    direct_model_requests: true
    tool_progress_events: true
platform_toolsets:
  api_server: [uir]
mcp_servers:
  uir:
    command: "C:/Program Files/Python313/python.exe"
    args: ["<root proyek>/mcp_uir_server.py"]
    enabled: true
```

Nama toolset yang benar adalah `uir`, bukan `mcp_uir`. Kredensial tetap hanya di
`.env` proyek dan environment; `config.yaml` hanya menyimpan nama env.

Perbaikan lingkungan yang diperlukan agar runtime privat Hermes berfungsi:

- Wheel cp313 diinstal ke site-packages privat Hermes: `pydantic-core`, `cffi`,
  `pywin32`, `jiter`, `rpds-py`, `multidict`, `frozenlist`, `yarl`, `aiohttp`,
  `httptools`, `websockets`, `watchfiles`.
- `mcp_uir_server.py` mendukung MCP SDK 1.x dan 2.x. Pada SDK 2.x, `FastMCP`
  diganti `MCPServer` dari `mcp.server.mcpserver`.
- Bug `_permintaan(..., method="POST")` diperbaiki menjadi `metode="POST"`.
- `_tool_dan_sumber()` diperbaiki untuk membaca kunci `data` pada transkrip,
  hanya menghitung tool `mcp__uir__*`, memakai filter waktu run, dan membuang
  backslash pada URL.
- Status `Iteration budget exhausted` / `maximum iterations` dipetakan ke
  `batas_langkah_tercapai`, bukan kegagalan penyedia.

## 5. Tool MCP UIR

Server `mcp_uir_server.py` hanya membungkus tiga tool yang sudah ada:

| Tool | Fungsi |
|---|---|
| `cari_uir` | Mencari halaman publik pada domain `uir.ac.id` |
| `ambil_halaman` | Mengambil dan mengekstrak teks bermakna dari satu halaman |
| `baca_cache` | Membaca cache konten dengan masa berlaku terbatas |

Server terverifikasi mengekspos ketiga tool tersebut lewat klien MCP stdio.
Tool mengembalikan JSON ringkas dengan URL di awal. Tidak ada agent loop di
dalam server MCP. Validasi URL tetap HTTPS eksak `uir.ac.id`, tanpa userinfo,
redirect ke domain lain ditolak.

Bukti tool pada transkrip Hermes:

```text
TOOL_UIR = ["mcp__uir__cari_uir", "mcp__uir__baca_cache"]
TOOL_UIR_B = ["mcp__uir__cari_uir", "mcp__uir__cari_uir",
              "mcp__uir__baca_cache", "mcp__uir__baca_cache"]
```

Sesi lain juga menunjukkan `mcp__uir__ambil_halaman`, sehingga ketiga tool
benar-benar terpakai oleh Hermes, bukan hanya terdaftar.

## 6. Pembuktian model live dan sitasi

`/api/health` sengaja tidak mengklaim model hanya karena konfigurasi:

| Momen | `model_api` | `model_tersedia` | Status UI |
|---|---|---|---|
| Sebelum inferensi | `""` | `false` | `Model belum tersedia` |
| Setelah chat sukses | `deepseek-v4.1-flash:free` | `true` | `Siap: deepseek-v4.1-flash:free · Hermes Agent` |

Bukti model diambil dari `runtime.model` pada status run Hermes, bukan label
frontend. Status terverifikasi disimpan bersama di modul `hermes_runtime`, agar
instance `HermesRuntime()` yang dipakai `health()` dapat melihatnya. Frontend
juga hanya mengubah status setelah menerima `detail_model` dengan
`agent=hermes`, `status=completed`, dan model yang cocok.

Sitasi dibangun hanya dari pesan tool `mcp__uir__*`. URL yang ditulis model
tanpa melewati tool tidak dijadikan bukti. Pada uji live, sitasi yang tampil
adalah `https://uir.ac.id/profil/` dengan `cited=true`.

Contoh bukti run live:

```text
agent          = hermes
run_id         = run_ab59d4ca598c43fa87985f00fe198e62
model          = deepseek-v4.1-flash:free
status         = completed
total_tokens   = 30205
tool           = mcp__uir__cari_uir, mcp__uir__baca_cache, mcp__uir__ambil_halaman
sumber         = https://uir.ac.id/profil/
```

## 7. Hasil integrasi backend live

Backend dijalankan dengan `AGENT_RUNTIME=hermes` pada port 8012 (CDP) dan 8013
(integrasi API). Port 8010/8011 sengaja tidak diganggu.

| Skenario | Hasil | Latensi | Token total |
|---|---|---:|---:|
| Health awal | `ok=true`, `agent_runtime=hermes`, `model_tersedia=false` | — | — |
| Chat A: visi UIR | Sukses, 2 langkah, 1 sitasi `cited=true` | 16,19 dtk | 24.977 |
| Chat B: rektor UIR | Sukses, 4 langkah, 1 sitasi `cited=true` | 20,21 dtk | 29.798 |
| Lanjutan A (session_id sama) | Sukses, mengingat visi | 3,07 dtk | 9.212 |
| Lanjutan B (session_id sama) | Sukses, mengingat pertanyaan terakhir | 4,12 dtk | 11.892 |
| Reset A lalu chat baru | ID baru, sukses, 1 sitasi | 10,11 dtk | 24.812 |
| Health setelah chat | `model_api=deepseek-v4.1-flash:free`, `model_tersedia=true` | — | — |

Dua chat pertama dijalankan bersamaan dengan `session_id` berbeda. Keduanya
tetap terpisah dan tidak saling menimpa memori percakapan. ID sesi internal
Hermes tidak muncul pada response frontend.

## 8. Hasil frontend CDP

### Mock (deterministik)

`node tests/uji_frontend_cdp.js` -> **14/14 lulus**. Memeriksa health mock,
Enter/Shift+Enter, double-send, XSS, penyaringan sumber jahat, detail proses,
salin, reset, session ID baru, dua sesi, responsivitas 375 px, status gagal, dan
error konsol/jaringan. Status mock masih menampilkan `Internal` karena payload
health mock tidak mengirim `agent_runtime`; ini sesuai kontrak lama.

### Live Hermes tanpa mock

`node tests/uji_frontend_cdp_hermes.js` terhadap `http://127.0.0.1:8012/` ->
**7/7 lulus**:

| Pemeriksaan | Hasil |
|---|---|
| Health awal jujur dan runtime Hermes | Lulus; `agent_runtime=hermes`, `model_tersedia=false`, UI `Model belum tersedia` |
| Inferensi live Hermes + DeepSeek | Lulus; model `deepseek-v4.1-flash:free`, `status=completed`, 30.205 token |
| Jawaban nyata memuat visi UIR | Lulus pada API dan UI |
| Sitasi berasal dari tool UIR | Lulus; `https://uir.ac.id/profil/`, `cited=true` |
| Status UI berubah setelah inferensi | Lulus; `Siap: deepseek-v4.1-flash:free · Hermes Agent` |
| Session ID diterbitkan server | Lulus |
| Error konsol/jaringan | Tidak ada |

## 9. File yang dibuat / diubah

| File | Perubahan |
|---|---|
| `app/hermes_runtime.py` | **Baru**: klien API Server Hermes, verifikasi model/sitasi, health bersama, mapping error |
| `mcp_uir_server.py` | **Baru**: tiga tool UIR sebagai MCP server, kompatibel SDK 1.x/2.x |
| `web/api.py` | Pemilihan runtime, `agent_runtime`, `detail_model`, sesi dengan instance runtime |
| `frontend/js/app.js` | Label runtime/status health dan status `Siap` setelah `detail_model` terverifikasi |
| `.env.example` | Variabel `AGENT_RUNTIME`, `HERMES_*`, kredensial dikosongkan |
| `scripts/jalankan_hermes.py` | **Baru**: peluncur gateway Hermes tanpa mencetak rahasia |
| `tests/test_hermes_runtime.py` | **Baru**: 11 uji unit jembatan Hermes tanpa jaringan |
| `tests/test_mcp_uir_server.py` | **Baru**: 4 uji unit tool MCP UIR |
| `tests/uji_frontend_cdp_hermes.js` | **Baru**: uji frontend live tanpa mock |
| `README.md`, `README_CUSTOM_UI.md` | Dokumentasi runtime Hermes, status, cara jalankan, rollback |
| `docs/ARSITEKTUR_HERMES.md` | **Baru**: batas tanggung jawab dan alur lengkap Hermes |
| `%LOCALAPPDATA%\hermes-uir\config.yaml` | Provider custom, MCP `uir`, toolset aman, `max_turns=5` |

Tidak ada file lama yang dihapus atau diganti nama. `app/orchestrator.py`,
`app/model_client.py`, Streamlit, dan seluruh pengujian lama tetap dipertahankan.

## 10. Hasil pengujian lengkap

| Pengujian | Hasil |
|---|---|
| `python -m unittest discover -s tests` | **323 test, OK** (35,46 dtk) |
| `python -m unittest tests.test_hermes_runtime tests.test_mcp_uir_server` | **15 test, OK** |
| `python -m unittest tests.test_inferensi_tokenharbor_live -v` | **4/4 lulus** (baseline TokenHarbor lama) |
| `node tests/uji_frontend_cdp.js` | **14/14 lulus** |
| `node tests/uji_frontend_cdp_hermes.js` | **7/7 lulus** |
| `node --check frontend/js/app.js` | Lulus |
| `node --check tests/uji_frontend_cdp.js` | Lulus |
| `node --check tests/uji_frontend_cdp_hermes.js` | Lulus |
| `python -m py_compile app/hermes_runtime.py tests/test_hermes_runtime.py scripts/jalankan_hermes.py` | Lulus |
| Pindaian rahasia | Tidak ada kunci yang dicetak di log, laporan, source, atau frontend |

Baseline pra-migrasi adalah 268 test, 13/13 CDP, 8/8 galat API, 9/9 konkurensi,
audit 36 dengan 30 PASS / 6 SKIPPED, rata-rata 39,82 detik. Migrasi TokenHarbor
sebelumnya mencatat 308 test dan 8/8 CDP live. Angka-angka itu tidak dipakai
sebagai bukti runtime Hermes; perbandingan langsung tidak dibuat karena beban,
model, dan jalur eksekusinya berbeda.

## 11. Perbandingan dan performa

| Metrik | Orkestrator internal (sebelum) | Hermes Agent (terukur) |
|---|---:|---:|
| eksekusi agen | Proses aplikasi | Proses gateway terpisah |
| panggilan tool per pertanyaan faktual | 1–3 tool internal | 2–4 tool MCP (`cari_uir`, `baca_cache`, `ambil_halaman`) |
| chat awan sederhana | ~1,7–7,5 dtk | 16,19–20,21 dtk (termasuk tool + reasoning Hermes) |
| pertanyaan lanjutan | ~2,3 dtk (tanpa tool baru) | 3,07–4,12 dtk |
| token | 24–120 per panggilan | 9.212–30.205 per run Hermes |
| bukti model | `performansi.api.model` | `detail_model.model` + `runtime` status run Hermes |
| sitasi | dari hasil tool internal | dari pesan tool MCP UIR pada transkrip Hermes |

Tidak ada klaim bahwa Hermes lebih cepat. Latensi bertambah karena Hermes
menjalankan reasoning/tool loop penuh dengan riwayat sesi dan sistem prompt.
Model `:free` tetap memiliki estimasi biaya nol di jalur internal; tagihan
sebenarnya berada di dashboard penyedia dan tidak diverifikasi di sini.

## 12. Masalah tersisa

- Warning non-fatal pada log gateway: source-update `WinError 5`,
  `automatic dependency repair retry limit reached`, dan SQLite 3.49.1
  `journal_mode=DELETE` (WAL-reset bug). Gateway tetap berjalan dan semua uji
  live lulus.
- Satu run transient gagal karena model mengirim `mcp__uir__cari_uir` tanpa
  argumen `query`, lalu mencapai batas 5 iterasi. Status kini dilaporkan jujur
  sebagai `batas_langkah_tercapai`; gagal ini tidak muncul pada pengulangan.
- `HERMES_HOME` sengaja dipindah ke `%LOCALAPPDATA%\hermes-uir` karena data root
  lama menolak akses. Backup konfigurasi lama disimpan dengan suffix
  `bak-migrasi-20261125-160344`.
- Health hanya terverifikasi dalam proses backend yang sama. Setelah restart,
  status kembali `Model belum tersedia` sampai ada inferensi live baru.
- Streaming masih dimatikan (`streaming=false` pada config dan frontend).
- Kredensial yang pernah terekspos pada tahap sebelumnya tetap disarankan untuk
  dirotasi; tidak ada nilai rahasia di file template maupun laporan ini.

## 13. Cara jalankan dan rollback

```bash
pip install -r requirements.txt
# Isi .env dari .env.example; jangan mencetak kredensial.
python scripts/jalankan_hermes.py
python -m uvicorn web.api:app --host 127.0.0.1 --port 8000
```

Rollback sementara tanpa menghapus fitur:

```bash
# PowerShell
$env:AGENT_RUNTIME='internal'
python -m uvicorn web.api:app --host 127.0.0.1 --port 8000

# sh/bash
AGENT_RUNTIME=internal python -m uvicorn web.api:app --host 127.0.0.1 --port 8000
```

Untuk Ollama lokal pada jalur internal: `MODEL_PROVIDER=ollama` dan
`OLLAMA_MODEL=hermes3:8b`. Tidak ada fallback diam-diam.

## 14. Kesimpulan

Klaim yang **boleh** dibuat: Hermes Agent resmi benar-benar menjadi runtime;
DeepSeek `deepseek-v4.1-flash:free` terbukti aktif lewat status run live; tool
UIR benar-benar dipanggil lewat MCP; sitasi berasal dari keluaran tool; sesi
frontend terpisah dan percakapan lanjutan berfungsi; frontend merespons status
secara jujur; seluruh regresi lulus.

Klaim yang **tidak** dibuat: tidak ada pernyataan bahwa Hermes lebih cepat,
bahwa warning lingkungan sudah hilang, atau bahwa model tidak pernah gagal
memanggil tool dengan argumen lengkap.
