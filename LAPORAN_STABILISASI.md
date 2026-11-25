# LAPORAN STABILISASI DAN OPTIMALISASI PRODUKSI — CHATBOT AGENTIC AI UIR

Tahap ini **tidak mengulang migrasi runtime**. Hermes Agent resmi tetap menjadi
runtime utama, `deepseek-v4.1-flash:free` TokenHarbor tetap model aktif, MCP tool
UIR tetap tiga tool baca, dan jalur rollback `AGENT_RUNTIME=internal` sengaja
dipertahankan. Semua perubahan masih **uncommitted**; tidak ada commit, push,
reset, atau penghapusan file.

## 1. Ringkasan hasil

| Item | Sebelum | Sesudah | Kesimpulan |
|---|---|---|---|
| Tool call tanpa `query` | Transien `missing required argument(s): query` pernah memicu `batas_langkah_tercapai` | 0/8 run gagal argumen; prompt dan deskripsi MCP memuat contoh argumen wajib | Pengaman terpasang; tidak ada pengulangan pada 12 run terukur |
| `tool_describe` (langkahDiscovery) | 4/4 run | 0/4 + 0/4 run | Dihilangkan 100% lewat instruksi, bukan loop baru |
| Token rata-rata (batch B vs gabungan) | 30.951,5 | 29.990,4 | Turun 3,1%; median turun 14,3% |
| Langkah rata-rata | 3,50 | 3,38 | Turun 3,4%; 2 dari 8 run mencapai batas 5 tetapi tetap `completed` |
| Latency rata-rata | 18,70 dtk (batch B) | 22,70 dtk (n=8) | **Tidak membaik**; variasi model sangat tinggi |
| Latency median / P95 | 16,70 / 29,26 dtk | 20,62 / 38,99 dtk | **Tidak membaik** pada gabungan sample |
| Keberhasilan tool calling | 4/4 | 12/12 run (8 dasar + 4 konkurensi) | 100% |
| Akurasi jawaban berbasis sumber | 4/4 (100%) | 4/4 (100%) | Semua URL di jawaban berasal dari tool `mcp__uir__*` |
| Konkurensi 4 pengguna | — | 4/4 sukses, 4 ID unik, total 30,57 dtk | Session isolation terjaga |
| Regression test | 323 | 334 | 334/334 lulus |
| CDP frontend | 14/14 mock, 7/7 live | 14/14 mock, 7/7 live | Tidak ada regresi UI |

**Tidak ada klaim latency membaik.** Dua batch sebelum optimasi saja sudah
bervariasi: rata-rata 30,55 dtk (P95 46,38 dtk) dan 18,70 dtk (P95 29,26 dtk).
Dua batch sesudah optimasi: 19,50 dtk (P95 22,46 dtk) dan 25,91 dtk (P95 38,99
dtk). Perbaikan yang dapat diklaim dengan bukti adalah penghapusan langkah
`tool_describe`, berkurangnya langkah/token pada sebagian besar run, dan tidak
adanya kegagalan argumen tool.

## 2. Metode dan bukti pengukuran

- Backend uji: `AGENT_RUNTIME=hermes` di `127.0.0.1:8013`; gateway Hermes di
  `127.0.0.1:8642` PID 26708 setelah restart terkontrol. Port demo 8010/8011
  tidak disentuh.
- Pertanyaan tetap: visi UIR, rektor UIR, lokasi kampus, program studi.
- Alat ukur baru: `tests/ukur_stabilisasi.py` (latency, token `usage`, `langkah`,
  sitasi, jejak tool dari `/api/sessions/{id}/messages`) dan
  `tests/evaluasi_kualitas.py` (membandingkan URL di `output` run Hermes dengan
  `sumber` hasil tool).
- Berkas bukti: `docs/bukti_stabilisasi_sebelum_dasar.json`,
  `docs/bukti_stabilisasi_setelah_dasar.json`,
  `docs/bukti_stabilisasi_setelah2_dasar.json`,
  `docs/bukti_stabilisasi_setelah_konkurensi.json`,
  `docs/bukti_stabilisasi_setelah_isolasi.json`,
  `docs/evaluasi_kualitas_sebelum.json`, `docs/evaluasi_kualitas_setelah.json`.

