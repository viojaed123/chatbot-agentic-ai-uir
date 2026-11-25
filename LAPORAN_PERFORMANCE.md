# Laporan Optimasi Performa Chatbot Agentic AI UIR

Tanggal pengukuran: 25 November 2026 (UTC).
Objek: alur `frontend â†’ backend UIR (8013) â†’ Hermes API Server (8642) â†’ TokenHarbor/DeepSeek â†’ MCP tool UIR`.
Model: `deepseek-v4.1-flash:free` (`provider=custom`). Runtime: `AGENT_RUNTIME=hermes`.

## 1. Ringkasan eksekutif

Bottleneck **terbukti oleh pengukuran** berada di dalam loop reasoning Hermes/provider, bukan pada frontend, backend, atau polling HTTP. Optimasi dilakukan hanya pada jalur yang terbukti: menunggu run via SSE resmi Hermes (menggantikan polling berulang) dan merapikan system prompt agar model lebih disiplin memakai satu pencarian serta tidak membaca halaman yang sama berulang.

Hasil A/B dengan 8 pertanyaan identik (batch utama):

| Metrik | Sebelum | Sesudah | Perubahan |
|---|---:|---:|---:|
| Latency rata-rata | 19,48 dtk | 16,24 dtk | **-16,6%** |
| Latency median | 18,97 dtk | 16,96 dtk | **-10,6%** |
| Latency P95 | 35,55 dtk | 23,88 dtk | **-32,8%** |
| Permintaan berhasil | 7/8 | 8/8 | +1 |
| Token rata-rata | 32.798 | 26.673,62 | **-18,7%** |
| Token median | 33.526,5 | 24.611 | **-26,6%** |
| Rata-rata panggilan tool | 3,88 | 2,75 | **-29,1%** |
| Overhead HTTP polling | 0,45 dtk | 0,00 dtk | -100% |
| Lolos verifikasi sitasi | 7/8 (87,5%) | 8/8 (100%) | +12,5 poin |

Konkurensi 4 permintaan: total **51,79 dtk â†’ 23,44 dtk (-54,7%)**, latency rata-rata **38,54 â†’ 19,39 dtk (-49,7%)**, tetap 4/4 berhasil dengan 4 session ID unik.

Regression: **336 test Python OK** (baseline 334 + 2 test SSE baru), CDP mock **14/14**, CDP live **7/7**. Tidak ada perubahan pada Hermes, model/provider, MCP server, port 8010/8011/8642, atau kontrak API/UI.

Catatan kejujuran: perubahan prompt dan SSE diterapkan bersama, sehingga atribusi perubahan latency tidak dapat dipisahkan secara eksperimen murni. Variasi provider juga teramati; lihat Bagian 5 dan 9.

## 2. Metodologi pengukuran

### 2.1 Instrumentasi

- `trace_id` acak 8 byte hex dihasilkan per permintaan dan dikirim sebagai field top-level `/api/chat` serta di dalam `performansi.trace_id`. Trace ID bukan session ID dan tidak mengandung kredensial.
- `penanda_waktu` menyimpan epoch UTC (detik) untuk setiap penanda, sehingga waktu klien, backend, dan event Hermes dapat disatukan pada mesin yang sama.
- Penanda backend: `api_terima`, `hermes_run_post_mulai/selesai`, `hermes_polling_mulai/selesai`, `hermes_status_akhir`, `hermes_transkrip_mulai/selesai`, `hermes_jawaban_siap`, `respons_siap`, `respons_kirim`.
- Counter baru: `sse`, `sse_gagal`, `polling` di dalam `performansi`.
- Profiler membaca **replay SSE Hermes setelah run selesai**. Hanya nama event, `timestamp`, `seq`, nama tool, `tool_call_id`, flag error, dan panjang teks yang disimpan. Isi `preview`, `text`, `output`, halaman web, dan kredensial tidak ditulis ke berkas bukti.
- Pairing `tool.started â†’ tool.completed` memakai `tool_call_id` karena field `duration` dari tool UIR sering bernilai `0.0`.

### 2.2 Sembilan tahap yang diukur

