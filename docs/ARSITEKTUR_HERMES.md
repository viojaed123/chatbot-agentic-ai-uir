# Arsitektur Runtime Hermes Agent

Dokumen ini menjelaskan batas tanggung jawab antara aplikasi UIR dan Hermes
Agent resmi. Tujuannya mencegah salah label: bila yang berjalan adalah
Orkestrator internal, status tidak boleh disebut Hermes.

## 1. Diagram komponen

```text
[Browser]
   |  POST /api/chat {question, session_id?}
   v
[web/api.py]  FastAPI
   |  - validasi input
   |  - PenyimpananSesi (RLock per sesi)
   |  - memilih runtime dari AGENT_RUNTIME
   |
   +--> AGENT_RUNTIME=hermes (default)
   |       [app/hermes_runtime.py]
   |          - POST /v1/runs
   |          - GET  /v1/runs/{run_id}
   |          - GET  /api/sessions/{id}/messages
   |          - verifikasi model & sitasi
   |             v
   |       [Hermes API Server :8642]
   |          - server-side Hermes AIAgent
   |          - reasoning, planning, memory, tool loop
   |          - max_turns: 5
   |             v
   |       [MCP toolset "uir"]
   |             v
   |       [mcp_uir_server.py]
   |          - cari_uir
   |          - ambil_halaman
   |          - baca_cache
   |             v
   |       https://uir.ac.id/ (publik)
   |
   +--> AGENT_RUNTIME=internal (rollback)
           [app/orchestrator.py]
              - loop agen lama, maksimal 5 langkah
              - [app/model_client.py] -> TokenHarbor / Ollama
```

## 2. Batas tanggung jawab

| Komponen | Boleh | Tidak boleh |
|---|---|---|
| `frontend/js/app.js` | Memanggil `/api/*`, merender teks/tautan aman, menampilkan status dari API | Memanggil Hermes/TokenHarbor, menyimpan kunci, memutuskan model aktif |
| `web/api.py` | Validasi, sesi, choice runtime, konversi response | Menjalankan reasoning atau tool loop |
| `app/hermes_runtime.py` | HTTP client, polling, verifikasi `runtime.model`, ekstraksi URL dari pesan tool | Membuat tool baru, merencanakan pencarian, menggantikan Hermes |
| Hermes Agent | Reasoning, planning, tool calling, eksekusi, memori sesi | Mengakses sistem akademik berautentikasi atau domain selain `uir.ac.id` (dicegah config + prompt + tool) |
| `mcp_uir_server.py` | Menyediakan tiga tool baca | Menjalankan agen atau menyimpan kredensial |

## 3. Alur satu pertanyaan

1. Frontend mengirim `POST /api/chat` ke backend yang sama origin.
2. Backend membersihkan pertanyaan, mengambil/membuat `session_id`, lalu
   mengunci sesi tersebut.
3. `HermesRuntime.tanya()` mengirim:

   ```json
   {
     "input": "<pertanyaan>",
     "session_id": "uir-<session_id aplikasi>",
     "instructions": "<SYSTEM_PROMPT>",
     "model": "deepseek-v4.1-flash:free",
     "provider": "custom"
   }
   ```

   Header `X-Hermes-Session-Id` dan `X-Hermes-Session-Key` memisahkan memori
   antar sesi. ID Hermes tidak dikembalikan ke browser.

4. Hermes membuat run baru, memilih tool MCP, dan berhenti hanya saat status
   run menjadi terminal (`completed`, `failed`, `cancelled`, `interrupted`,
   atau `unknown`).
5. Backend membaca status run. Bila `completed`, model pada `runtime.model`
   harus cocok dengan konfigurasi; jika tidak, respons ditolak.
6. Backend membaca transkrip sesi dan hanya mengumpulkan URL dari pesan
   `role=tool` dengan `tool_name=mcp__uir__*`. URL dari teks model saja tidak
   dijadikan bukti.
7. Backend mengembalikan field lama plus `agent_runtime`, `detail_model`, dan
   `status_sumber`. Frontend menaikkan status `Siap` hanya jika bukti lengkap.

## 4. Status dan verifikasi model