### Statistik lengkap

| Sampel | n | Latency rata/median/P95 (dtk) | Token rata/median/P95 |
|---|---:|---|---|
| Sebelum batch A | 4 | 30,55 / 28,80 / 46,38 | 36.325 / — / — |
| Sebelum batch B | 4 | 18,70 / 16,70 / 29,26 | 30.952 / 31.244 / 36.333 |
| Sesudah batch A | 4 | 19,50 / 18,59 / 22,46 | 23.974 / 21.119 / 33.853 |
| Sesudah batch B | 4 | 25,91 / 25,68 / 38,99 | 36.007 / 37.784 / 48.370 |
| Sesudah gabungan | 8 | 22,70 / 20,62 / 38,99 | 29.990 / 26.783 / 48.370 |

Token dihitung dari `detail_model.usage.total_tokens` pada status run Hermes
(`input_tokens + output_tokens`). Daftar sesi Hermes juga mencatat
`cache_read_tokens`, tetapi angka itu tidak dipakai sebagai token tagihan run.

## 3. Perubahan yang diterapkan

### 3.1 Tool call tanpa argumen `query`

- `app/system_prompt.py` kini memuat bentuk panggilan eksplisit:
  `cari_uir({"query":"..."})`, `baca_cache({"url":"..."})`,
  `ambil_halaman({"url":"..."})`; larangan `tool_describe`; instruksi mengulang
  satu kali dengan argumen benar bila muncul `missing required argument`;
  maksimal satu `cari_uir` per pertanyaan; jangan membaca URL yang sama dua kali.
- `mcp_uir_server.py` deskripsi ketiga tool kini menyebut argumen wajib beserta
  contoh. Deskripsi baru termuat setelah gateway direstart.
- Bukti: `tool_describe` 4/4 → 0/8; `argumen_gagal` 0/8; `cari_uir` dan
  `baca_cache` dipakai 8/8 run; `ambil_halaman` 4/8 run.
- Jika batas iterasi tetap tercapai, `app/hermes_runtime.py` kini mengembalikan
  pesan pemulihan: sarankan pertanyaan lebih spesifik.

### 3.2 Error handling HTTP 401/403, 429, timeout, 5xx

`app/hermes_runtime.py`:

| Kondisi | Perlakuan baru | Status API |
|---|---|---|
| 401 / 403 | Tanpa retry, pesan generik, kode `kunci_tidak_tersedia` | `kunci_tidak_tersedia` |
| 429 | GET diulang maksimal 2 kali dengan backoff 0,5/1/2 dtk atau `Retry-After` (maks 5 dtk); POST tidak diulang agar tidak ada dua eksekusi | `rate_limit` |
| 5xx | GET diulang seperti 429; POST tidak diulang | `hermes_5xx` |
| Timeout / URLError / OSError | GET diulang; POST langsung gagal jujur | `timeout` / `penyedia_tidak_tersedia` |
| Error 4xx lain | `respons_tidak_valid` | `respons_tidak_valid` |

`frontend/js/app.js` menambah dua label status (`rate_limit`, `hermes_5xx`)
tanpa mengubah alur render. Kontrak lama tidak berubah. Uji unit mencakup retry
GET, tidak adanya retry POST, batas `Retry-After`, 401/403, 5xx habis retry, dan
kelanjutan status ke API (`tests/test_stabilisasi.py`).

### 3.3 Polling dan konfigurasi

- `HERMES_POLL` default 1,0 → 0,3 detik; `.env` dan `.env.example` diselaraskan.
- `HERMES_MAX_RETRY=2` ditambahkan ke `.env.example` (tanpa nilai rahasia).
- Runtime, model, MCP toolset, dan `agent.max_turns=5` tidak diubah.

### 3.4 Audit warning dan SQLite