| # | Tahap | Sumber bukti |
|---|---|---|
| 1 | klien/frontend â†’ backend menerima request | `klien_mulai` vs `penanda_waktu.api_terima` |
| 2 | backend â†’ POST `/v1/runs` selesai | `hermes_run_post_selesai` |
| 3 | run diterima â†’ event Hermes pertama (tunggu proxy model) | timestamp SSE pertama |
| 4 | jeda reasoning model antar tool | `tool.completed â†’ tool.started / run.completed` |
| 5 | pemanggilan MCP (UIR) | `tool.started â†’ tool.completed` per `tool_call_id` |
| 6 | retrieval/pengambilan halaman | subset tool `cari_uir`, `baca_cache`, `ambil_halaman` |
| 7 | menunggu terminal run + overhead HTTP | penanda backend + `tahapan.polling_status.total_detik` |
| 8 | model menyusun jawaban akhir + verifikasi sitasi | jeda ke `run.completed` + `hermes_jawaban_siap` |
| 9 | backend â†’ klien menerima JSON | `respons_kirim` vs `klien_selesai` |

Tahap 3â€“7 sebagian **berimbrikan**: angkanya bukan untuk dijumlahkan. Latency total diambil dari stopwatch klien.

### 2.3 Skenario A/B

- Dasar: 8 pertanyaan berurutan yang sama persis pada sebelum dan sesudah: visi, rektor, lokasi kampus, program studi, diulang dua kali.
- Konkurensi: 4 pertanyaan bersamaan dengan `ThreadPoolExecutor(max_workers=4)`.
- Isolasi: A â†’ lanjutan dengan `session_id` A â†’ B (pengguna lain) â†’ kembali ke A memakai `session_id` A.
- Backend uji memakai port 8013. Port demo 8010/8011 dan gateway 8642 tidak diubah.

Berkas bukti utama:

- `docs/profil_ringkasan_sebelum_dasar.json`, `docs/profil_performa_sebelum_dasar_20261125T110437Z.json`
- `docs/profil_ringkasan_sesudah_dasar.json`, `docs/profil_performa_sesudah_dasar_20261125T111752Z.json`
- `docs/profil_ringkasan_sebelum_konkurensi.json`, `docs/profil_ringkasan_sesudah_konkurensi.json`
- `docs/profil_ringkasan_sesudah_isolasi.json`, `docs/profil_performa_sesudah_isolasi_20261125T111843Z.json`
- `docs/evaluasi_kualitas_sebelum.json`, `docs/evaluasi_kualitas_sesudah.json`

## 3. Bottleneck yang terukur (baseline)

| Tahap | Rata | Median | P95 | Interpretasi |
|---|---:|---:|---:|---|
| 1. Frontend â†’ backend | 0,02 dtk | 0,01 | 0,10 | Diabaikan |
| 2. Backend â†’ run | 0,04 dtk | 0,04 | 0,04 | Diabaikan |
| 3. Tunggu event model pertama | 2,97 dtk | 2,64 | 4,88 | Signifikan, di dalam provider |
| 4. Jeda reasoning antar tool | 11,73 dtk | 12,57 | 20,16 | **Dominan** |
| 5. Total tool MCP | 4,54 dtk | 3,33 | 13,07 | **Dominan**; 3,88 panggilan/rata-rata |
| 6. Retrieval halaman UIR | 4,54 dtk | 3,33 | 13,07 | Sama dengan tahap 5 (semua tool memang UIR) |
| 7. Polling wall-clock | 19,41 dtk | 18,90 | 35,49 | Berimbrikan dengan tahap 3â€“6 |
| 7b. **Overhead HTTP polling saja** | 0,45 dtk | 0,43 | 0,84 | Kecil; Â±20 GET/permintaan |
| 8a. Jawaban akhir model | 4,92 dtk | 4,70 | 6,93 | Signifikan, di dalam provider |
| 8b. Verifikasi backend | 0,01 dtk | 0,01 | 0,03 | Diabaikan |
| 9. Backend â†’ frontend | 0,00 dtk | 0,00 | 0,00 | Diabaikan |