State terverifikasi disimpan di modul `app/hermes_runtime.py` dengan lock:

```text
_MODEL_TERVERIFIKASI = {model, provider, waktu}
```

- Awal proses: kosong. `/api/health` melaporkan `model_tersedia=false`.
- Setelah `tanya()` sukses dengan jawaban tidak kosong dan model cocok: state diisi.
- `health()` membuat instance `HermesRuntime()` baru, tetapi tetap membaca
  state bersama, sehingga health dapat melaporkan bukti yang sama.
- Setelah backend restart, state kembali kosong sampai ada inferensi live baru.

Ini mencegah klaim model aktif hanya dari `config.yaml` atau `/api/model/options`.

## 5. Keamanan

- Domain: hanya HTTPS eksak `uir.ac.id`; userinfo, port selain 443, dan redirect
  ke host lain ditolak oleh tool.
- Toolset berbahaya dimatikan: `terminal`, `file`, `code_execution`, `browser`.
- Prompt dan transkrip halaman diperlakukan sebagai data, bukan instruksi.
- Kunci `HERMES_API_KEY` dan `DEEPSEEK_API_KEY` hanya dari environment/.env;
  tidak dicetak, tidak dikirim ke frontend, tidak masuk telemetri.
- `SYSTEM_PROMPT` mewajibkan sitasi, menjawab jujur bila bukti tidak cukup, dan
  menuliskan argumen tool secara eksplisit sehingga model tidak memanggil
  `tool_describe` atau mengirim `cari_uir` tanpa `query`.
- Batas 5 iterasi mencegah perulangan agen tanpa akhir.

## 6. Mapping error

| Kondisi | Status API |
|---|---|
| `HERMES_API_KEY` kosong / 401 / 403 | `kunci_tidak_tersedia` |
| HTTP 429 | `rate_limit`; GET diulang maksimal 2x dengan `Retry-After`, POST tidak diulang |
| HTTP 5xx | `hermes_5xx`; GET diulang maksimal 2x, POST tidak diulang |
| Timeout run / polling | `timeout`; GET diulang sebelum gagal |
| Iteration budget / maximum iterations / cancelled | `batas_langkah_tercapai` |
| Model run tidak cocok | `model_tidak_tersedia` |
| Run completed tanpa jawaban | `sumber_tidak_tersedia` |

Detail teknis tetap di log server; browser hanya menerima pesan generik.

## 7. Konfigurasi minimal

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
    command: "<python 3.13>"
    args: ["<root proyek>/mcp_uir_server.py"]
    enabled: true
```

Variabel lingkungan aplikasi: `AGENT_RUNTIME`, `HERMES_API_URL`,
`HERMES_API_KEY`, `HERMES_TIMEOUT`, `HERMES_POLL`, `DEEPSEEK_*`.

## 8. Rollback

Set `AGENT_RUNTIME=internal`. Backend memakai `app/orchestrator.py` dan
`app/model_client.py` seperti sebelum migrasi. Tidak ada file yang dihapus,
tidak ada perubahan riwayat Git, dan konfigurasi Hermes dapat dibiarkan aktif
tanpa dipanggil.

## 9. Bukti singkat

- Hermes API Server HTTP 200 pada `/health`, `/v1/capabilities`,
  `/v1/toolsets`, `/v1/models`, `/api/model/options`, `/api/sessions`.
- MCP mengekspos tepat tiga tool UIR.
- Run live `completed` dengan `model=deepseek-v4.1-flash:free` dan token nyata.
- Dua sesi bersamaan tetap terpisah; pertanyaan lanjutan memakai sesi sama.
- 334 test Python lulus; 14/14 CDP mock; 7/7 CDP live Hermes.
- SSE `/v1/runs/{id}/events` terverifikasi (9 event), tidak diaktifkan ke frontend.
- `tool_describe` 0/8 run; argumen tool gagal 0/8; sitasi sah 4/4.

Rincian lengkap ada di [`LAPORAN_MIGRASI.md`](../LAPORAN_MIGRASI.md) dan
[`LAPORAN_STABILISASI.md`](../LAPORAN_STABILISASI.md).