| Warning | Penyelidikan | Keputusan |
|---|---|---|
| `source-update completion failed: [WinError 5] Access is denied` | Berasal dari `hermes_bootstrap.py` saat sinkronisasi checkout Hermes; gateway memakai dependensi generasi sebelumnya | Non-fatal. Gateway direstart dan `/health` kembali `ok`, versi 0.22.1. Tidak menjalankan `hermes update` karena berisiko mengubah lingkungan yang sedang bekerja |
| `automatic dependency repair retry limit reached` | Marker `%LOCALAPPDATA%\hermes-uir\.repair-incomplete` aktif; percobaan perbaikan otomatis habis | Non-fatal, tidak dihapus (tidak ada penghapusan file). Perbaikan manual `hermes pm repair` diserahkan ke pemilik lingkungan |
| SQLite `3.49.1 ... journal_mode=DELETE` | Semua DB Hermes (`state.db`, `response_store.db`, `runs_idempotency.db`, `kanban.db`) diverifikasi `journal_mode=delete`; tidak ada file `*-wal` | Hermes **sengaja** menghindari WAL karena bug korupsi reset WAL pada SQLite 3.49.1. Memaksa WAL tidak aman, jadi tidak dilakukan. Upgrade ke SQLite 3.51.3+ perlu perubahan runtime terkontrol |

Ketiga warning muncul lagi setelah restart, jadi sifatnya lingkungan, bukan
regresi kode proyek.

### 3.5 Kualitas jawaban dan sitasi

Evaluator memeriksa empat syarat: jawaban cukup panjang, ada sitasi terverifikasi,
semua URL di jawaban termasuk daftar `sumber` hasil tool, dan tidak ada URL
domain lain.

- Sebelum: 4/4 lolos, rata-rata 1,0 URL per jawaban.
- Sesudah: 4/4 lolos, rata-rata 1,5 URL per jawaban.
- Contoh manual: visi UIR dikutip dari `https://uir.ac.id/profil/`; alamat kampus
  dari `https://uir.ac.id/kontak/`; rektor dari berita resmi UIR 22 Juli 2025;
  program studi dari `https://uir.ac.id/program-pendidikan/`. Jawaban menyertakan
  keterbatasan bila halaman lanjutan belum terbaca.
- Sitasi masih dibangun hanya dari pesan `role=tool` `mcp__uir__*`; URL yang
  ditulis model tanpa tool tidak dijadikan bukti.

### 3.6 Konkurensi dan session isolation

- Empat permintaan bersamaan dengan empat `session_id` berbeda: 4/4 sukses,
  4 ID unik, total 30,57 dtk, 143.836 token kumulatif.
- Skenario isolasi: sesi A bertanya visi, lanjutan pada sesi A menjawab tahun
  target 2041 dari memori, sesi B bertanya rektor, sesi A ditanya ulang visi.
  Semua berhasil; transkrip Hermes menunjukkan sesi A hanya berisi percakapan
  visi dan sesi B hanya berisi percakapan rektor.
- `id_berbeda=True`, `id_sama=True`, `semua_berhasil=True`.

### 3.7 Evaluasi streaming

Endpoint Hermes `GET /v1/runs/{run_id}/events` diuji live: 9 event SSE
(`tool.started`, `tool.completed`, `run.completed`) dalam satu run. Streaming
**tidak** diaktifkan end-to-end karena `POST /api/chat` dan frontend
mengharapkan satu respons JSON lengkap; mengubahnya berisiko memecah kontrak
yang sudah lolos 14/14 dan 7/7 uji CDP. Frontend tetap memakai jalur non-stream.

## 4. Pengujian akhir