Rincian tool baseline (8 sampel): `cari_uir` rata-rata 1,974 dtk (maks 9,852), `ambil_halaman` rata-rata 1,830 dtk (maks 2,229), `baca_cache` rata-rata 0,036 dtk. Tiga sampel lambat memiliki satu pencarian UIR 5,6â€“9,9 dtk; dua di antaranya juga mengambil halaman tambahan.

Kesimpulan: **bukan frontend, bukan serialisasi backend, dan bukan overhead HTTP polling** yang mendominasi. Sekitar 16,7 dtk dari 19,48 dtk rata-rata berada di reasoning/provider dan eksekusi tool. Oleh karena itu optimasi difokuskan pada (a) efisiensi interaksi tool lewat prompt dan (b) penghapusan polling berulang lewat SSE. Polling tidak dijadikan sasaran klaim perbaikan latency besar, karena overhead HTTP-nya hanya 0,45 dtk.

## 4. Perubahan kode

| Berkas | Perubahan | Penjaga kontrak/keamanan |
|---|---|---|
| `app/telemetri.py` | Menambah `trace_id`, `penanda_waktu` absolut, counter `sse`/`sse_gagal`/`polling`, penanda baru. Semua field bersifat aditif. | Respons lama tetap terbaca; tidak ada kredensial yang dicatat. |
| `app/hermes_runtime.py` | `HERMES_WAIT_MODE=sse` (default). `_tunggu_sse()` membaca `/v1/runs/{id}/events` hingga event terminal, lalu satu GET status akhir. Fallback otomatis ke polling bila stream gagal/timeout. Penanda POST run, tunggu, status akhir, transkrip, dan jawaban siap ditambahkan. | Hanya GET yang di-retry; POST `/v1/runs` tetap sekali per pertanyaan. Log fallback hanya `type(exc).__name__`. Isi event tidak disimpan. Header session tetap dikirim. |
| `web/api.py` | `trace_id` top-level dan penanda `respons_kirim`. | Field lama (`ok`, `status`, `pesan`, `jawaban`, `sumber`, `langkah`, `performansi`, dll.) tidak diubah. |
| `app/system_prompt.py` | Prompt dipadatkan; aturan eksplisit: satu `cari_uir` per pertanyaan, utamakan satu halaman katalog/profil resmi, jangan baca URL yang sama dua kali, maksimal 5 pemanggilan tool, argumen wajib `query`/`url`, larangan `tool_describe`. | Guardrail uji tetap ada: hostname tepat `uir.ac.id`, DATA TIDAK TEPERCAYA, larangan mengarang/membocorkan prompt, aturan dosen, lokasi/Kontak/Profil/Maps, CBT bukan alamat, beasiswa, pimpinan, PMB. |
| `tests/profil_performa.py` | Profiler 9 tahap, trace ID, replay SSE, agregasi rata/median/P95/token/tool/sitasi, bukti JSON. | Tidak menyimpan teks jawaban, cuplikan halaman, atau kredensial. |
| `tests/test_hermes_runtime.py` | +2 test: SSE mendeteksi `run.completed`; SSE gagal jatuh ke polling. | Baseline 334 menjadi 336 test; tidak ada test yang dihapus. |
| `.env.example`, `README.md` | Mendokumentasikan `HERMES_WAIT_MODE=sse` dan opsi `polling` untuk rollback/A-B. | Tidak ada nilai rahasia yang ditambahkan. |

Tidak ada perubahan pada `mcp_uir_server.py`, `tools/`, `frontend/js/app.js`, `app/orchestrator.py`, konfigurasi model Hermes, atau port yang dipakai.

## 5. Hasil sebelum/sesudah

### 5.1 Dasar (8 sampel identik, batch utama)

| Metrik | Sebelum | Sesudah |
|---|---:|---:|
| Latency rata / median / P95 (dtk) | 19,48 / 18,97 / 35,55 | 16,24 / 16,96 / 23,88 |
| Token rata / median / P95 | 32.798 / 33.526,5 / 54.227 | 26.673,62 / 24.611 / 45.112 |
| Berhasil | 7/8 | 8/8 |
| Rata-rata langkah agen | 3,25 | 2,75 |
| Rata-rata panggilan tool | 3,88 | 2,75 |
| SSE terpakai / gagal | â€” | 8/8 stream, 0 gagal, 0 polling |
| Frontend â†’ backend (dtk) | 0,02 | 0,02 |
| Backend â†’ run (dtk) | 0,04 | 0,03 |
| Tunggu event model (dtk) | 2,97 | 3,18 |
| Jeda reasoning antar tool (dtk) | 11,73 | 11,37 |
| Total tool MCP (dtk) | 4,54 | 1,61 |
| Overhead HTTP polling (dtk) | 0,45 | 0,00 |
| Jawaban akhir model (dtk) | 4,92 | 5,94 |
| Verifikasi backend (dtk) | 0,01 | 0,02 |

Batch sesudah pertama (sebelum field counter ditambahkan ke profiler) menghasilkan latency rata/median/P95 **18,57 / 18,18 / 30,26 dtk**, token rata 29.188,88, dan 8/8 berhasil. Kedua batch sesudah lebih baik dari baseline, tetapi besarnya perbaikan bervariasi; tabel di atas memakai batch kedua sebagai primary.

Rincian tool pada batch utama:

| Tool | Sebelum: jumlah / rata / maks (dtk) | Sesudah: jumlah / rata / maks (dtk) |
|---|---:|---:|
| `cari_uir` | 8 / 1,974 / 9,852 | 8 / 0,727 / 5,646 |
| `baca_cache` | 12 / 0,036 / 0,139 | 10 / 0,030 / 0,073 |
| `ambil_halaman` | 11 / 1,830 / 2,229 | 4 / 1,685 / 1,947 |

Penurunan waktu tool terutama berasal dari **lebih sedikit panggilan** (`ambil_halaman` 11 â†’ 4), bukan dari tool menjadi lebih cepat. Waktu `ambil_halaman` per panggilan hampir sama. Jeda reasoning hanya turun 3,1% dan jawaban akhir justru naik 20,7%; keduanya tidak diklaim membaik karena berada di dalam provider dan dipengaruhi variasi respons.

### 5.2 Konkurensi 4

| Metrik | Sebelum | Sesudah |
|---|---:|---:|
| Total wall-clock | 51,79 dtk | 23,44 dtk |
| Berhasil | 4/4 | 4/4 |
| Session ID unik | 4 | 4 |
| Latency rata / median / P95 | 38,54 / 36,18 / 51,78 | 19,39 / 20,21 / 23,20 |
| Token rata-rata | 31.196 | 31.201 |
| Rata-rata panggilan tool | 3,50 | 3,25 |
| Overhead HTTP polling | 0,87 dtk | 0,00 dtk |

Token rata-rata praktis tidak berubah pada konkurensi; perbaikan waktu lebih banyak berasal dari berkurangnya antrean tool dan hilangnya polling HTTP.

## 6. Kualitas jawaban dan sitasi

Evaluator `tests/evaluasi_kualitas.py` memeriksa: setiap URL di jawaban harus berasal dari pesan tool `mcp__uir__*`, tidak ada URL domain lain, ada sitasi terverifikasi, dan panjang jawaban minimal 60 karakter. Ini adalah **verifikasi struktural/provenance**, bukan penilaian kebenaran fakta semantik.

| Metrik | Sebelum | Sesudah |
|---|---:|---:|
| Lolos verifikasi | 7/8 (87,5%) | 8/8 (100%) |
| Rata-rata URL per jawaban | 1,50 | 1,00 |
| Rata-rata entri sitasi terverifikasi | 1,25 | 1,00 |

Satu sampel baseline gagal pada pertanyaan program studi: status `penyedia_tidak_tersedia`, URL tidak tersumber, dan sitasi tidak terverifikasi. Pada batch sesudah, delapan jawaban semuanya memiliki URL yang dapat ditelusur ke tool UIR. Jumlah URL yang lebih sedikit konsisten dengan strategi "satu halaman resmi yang relevan lebih dulu"; tidak ada penambahan sumber di luar `uir.ac.id`.