| Pengujian | Perintah | Hasil |
|---|---|---|
| Unit + integrasi Python | `python -m unittest discover -s tests` | 334 test, `OK`, exit 0 |
| CDP frontend mock | `node tests/uji_frontend_cdp.js` | 14/14 lulus |
| CDP frontend live | `APP_URL=http://127.0.0.1:8013/ node tests/uji_frontend_cdp_hermes.js` | 7/7 lulus setelah backend dimulai segar |
| Sintaks JS | `node --check ...` tiga berkas | lulus |
| Sintaks Python | `python -m py_compile ...` | lulus |
| Pindaian kredensial | bandingkan nilai `*_API_KEY` dengan berkas proyek | `RAHASIA_TERCETAK_DI_FILE_PROYEK=0` |
| Inferensi live pasca-restart gateway | satu chat visi UIR lewat port 8013 | `berhasil`, 3 langkah, `deepseek-v4.1-flash:free`, sitasi `https://uir.ac.id/profil/` |

Catatan: uji CDP live mengharuskan backend baru saja dimulai. Jika proses backend
sudah pernah melayani chat, health sah melaporkan `model_tersedia=true`, sehingga
Asersi "health awal belum terverifikasi" memerlukan backend yang baru dimulai.

## 5. File yang dibuat/diubah

Dibuat:
- `tests/ukur_stabilisasi.py`
- `tests/evaluasi_kualitas.py`
- `tests/test_stabilisasi.py`
- `docs/bukti_stabilisasi_sebelum_dasar.json`
- `docs/bukti_stabilisasi_setelah_dasar.json`
- `docs/bukti_stabilisasi_setelah2_dasar.json`
- `docs/bukti_stabilisasi_setelah_konkurensi.json`
- `docs/bukti_stabilisasi_setelah_isolasi.json`
- `docs/evaluasi_kualitas_sebelum.json`
- `docs/evaluasi_kualitas_setelah.json`
- `LAPORAN_STABILISASI.md`

Diubah:
- `app/system_prompt.py` (bentuk argumen tool, larangan `tool_describe`, disiplin pencarian)
- `app/hermes_runtime.py` (retry selektif GET, status 429/5xx, pesan pulih batas langkah)
- `mcp_uir_server.py` (deskripsi argumen wajib)
- `frontend/js/app.js` (dua label status baru)
- `tests/test_hermes_runtime.py` (uji pesan batas langkah)
- `.env`, `.env.example` (`HERMES_POLL=0.3`, `HERMES_MAX_RETRY=2`)

## 6. Masalah tersisa

1. Latency rata-rata/median/P95 tidak turun secara meyakinkan; variasi waktu
   inferensi DeepSeek mendominasi. Perlu sampel jauh lebih besar sebelum
   menyimpulkan perbaikan latency.
2. Dua dari delapan run mencapai 5 langkah (batas `max_turns`) meskipun tetap
   selesai. Bila sering terjadi, perlu evaluasi lanjutan terhadap strategi
   pencarian model, bukan menaikkan batas agen.
3. Warning `WinError 5`, dependency repair, dan SQLite 3.49.1 masih muncul di log
   gateway. Semua non-fatal, tetapi perlu perbaikan lingkungan oleh operator.
4. Streaming baru tersedia di level Hermes SSE; belum ada endpoint/UI streaming
   di aplikasi. Keputusan ini sengaja diambil untuk menjaga kontrak frontend.
5. Sesi masih disimpan di memori proses; restart backend menghilangkan riwayat
   dan status verifikasi model sampai ada chat live baru.

## 7. Rollback

`AGENT_RUNTIME=internal` masih tersedia dan tidak dihapus. Untuk kembali ke
konfigurasi sebelum stabilisasi tanpa mengganti runtime: kembalikan nilai
`HERMES_POLL` ke `1.0` dan hapus `HERMES_MAX_RETRY` dari lingkungan; perubahan
prompt hanya berupa instruksi tambahan dan tidak menyentuh struktur response.
Hermes Agent, model DeepSeek, MCP UIR, RAG/sitasi, isolation, dan frontend
tidak diubah atau dihapus.

Detail arsitektur dan bukti migrasi sebelumnya tetap ada di
[`LAPORAN_MIGRASI.md`](LAPORAN_MIGRASI.md),
[`docs/ARSITEKTUR_HERMES.md`](docs/ARSITEKTUR_HERMES.md), dan
[`README.md`](README.md).