## 7. Session isolation dan stabilitas

- Konkurensi sesudah: 4/4 berhasil, 4 session ID berbeda.
- Isolasi sesudah (skrip sudah diperbaiki): `id_berbeda=True`, `id_sama=True`, `semua_berhasil=True` untuk A â†’ lanjutan A â†’ B â†’ kembali ke A.
- Catatan: berkas `docs/profil_ringkasan_sebelum_isolasi.json` **tidak dipakai untuk A/B** karena skrip lama salah membandingkan sesi A, lanjutan, dan B (menampilkan `id_sama=False`). Perbaikan diterapkan sebelum pengukuran sesudah. Isolation juga diuji oleh unit test dan CDP.
- Backend di-restart dua kali selama pengujian; `/api/health` kembali 200. Riwayat sesi memang berada di memori dan ikut hilang saat restartâ€”ini perilaku yang sudah ada, bukan regresi.

## 8. Audit production readiness

| Area | Status | Bukti |
|---|---|---|
| 401 | Baik | Probe live gateway dengan kunci salah â†’ **401**; kunci sah untuk run yang tidak ada â†’ 404. Unit test memeriksa penolakan 401 pada pemeriksaan model. |
| 403 | Tidak ada bukti 403 live | Tidak dipicu sengaja agar tidak mengganggu gateway. Akses dibatasi oleh validasi URL HTTPS hostname tepat `uir.ac.id` dan tool read-only; unit test tersedia. Tidak ada klaim 403. |
| 429 / 5xx | Terkendali pada jalur yang diuji | Unit test mencakup HTTP 429 pada sitemap, 503 pada REST, timeout, dan `connection refused`. Retry hanya untuk GET, maksimal 2 dengan backoff; POST tidak diulang. Tidak ada 429/5xx yang dipicu pada gateway produksi selama audit. |
| Timeout & pemulihan gateway | Baik, dengan batas | `HERMES_TIMEOUT=180`. SSE memakai batas `min(timeout, 30 dtk)`; bila stream berakhir tanpa event terminal atau gagal, fallback polling aktif. Log fallback hanya nama jenis exception. Unit test membuktikan jalur sukses dan fallback. |
| Verifikasi model | Baik | Runtime menolak model/provider yang tidak cocok dengan konfigurasi; health tidak mengklaim model tersedia sebelum ada inferensi live. Setelah restart, health awal `model_tersedia=false`, lalu `true` setelah inferensiâ€”sesuai desain. |
| Stabilitas sesi | Baik | Isolasi live 4/4, lanjutan memakai `session_id` yang sama, CDP mock 14/14 termasuk reset dan dua sesi terpisah. |
| Keamanan MCP | Baik | Tiga tool bersifat baca/cache; hanya HTTPS hostname tepat `uir.ac.id`; robots.txt dipatuhi; isi halaman diperlakukan sebagai data tidak tepercaya; jawaban faktual wajib sitasi. Tidak ada URL asing pada berkas bukti evaluasi. |
| Kebocoran antar pengguna | Tidak teramati | Session ID unik pada konkurensi; uji isolasi A/B lolos; header session dikirim ke SSE maupun POST. |
| Kredensial | Baik | Pencarian pola kunci/token/secret di source tidak menemukan nilai tersimpan; `.env.example` kosong untuk `DEEPSEEK_API_KEY` dan `HERMES_API_KEY`; berkas bukti tidak memuat `Authorization`/`Bearer`. Kunci hanya dipakai pada header HTTP dan tidak diketik di log. |
| Logging & monitoring | Baik, aditif | `trace_id`, `penanda_waktu`, counter SSE/polling, dan `performansi` tersedia di respons. Tidak ada teks jawaban, cuplikan halaman, atau kredensial yang disimpan profiler. |
| Frontend | Baik | CDP mock 14/14 (XSS, sumber jahat, double-send, Shift+Enter, salin, reset sesi, responsif, tidak ada error konsol). CDP live terhadap 8013 dengan proses baru 7/7. |
| Regression test | Baik | `python -m unittest discover -s tests` â†’ **Ran 336 tests ... OK**. |
| Backup / rollback | Tersedia, perlu tindakan pengguna | Tidak ada commit/push/reset/hapus file; semua perubahan masih uncommitted. Rollback: `HERMES_WAIT_MODE=polling` untuk kembali ke polling; `AGENT_RUNTIME=internal` untuk runtime lama. Konfigurasi Hermes di `%LOCALAPPDATA%\hermes-uir\config.yaml` tidak diubah. |

Peringatan gateway yang sudah ada dan **tidak diperbaiki** dalam pekerjaan ini (di luar ruang lingkup optimasi): `source-update completion failed: [WinError 5] Access is denied`, `automatic dependency repair retry limit reached`, dan SQLite `journal_mode=DELETE` pada versi 3.49.1. Peringatan tersebut non-fatal; health gateway tetap `{"status":"ok","platform":"hermes-agent","version":"0.22.1"}`. WAL tidak dipaksa.

## 9. Masalah tersisa

1. **Variasi provider.** Dua batch sesudah menghasilkan latency rata 16,24 dan 18,57 dtk. Arah perbaikan konsisten, tetapi besarnya tidak stabil. P95 terbaik yang terukur masih 23,88 dtk.
2. **Jeda reasoning masih dominan.** Rata-rata 11,37 dtk antar tool dan 5,94 dtk untuk jawaban akhir. Keduanya berada di dalam Hermes/provider dan tidak dioptimasi karena model tidak boleh diganti.
3. **Atribusi perubahan tidak murni.** Prompt dan SSE diterapkan bersamaan. Perbaikan tool (-64,5% waktu MCP) lebih plausibel berasal dari prompt; hilangnya polling HTTP (-0,45 dtk) berasal dari SSE. Ini inferences, bukan hasil A/B terpisah per fitur.
4. **SSE punya batas 30 dtk per stream.** Run yang lebih lama akan jatuh ke polling. Ini disengaja agar tidak menunggu tanpa batas; belum ada pengukuran khusus untuk run >30 dtk.
5. **Evaluator kualitas bersifat struktural.** Ia menjamin provenance URL/sitasi, bukan kebenaran fakta. Pemeriksaan fakta manual tetap diperlukan untuk jawaban important.
6. **A/B isolasi tidak sepenuhnya valid.** Baseline isolasi dibuat dengan skrip yang salah; sesudah sudah benar, tetapi perbandingan before/after ketat tidak tersedia.
7. **Memori sesi tidak persisten.** Restart backend menghilangkan riwayat; sesuai desain privasi, tetapi perlu dicatat untuk deployment.
8. **Belum ada commit/git snapshot.** Karena semua berkas masih uncommitted, rollback bergantung pada env toggle dan cadangan manual. Disarankan pengguna membuat commit hanya setelah meninjau laporan ini.

## 10. Reproduksi dan rollback

```powershell
# Ukur ulang A/B dasar (backend uji di 8013, gateway di 8642)
python tests/profil_performa.py --fasa sesudah --mode dasar --ulang 8 --port 8013
python tests/profil_performa.py --fasa sesudah --mode konkurensi --pekerja 4 --port 8013
python tests/profil_performa.py --fasa sesudah --mode isolasi --port 8013

# Evaluasi kualitas/sitasi dari berkas bukti
python tests/evaluasi_kualitas.py --bukti docs/profil_performa_sesudah_dasar_20261125T111752Z.json

# Regression dan frontend
python -m unittest discover -s tests
$env:APP_URL = 'http://127.0.0.1:8013/'
node tests/uji_frontend_cdp.js
node tests/uji_frontend_cdp_hermes.js

# Rollback tanpa mengubah kode
$env:HERMES_WAIT_MODE = 'polling'   # kembali ke polling status run
$env:AGENT_RUNTIME = 'internal'     # rollback runtime lama (opsional)
```

Kesimpulan: perbaikan latency dan token **terukur** pada skenario identik, dengan batasan variasi provider dan atribusi gabungan. Fitur Agentic AI, Hermes, DeepSeek, MCP UIR, sitasi, session isolation, guardrails, frontend, dan kontrak API tetap dipertahankan.
